"""``can_use_tool`` permission broker: one ``asyncio.Future`` per outstanding
request, project-scoped allow-always grants, and the mapping onto
``PermissionUpdate`` that lets the SDK's own rule matcher take over a
remembered grant for the rest of a running session.

A bug here means a tool ran that the human never approved, so every path
through ``ask`` returns a ``PermissionResultAllow`` or
``PermissionResultDeny`` and nothing here ever raises out of ``ask`` itself
(fact 14 in ``docs/agent-chat-plan.md`` section 2a): a returned dict, or an
uncaught exception, both surface to the model as an infrastructure error
rather than a decision, which is worse than a denial with a clear reason.

Calling contract, mirroring ``viewers.py``'s own documented pattern rather
than inventing a second one:

- ``session/sdk.py`` binds ``options.can_use_tool = broker.ask`` directly;
  the SDK calls it once per tool invocation that its own rules evaluate to
  "ask" (fact 2: ``allowed_tools`` and ``permissions.allow`` rules never
  reach it at all).
- Whatever dispatches an inbound ``permission`` frame (``ws.py`` via
  ``AgentSession.decide_permission``) calls ``decide`` with the frame's
  ``request_id``, ``decision`` and ``message``.
- Whatever tracks connection count (the same wiring that calls
  ``ViewerRegistry.add``/``remove``) calls ``viewer_connected`` and
  ``viewer_disconnected`` on the same transitions, so ``ask`` knows whether
  anyone exists to answer before it ever creates a request, and a request
  already outstanding is denied the moment its last possible answerer
  leaves rather than at the end of a timeout that teaches nothing new.
- Whatever builds a fresh or reconnecting viewer's ``hello`` reply calls
  ``pending_requests`` and sends each one to that connection, since a
  request outstanding long enough to fall off the ``EventLog``'s 500-event
  ring is exactly what a plain seq replay cannot recover.
- The shutdown sequence (plan section 3.4) calls ``shutdown`` once.

Nothing here reads or writes a socket, holds a ``ViewerRegistry``, or
touches ``.mesh/config.toml`` or the user's ``settings.toml``; those are
reached through ``on_event`` and the four methods above, all of it
call-and-return with no reference back into this module's caller.
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path
from typing import Callable, Dict, FrozenSet, List, Optional, Tuple, Union

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny, PermissionUpdate
from claude_agent_sdk.types import PermissionRuleValue, ToolPermissionContext

from .base import AgentEvent, PermissionRequest

try:
    import tomllib  # Python 3.11+, stdlib, read-only.
except ImportError:  # Python 3.10: no tomllib, and this project takes no TOML dependency.
    tomllib = None  # type: ignore[assignment]

PermissionResult = Union[PermissionResultAllow, PermissionResultDeny]

# A human reviewing a running agent may be away from the keyboard for a
# while without that meaning "no"; a session nobody is watching should
# still not hold one tool call open forever. Five minutes is long enough
# for the first and short enough for the second. Always injectable, per
# the M5 brief: tests pass a small value, never sleep past a real one.
DEFAULT_TIMEOUT = 300.0

# Never persisted and never covered by an allow-always grant (plan section
# 5): a broad standing grant for the one tool with unrestricted shell
# access would turn one careless click into a permanent bypass. Enforced
# here, not only in the pane, because a compromised or buggy client could
# send ``allow_always`` for this tool regardless of what the UI offers.
NEVER_REMEMBERED = frozenset({"Bash"})

DEFAULT_DENY_MESSAGE = "the human reviewing this session denied the request"

_DENY_TIMEOUT_TEMPLATE = (
    "no decision was received within %g seconds and the request has expired; "
    "if this tool call is still needed, ask the human to reopen the mesh "
    "viewer and try again"
)

_DENY_ALL_GONE = (
    "every browser viewer disconnected while this request was awaiting a "
    "decision; ask the human to reopen the mesh viewer and try again"
)

_DENY_SHUTDOWN = (
    "the mesh session is shutting down and cannot ask a human to decide "
    "this; the request was not approved"
)

_DENY_EMIT_FAILED_TEMPLATE = (
    "could not deliver this permission request to the browser due to an "
    "internal error; the request was not approved"
)


class UnknownRequest(Exception):
    """Raised by ``decide`` when ``request_id`` names no request currently
    awaiting a decision: never asked, already decided by an earlier call,
    or already resolved by a timeout, ``viewer_disconnected`` reaching
    zero, or ``shutdown``. The caller (whatever dispatches an inbound
    ``permission`` frame) turns this into the "already decided" reply the
    losing tab is told, per plan section 3.4's two-tabs-racing rule,
    typically via ``protocol.build_refused`` on the connection that sent
    the losing frame; this exception itself carries no connection to send
    that on."""


class PermissionBroker:
    """One broker per running agent session. See the module docstring for
    the full calling contract."""

    def __init__(
        self,
        on_event: Callable[[AgentEvent], None],
        *,
        permissions_path: Optional[Union[str, Path]] = None,
        timeout: float = DEFAULT_TIMEOUT,
        viewer_url: Optional[str] = None,
    ):
        self._on_event = on_event
        self._permissions_path = Path(permissions_path) if permissions_path is not None else None
        self._timeout = timeout
        self._viewer_url = viewer_url
        self._viewer_count = 0
        self._shutdown = False
        self._next_id = 1
        # One future per outstanding request, keyed by the id ``ask``
        # allocates; cleaned up in ``ask``'s own ``finally`` regardless of
        # how the request ends, mirroring viewers.py's ``call``.
        self._pending: Dict[str, asyncio.Future] = {}
        # The PermissionRequest event for each entry above, kept alongside
        # it so decide() can look up which tool a request_id was for
        # without ask() having to pass it back out of band, and so
        # pending_requests() has something to hand a reconnecting viewer.
        self._open: Dict[str, PermissionRequest] = {}
        # Serialises the disk write inside _remember: two allow_always
        # decisions racing on the executor could otherwise complete out of
        # order and leave the file holding a smaller set than memory,
        # transiently losing a grant on disk (never in this process's own
        # memory, and never a false grant, only ever an under-write that a
        # later decision or restart-time re-prompt self-heals). See
        # _remember's docstring.
        self._write_lock = asyncio.Lock()
        self._granted_tools: FrozenSet[str] = _load_grants(self._permissions_path)

    # -- can_use_tool itself ----------------------------------------------

    async def ask(
        self, tool_name: str, input_data: dict, context: ToolPermissionContext
    ) -> PermissionResult:
        """The ``can_use_tool`` callback: bind as ``options.can_use_tool =
        broker.ask`` directly, never through a wrapper, since a wrapper
        that raises or returns something other than what this method
        returns defeats the one guarantee this module exists to give
        (fact 14: a returned dict or a raised exception both read to the
        model as the permission system erroring, not as a decision).

        ``context`` carries CLI-side hints (``suggestions``, ``title``,
        ``blocked_path`` and the rest of ``ToolPermissionContext``) this
        broker does not act on; it is accepted only to match
        ``CanUseTool``'s signature, and every field beyond ``tool_name``
        and ``input_data`` reaches the pane, if at all, through the
        ``permission_request`` event this method emits, which does not
        currently carry them (plan section 3.3's wire example has none).

        Every branch below returns a ``PermissionResult*``, and the two
        checks before a request is ever created (already granted, no
        viewer to ask) run with no ``await`` between them and this
        method's entry, so nothing else on this event loop can change
        either answer out from under this call before it commits to one.
        """
        if self._shutdown:
            return PermissionResultDeny(message=_DENY_SHUTDOWN)
        if tool_name in self._granted_tools:
            return PermissionResultAllow(updated_permissions=[_session_rule(tool_name)])
        if self._viewer_count == 0:
            return PermissionResultDeny(message=self._no_viewer_message())

        request_id = "pr_%d" % self._next_id
        self._next_id += 1
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        event = PermissionRequest(request_id=request_id, tool=tool_name, input=input_data)
        self._pending[request_id] = future
        self._open[request_id] = event
        try:
            try:
                self._on_event(event)
            except Exception as exc:
                # on_event is the wiring layer's own broadcast path
                # (EventLog.append plus ViewerRegistry.broadcast); a bug
                # there must not turn into this coroutine raising, which
                # is exactly the infrastructure-error failure fact 14
                # warns against. The request never reached a browser, so
                # there is nothing to wait for; deny at once instead of
                # awaiting a future nothing will ever resolve.
                sys.stderr.write(
                    "error: failed to emit permission_request %r for %r: %r\n"
                    % (request_id, tool_name, exc))
                return PermissionResultDeny(message=_DENY_EMIT_FAILED_TEMPLATE)
            try:
                return await asyncio.wait_for(future, timeout=self._timeout)
            except asyncio.TimeoutError:
                # asyncio.wait_for has already cancelled `future` (it
                # wraps any bare Future the same way it would a Task);
                # this method's own Deny is returned directly rather than
                # inspecting that future's now-cancelled state, so a
                # decide() that was mid-flight when the deadline hit and
                # loses the race against it is told "already decided" by
                # its own done() check, not by a value read off a future
                # this method has already stopped trusting.
                return PermissionResultDeny(message=_DENY_TIMEOUT_TEMPLATE % self._timeout)
        finally:
            self._pending.pop(request_id, None)
            self._open.pop(request_id, None)

    # -- resolving a request ------------------------------------------------

    async def decide(self, request_id: str, decision: str, message: str = "") -> None:
        """Resolve one outstanding request. Called for an inbound
        ``permission`` frame (plan section 3.3), dispatched here via
        ``AgentSession.decide_permission``.

        Raises ``UnknownRequest`` if ``request_id`` names nothing pending;
        see that exception's docstring for the four ways that happens.

        The lookup-and-check above, and the ``future.set_result`` that
        answers the request, happen with no ``await`` between them: two
        ``decide`` calls racing the same ``request_id`` (two tabs both
        holding the same card, or a human's click racing the timeout
        inside ``ask``) can therefore only ever observe the future
        strictly before or strictly after this method resolves it, never
        mid-resolution, so the loser's ``future.done()`` check always sees
        a definite answer. Without that guarantee, a second ``decide``
        that read ``done() is False`` before the first one's own
        ``set_result`` would call ``set_result`` again itself once it got
        the chance to run, and ``asyncio.Future.set_result`` on an
        already-done future raises ``InvalidStateError`` rather than
        losing gracefully, which would surface as an unhandled error deep
        inside whatever task happened to run second, not as the ordinary
        "already decided" reply the loser is owed. This is why the disk
        write for ``allow_always`` happens *after* this section, in
        ``_remember``, rather than before ``future.set_result``:
        persistence is not on the critical path of answering the tool
        call this decision unblocks, and putting it before the resolve
        would reintroduce exactly the ``await``-shaped gap this method is
        written to avoid.
        """
        future = self._pending.get(request_id)
        if future is None or future.done():
            raise UnknownRequest(
                "permission request %r was already decided or does not exist" % (request_id,))
        event = self._open.get(request_id)
        tool_name = event.tool if event is not None else ""
        result, grant = _build_result(tool_name, decision, message)
        future.set_result(result)
        if grant is not None:
            await self._remember(grant)

    async def _remember(self, tool_name: str) -> None:
        """Add ``tool_name`` to the in-memory grant set ``ask`` consults
        first, and persist it to ``.mesh/permissions.toml`` if this
        broker was given a path.

        The in-memory set is kept independently of the SDK's own
        in-session rule matching (the ``updated_permissions`` this
        broker's ``ask`` and this method's caller both return): if a
        future SDK version's live rule matching ever failed to cover a
        call this grant should have, ``ask``'s first check still catches
        it, since it never has to trust a second system's memory of a
        decision this process already made.

        The disk write is serialised by ``self._write_lock`` and always
        writes the *current* ``self._granted_tools`` at the moment it
        acquires the lock, not a value captured earlier: two grants
        arriving close together therefore never race each other's write
        to completion out of order, which could otherwise leave the file
        holding only the first grant even though memory holds both.
        """
        if tool_name in self._granted_tools:
            return
        self._granted_tools = self._granted_tools | {tool_name}
        if self._permissions_path is None:
            return
        async with self._write_lock:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None, _write_grants, self._permissions_path, self._granted_tools)

    # -- viewer presence ------------------------------------------------------

    def viewer_connected(self) -> None:
        """Record one browser connection becoming available to answer a
        request. Call once per successful WebSocket upgrade, on the same
        transition that calls ``ViewerRegistry.add``, so ``ask`` stops
        denying immediately once at least one viewer exists to ask."""
        self._viewer_count += 1

    def viewer_disconnected(self) -> None:
        """Record one browser connection going away, on the same
        transition that calls ``ViewerRegistry.remove``. Never lets the
        count go negative, so a caller that reports one disconnection
        twice cannot make a later, real connection look like it was never
        registered.

        When this drops the count to zero, every request still awaiting a
        decision is denied at once: waiting out the rest of ``ask``'s
        timeout to learn that nobody is left to answer teaches the model
        nothing a prompt denial would not, the same reasoning
        ``viewers.py``'s own ``ViewerGone`` applies to a stalled tool
        call. This is what keeps a request from outliving every viewer
        that could have answered it, rather than merely from outliving
        one particular viewer.
        """
        self._viewer_count = max(0, self._viewer_count - 1)
        if self._viewer_count == 0:
            self._deny_all_pending(_DENY_ALL_GONE)

    def _no_viewer_message(self) -> str:
        if self._viewer_url:
            return (
                "no browser viewer is connected to decide this permission request; "
                "ask the human to open %s, then try again" % self._viewer_url)
        return (
            "no browser viewer is connected to decide this permission request; "
            "ask the human to open the mesh viewer, then try again")

    # -- replay and shutdown -----------------------------------------------

    def pending_requests(self) -> List[PermissionRequest]:
        """Every ``permission_request`` still awaiting a decision, for a
        freshly connected or reconnected viewer.

        These are the same event objects originally passed to
        ``on_event``, not a re-fetch from the ``EventLog``: a request
        outstanding long enough to fall off the log's 500-event ring is
        exactly the case a plain seq replay cannot recover, and a request
        already answered is absent here the instant ``decide``, a
        timeout, ``viewer_disconnected`` reaching zero, or ``shutdown``
        resolves it, so a stale card is never handed to a fresh
        connection. The caller decides how to deliver these (send
        directly to the one new connection, or re-broadcast through
        ``on_event`` so every tab's log gains a fresh entry); this method
        only reports what is still open.
        """
        return list(self._open.values())

    def shutdown(self) -> None:
        """Deny every outstanding request and make every future ``ask``
        call deny immediately, for process shutdown (plan section 3.4).
        Idempotent: a second call finds nothing left pending and a flag
        already set."""
        self._shutdown = True
        self._deny_all_pending(_DENY_SHUTDOWN)

    def _deny_all_pending(self, message: str) -> None:
        for future in list(self._pending.values()):
            if not future.done():
                future.set_result(PermissionResultDeny(message=message))


# ---------------------------------------------------------------------------
# Decision application, pure and synchronous (see decide()'s docstring for
# why this must never await).
# ---------------------------------------------------------------------------


def _build_result(
    tool_name: str, decision: str, message: str
) -> Tuple[PermissionResult, Optional[str]]:
    """The ``PermissionResult`` for one decision, and the tool name to
    remember afterward (``None`` if nothing should be persisted)."""
    if decision == "allow":
        return PermissionResultAllow(), None
    if decision == "deny":
        return PermissionResultDeny(message=message or DEFAULT_DENY_MESSAGE), None
    if decision == "allow_always":
        if tool_name in NEVER_REMEMBERED:
            # The pane should never offer this button for this tool, but
            # this broker does not trust that it never will: downgraded to
            # a one-time allow rather than denied outright, because the
            # human already said "allow this call" and refusing the call
            # itself over a labelling mismatch would be a surprise in the
            # wrong direction. What is refused is only the memory of it.
            sys.stderr.write(
                "warning: allow_always for %r downgraded to a one-time allow; "
                "%r is never remembered (plan section 5)\n" % (tool_name, tool_name))
            return PermissionResultAllow(), None
        return PermissionResultAllow(updated_permissions=[_session_rule(tool_name)]), tool_name
    raise ValueError("unknown permission decision: %r" % (decision,))


def _session_rule(tool_name: str) -> PermissionUpdate:
    """The ``PermissionUpdate`` a remembered grant maps to: "map remembered
    grants through PermissionUpdate so the SDK does the matching" (plan
    section 5). ``destination="session"`` keeps the rule in the running
    CLI process's own memory only; ``.mesh/permissions.toml`` is what
    survives a restart, so a destination that also wrote a settings file
    on disk would be a second, competing place the same fact lives. A
    bare ``PermissionRuleValue`` with no ``rule_content`` matches any
    input for ``tool_name`` (fact 2's own convention for a whole-tool
    ``allowed_tools`` entry), which is the granularity the pane offers:
    per tool, never per input.
    """
    return PermissionUpdate(
        type="addRules",
        rules=[PermissionRuleValue(tool_name=tool_name, rule_content=None)],
        behavior="allow",
        destination="session",
    )


# ---------------------------------------------------------------------------
# .mesh/permissions.toml: load at construction, write on every new grant.
# ---------------------------------------------------------------------------

_GRANTS_KEY = "allow_always_tools"

_FALLBACK_ARRAY_RE = re.compile(_GRANTS_KEY + r"\s*=\s*\[(.*?)\]", re.DOTALL)
_FALLBACK_STRING_RE = re.compile(r'"((?:[^"\\]|\\.)*)"')


def _load_grants(path: Optional[Path]) -> FrozenSet[str]:
    """The allow-always grants recorded in ``path``, or an empty set if
    there is no path, no file yet, or the file cannot be read or parsed.

    Every failure here falls back to "no grants", never to raising:
    losing a remembered grant only costs one re-prompt for a tool the
    human already trusted, which is the safe direction (fact 17's own
    reasoning: fail toward more asking, not toward less). ``Bash`` is
    filtered out even if present, so a hand-edited file cannot reintroduce
    the one grant this module refuses to ever create itself.
    """
    if path is None:
        return frozenset()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return frozenset()
    except OSError as exc:
        sys.stderr.write(
            "warning: could not read %s: %r; starting with no remembered grants\n"
            % (path, exc))
        return frozenset()
    try:
        tools = _parse_grants(text)
    except ValueError as exc:
        sys.stderr.write(
            "warning: could not parse %s: %r; starting with no remembered grants\n"
            % (path, exc))
        return frozenset()
    return frozenset(t for t in tools if t not in NEVER_REMEMBERED)


def _parse_grants(text: str) -> List[str]:
    """Extract the ``allow_always_tools`` array from ``text``, the shape
    ``_write_grants`` produces.

    Uses the stdlib ``tomllib`` when available (Python 3.11+). On 3.10,
    where ``tomllib`` does not exist and this project takes no TOML
    dependency, falls back to a parser for exactly this module's own
    shape: one ``allow_always_tools = [...]`` assignment, one quoted
    string per element. A file in that shape, however reordered,
    recommented or reformatted, still parses under the fallback; a file
    using TOML features this shape has no need for (inline tables,
    multiline strings, a differently named key) does not, and is treated
    by the caller as unreadable rather than partially trusted.
    """
    if tomllib is not None:
        data = tomllib.loads(text)
        tools = data.get(_GRANTS_KEY, [])
    else:
        tools = _parse_grants_fallback(text)
    if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
        raise ValueError("%s must be an array of strings" % _GRANTS_KEY)
    return tools


def _parse_grants_fallback(text: str) -> List[str]:
    match = _FALLBACK_ARRAY_RE.search(text)
    if match is None:
        return []
    return [
        s.replace('\\"', '"').replace("\\\\", "\\")
        for s in _FALLBACK_STRING_RE.findall(match.group(1))
    ]


def _write_grants(path: Path, tools: FrozenSet[str]) -> None:
    """Write ``tools`` to ``path`` as valid, hand-editable TOML.

    Runs on the executor thread ``_remember`` schedules it on, not the
    event loop, matching the project's rule that blocking file IO never
    runs inline on the loop (plan section 3.1); this file is a handful of
    lines, but the rule is simplest to keep by never making an exception
    for "this one is small".

    Written to a sibling temp file and renamed into place, which on POSIX
    is atomic with respect to any concurrent open of ``path`` by name: a
    reader (this module's own ``_load_grants``, at the next process
    start) either sees the old complete file or the new complete one,
    never a half-written one from a write in progress.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Allow-always permission grants for this project (annealage-mesh).",
        "# Managed by annealage-mesh; hand-editing is safe, this is plain TOML.",
        "# Bash is never recorded here (plan section 5); an entry here would",
        "# be ignored on load regardless.",
        "%s = [" % _GRANTS_KEY,
    ]
    for tool in sorted(tools):
        lines.append("    %s," % _toml_string(tool))
    lines.append("]")
    lines.append("")
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(lines), encoding="utf-8")
    tmp.replace(path)


def _toml_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return '"%s"' % escaped
