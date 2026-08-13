"""The real agent session: a ``ClaudeSDKClient`` behind the ``AgentSession`` seam.

This module and ``session/permissions.py`` are the only two that import
``claude_agent_sdk``, and the split is deliberate. This file owns the client,
the options and the message pump; the broker owns the permission callback,
whose return type the SDK dictates (it must be a ``PermissionResultAllow`` or
``PermissionResultDeny``, and a dict is rejected outright), so interposing a
translation there would mean inventing a parallel vocabulary of decisions whose
only purpose is to be converted back one call later.

What the seam buys is not importability without the SDK, which stopped being a
question when the SDK became a base dependency. It is that ``session/fake.py``
substitutes for this whole file, so every HTTP, WebSocket, viewer and chat test
runs with no subprocess and no ``claude`` binary, and only
``tests/test_sdk_session.py`` drives the real client, through an injected
``Transport``. A third import site would mean something other than a session or
a permission decision had grown an SDK dependency, and that is worth resisting.

Three behaviours here are less obvious than they look and are the reason this
file is not a thin wrapper.

**The pump keeps consuming whether or not a browser is attached.** A turn
belongs to the session, not to a socket, so every event is appended to the
``EventLog`` and broadcast through the callback regardless of who is listening.
That is what makes a browser reloaded mid-turn, a browser closed and reopened,
and a process restart all the same mechanism rather than three.

**Agent health never gates viewer health.** A missing or unauthenticated CLI, a
child that dies, a malformed message: each becomes an ``AgentError`` event
carrying the child's own stderr, and the viewer, the pins and ``/submit`` keep
working. Nothing in this file may raise into the server's startup path.

**The sandbox is reported, never assumed.** ``sandbox={"enabled": True}``
degrades silently to unsandboxed execution when its dependencies are absent,
announcing that only on the child's stderr, so this file captures that stream
and parses it. The banner and ``doctor`` read ``sandbox_status()`` and state
which posture is actually in effect. A human who asked for a contained shell
and silently got a prompting one has been misled.

**The served directory's Claude configuration is watched for the whole run.**
A settings file there can allow tools outright, and an allow rule is applied
before ``can_use_tool`` is consulted, so such a file decides permissions
instead of the human. The CLI re-reads those files while the session runs,
which means accepting them once at startup does not keep them accepted. The
``PreToolUse`` hook installed here re-checks their digest on every tool call
and denies the call when it has changed. See ``session/workspace_trust.py``
for what is watched and why that is the boundary.
"""

import asyncio
import base64
import dataclasses
import os
import warnings
import re
import shutil
import sys
import platform
from typing import Any, Optional, Tuple

from claude_agent_sdk import (
    AssistantMessage,
    CanUseToolShadowedWarning,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from .. import paths
from ..tools import registry
from . import workspace_trust

from .base import (
    AGENT_CONNECTING,
    AGENT_READY,
    AGENT_UNAVAILABLE,
    AgentError,
    AgentStatus,
    PermissionRequest,
    SandboxStatus,
    SessionReset,
    TextDelta,
    ToolResult,
    ToolUse,
    TurnEnd,
    UnknownRequest,
)

# The mesh tools that never reach the broker, and therefore never interrupt the
# human: the read-class ones, which change nothing, and the view-class ones,
# which change only what is on the screen the human is already watching.
# Namespaced, because an in-process MCP tool is visible to the model as
# ``mcp__<server>__<tool>`` and a bare name in this list silently matches
# nothing. The write-class tools are deliberately absent: they leave something
# on disk, and their whole point is that they reach the human.
#
# Re-exported from ``tools/registry.py`` rather than restated, so the list this
# file pre-allows and the list that module refuses to build without cannot
# disagree. A name here that no tool answers to would be a pre-allowed tool
# that does not exist; a write-class name reaching this list would silently
# remove the human's approval card.
PRE_ALLOWED_MESH_TOOLS = registry.PRE_ALLOWED_MESH_TOOLS

# Settings files this session reads. "Behaves like Claude Code in that folder"
# requires it: the default is to load none at all, so a project CLAUDE.md would
# be ignored.
SETTING_SOURCES = ("user", "project", "local")

# Bash runs contained and unprompted; edit, write and network still reach the
# broker. ``git`` is excluded because it has to see the real filesystem to work
# on the project's own repository.
#
# ``allowUnsandboxedCommands`` is false because it defaults to true, and while
# true a command may opt out of containment by setting
# ``dangerouslyDisableSandbox``, which the model does when a sandbox denial
# blocks something it judges reasonable. That turns the posture from "bash is
# contained" into "bash is contained until it would rather not be": the opt-out
# reaches the broker, so a human is asked, but they are asked to approve a
# command whose containment has already been dropped, and the card looks like
# any other. False leaves exactly two ways to run outside the sandbox, both
# chosen here rather than by the model: the ``excludedCommands`` above, and
# viewer-only mode, which runs no agent at all.
SANDBOX_SETTINGS = {
    "enabled": True,
    "autoAllowBashIfSandboxed": True,
    "excludedCommands": ["git"],
    "allowUnsandboxedCommands": False,
}

# How the CLI announces that a requested sandbox could not engage. Matched
# loosely on purpose: the leading words and the separator have changed shape
# before, while "dependencies are missing" followed by a list has been stable,
# and a status that silently reads "active" because a log line was reworded is
# the one failure this parser exists to prevent. When the line is present but
# no dependency can be extracted, the status still reports inactive with an
# empty ``missing``, which the banner renders as an unreported dependency.
_SANDBOX_DISABLED_RE = re.compile(r"sandbox\s+disabled", re.IGNORECASE)
_SANDBOX_MISSING_RE = re.compile(
    r"dependencies?\s+are\s+missing:\s*(?P<items>[^·\n]+)", re.IGNORECASE)

# How many unparseable messages in a row are tolerated before the session gives
# up. One is a hiccup worth skipping; a stream of them means this build and the
# child no longer agree on the protocol, and continuing would spin forever
# logging warnings while the human waited for a reply that will never come.
_MAX_CONSECUTIVE_PARSE_FAILURES = 5

# Kept so a stderr burst from a wedged child cannot grow without bound while
# still leaving enough context to be worth reporting in an AgentError.
_STDERR_KEEP_LINES = 200

# The SDK warns, on every ``connect()``, that ``can_use_tool`` will not be
# consulted for the tools in ``allowed_tools``. Pre-allowing the mesh tools that
# do not need a human is exactly what that list is for, so on a correct run the
# warning is a paragraph of advice printed over the startup banner about a
# decision this file made deliberately.
#
# Filtered here, at import, rather than around the call: the warning is raised
# inside ``connect()``, and ``warnings.catch_warnings`` manipulates process-wide
# state, so holding it open across an ``await`` would suppress warnings for
# whatever else ran on the loop meanwhile. Narrowed to this one category, and
# the names it would list are pinned by a test, so a tool reaching that list
# unintentionally still fails there.
warnings.filterwarnings("ignore", category=CanUseToolShadowedWarning)


# -- turn image expansion (fact 6: dual delivery) ----------------------------
#
# An ``image_path`` block (``{"type": "image_path", "path": "images/<name>"}``)
# is this project's own invention, checked by ``protocol.py``'s
# ``_BLOCK_SPECS`` on the way in but meaningless to the CLI, which speaks only
# the Anthropic content-block shapes. ``_expand_turn_blocks`` is what turns
# such a block into something the model can actually see: an inline base64
# ``image`` block, standing next to a ``text`` block naming the same
# ``images/<name>`` string, so the model can later hand that path to its own
# tools. One without the other is half of fact 6's dual delivery, not a
# smaller version of it: pixels with no path leave the model unable to name
# what it saw, and a path with no pixels is not an attachment at all.
#
# Ordering is not a preference. Fact 6 requires every image block to precede
# whatever text refers to it; the observed failure when that is violated is
# the model ignoring the attached image outright and going hunting the
# filesystem for a file matching words in the trailing text instead of
# looking at what it was handed. ``_expand_turn_blocks`` therefore reorders
# unconditionally: every block this expansion produces for an ``image_path``
# (an accepted pair, or a rejection note standing in for one) is placed before
# every ``text`` block the caller supplied, regardless of how the two were
# interleaved on the wire.

# Per-turn cap on how many image_path blocks are expanded. store.js's own
# MAX_CHAT_ATTACHMENTS is 4, so a turn built by the shipped composer never
# reaches this; it is what still refuses a turn frame assembled by hand over
# the ``/ws`` protocol, or a future caller of ``submit_turn`` that does not
# share that policy. A block beyond the cap is dropped, not silently: one
# text note names every path dropped this way.
MAX_TURN_IMAGES = 4

# Cap on the sum of decoded (pre-base64) bytes this expansion will inline in
# one turn, checked against a running total as attachments are accepted, in
# encounter order, so a later small attachment can still fit after an earlier
# large one has used most of the budget rather than one oversized image
# starving every attachment behind it.
#
# Twice ``paths.MAX_INLINE_IMAGE_BYTES``, which is deliberately less than
# ``MAX_TURN_IMAGES`` times it: four attachments are allowed, but four at the
# per-image ceiling would put about 19 MiB of base64 into the single JSON line
# ``submit_turn`` writes and the CLI child must read whole before it can act on
# any of it, so this budget trims such a turn well before the count cap alone
# would. Together the two constants bound that line, which is the number
# ``TRANSPORT_BUFFER_BYTES`` below is derived from.
MAX_TURN_IMAGE_BYTES = 2 * paths.MAX_INLINE_IMAGE_BYTES

# Ceiling handed to the agent SDK for one line of the CLI's stdout.
#
# The SDK's subprocess transport refuses any single line longer than its buffer
# (``_internal/transport/subprocess_cli.py``, default 1 MiB) and raises a JSON
# decode error for it, which this file's pump counts as a parse failure and,
# after ``_MAX_CONSECUTIVE_PARSE_FAILURES`` of them, answers by ending the
# session. The CLI echoes each user message back on one line with its content
# intact, so a turn carrying an inlined image is read back at roughly its
# base64 size: on the default buffer an ordinary attachment would take the
# conversation to unavailable, which is why this is set rather than left alone.
#
# Derived from the turn budget above so the two cannot drift. Base64 inflates
# by four thirds, and the factor past that is measured rather than assumed: a
# real conversation record carrying one image was 330,846 bytes on a line whose
# base64 payload was 162,940, so the line runs to about twice the payload once
# the JSON envelope and the rest of the message are counted. Three times leaves
# margin over that, and it bounds a read rather than an allocation, so headroom
# here is close to free while being under it ends the session.
TRANSPORT_BUFFER_BYTES = 3 * (MAX_TURN_IMAGE_BYTES * 4 // 3)

# An ``image_path`` block's ``path`` is always "images/<name>" (plan section
# D): project-relative, one directory component, so the model can hand this
# exact string to its own tools later. Stripping this prefix is the first of
# two containment checks, and the only one that runs with no filesystem
# access at all: anything not shaped this way is refused on the string alone.
_IMAGES_PREFIX = paths.IMAGES_DIRNAME + "/"


class _ImageRejected(Exception):
    """One ``image_path`` block could not be expanded into pixels.

    Carries the sentence that becomes this attachment's text note, so a human
    who attached a file and gets no image back still learns why, rather than
    the attachment simply vanishing from the turn.
    """


# Longest a rejected attachment's path is quoted at inside a note, and the most
# paths the overflow note names before it switches to a count. Both bound the
# notes themselves: a ``turn`` frame is only checked for block shape, so its
# paths are arbitrary strings of any length up to the frame ceiling, and a note
# quoting them whole would turn the caps that exist to bound one JSON line into
# a way to grow it. It is also the one place an inbound string is copied into
# the model's context, which is reason enough to keep it short.
_PATH_IN_NOTE_CHARS = 120
_PATHS_NAMED_IN_NOTE = 3


def _show_path(path: Any) -> str:
    """``path`` as a short, quoted, single-line string safe to put in a note."""
    shown = path if isinstance(path, str) else repr(path)
    if len(shown) > _PATH_IN_NOTE_CHARS:
        shown = shown[:_PATH_IN_NOTE_CHARS] + "..."
    return repr(shown)


def _images_rel(path: Any) -> str:
    """The single path component after ``images/``, or raise ``_ImageRejected``.

    Refuses, without touching the filesystem: anything that is not a string,
    does not begin with ``images/``, names nothing after that prefix, or
    names more than one component (a second ``/``, which is what a traversal
    attempt such as ``images/../../etc/passwd`` produces once the prefix is
    stripped). ``resolve_asset`` below re-refuses a single-component name that
    still climbs out via a leading dot or a symlink; this check exists so the
    plainly malformed case never reaches it.
    """
    if not isinstance(path, str) or not path.startswith(_IMAGES_PREFIX):
        raise _ImageRejected(
            "an attachment was not an images/<name> reference and was "
            "dropped: %s" % (_show_path(path),))
    rel = path[len(_IMAGES_PREFIX):]
    if not rel or "/" in rel:
        raise _ImageRejected(
            "%s does not name a single file directly under images/ and was "
            "dropped" % (_show_path(path),))
    return rel


def _read_turn_image(serve_dir: str, path: Any) -> Tuple[str, bytes]:
    """Read and sniff one ``image_path`` block's file, or raise ``_ImageRejected``.

    ``paths.resolve_asset`` is the second and only filesystem-aware
    containment check: it refuses a name that resolves outside
    ``serve_dir``/images (including through a symlink), a symlinked
    ``images/`` itself, and anything that is not a regular file, returning the
    resolved path and the identity it validated. That identity is reasserted
    against the freshly opened descriptor below, the same re-check
    ``paths.read_fixed_file`` performs for the same reason: resolution and
    open are separate operations, and a served directory's ``images/`` is not
    a tree only this process writes to, so the name can be relinked in
    between.

    The size cap is ``paths.MAX_INLINE_IMAGE_BYTES``, what may be inlined,
    which is well below what ``/upload`` will store: a photograph a human
    attached is kept at full resolution as evidence, and a file above the
    inline cap is refused as a picture while still being named to the model as
    a path it can read. The check runs before the bytes are read, not after,
    because a file placed under ``images/`` by hand rather than through
    ``/upload`` is bounded by nothing else, and reading an oversized file just
    to reject it would spend the memory and IO this check exists to avoid.

    The media type is decided by ``paths.sniff_image`` against these bytes,
    never against ``path``'s own suffix: that suffix was chosen by this
    project from a file's bytes for anything ``/upload`` wrote, or was not
    chosen by anyone for a file a human dropped into ``images/`` directly,
    and either way it is the weaker source of the two.
    """
    rel = _images_rel(path)
    resolved = paths.resolve_asset(serve_dir, rel)
    if resolved is None:
        raise _ImageRejected(
            "%s does not resolve inside images/ and was dropped" % (_show_path(path),))
    target, identity = resolved
    try:
        fd = os.open(str(target), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise _ImageRejected(
            "%s could not be read (%s) and was dropped" % (_show_path(path), exc))
    try:
        st = os.fstat(fd)
        if (st.st_dev, st.st_ino) != identity:
            raise _ImageRejected(
                "%s changed while it was being attached and was dropped" % (_show_path(path),))
        if st.st_size > paths.MAX_INLINE_IMAGE_BYTES:
            # Neither an error nor a drop. The file is on disk, is served by
            # /asset and is named to the model in this very note, so only the
            # inline copy is withheld. Inlining it anyway would put a base64
            # block past what the API accepts into the turn, which fails the
            # whole turn rather than one attachment.
            raise _ImageRejected(
                "%s is %d bytes, over the %d byte limit for an image sent "
                "inline, so it was not attached as a picture. It is on disk at "
                "that path and can be read with the Read tool."
                % (_show_path(path), st.st_size, paths.MAX_INLINE_IMAGE_BYTES))
        chunks = []
        remaining = st.st_size
        while remaining > 0:
            chunk = os.read(fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
    except OSError as exc:
        raise _ImageRejected(
            "%s could not be read (%s) and was dropped" % (_show_path(path), exc))
    finally:
        os.close(fd)
    sniff = paths.sniff_image(data)
    if sniff is None:
        raise _ImageRejected(
            "%s is not a recognised image (PNG, JPEG or WEBP) and was dropped"
            % (_show_path(path),))
    media_type, _suffix = sniff
    return media_type, data


def _image_pair(path: str, media_type: str, data: bytes) -> list:
    """The two blocks one accepted attachment expands to: the pixels, then
    text naming the path the model can hand to its own tools afterwards. See
    this section's header comment for why neither block is sent alone."""
    return [
        {"type": "image", "source": {"type": "base64", "media_type": media_type,
                                      "data": base64.b64encode(data).decode("ascii")}},
        {"type": "text", "text": "attached image: %s" % path},
    ]


def _expand_turn_blocks(blocks: list, serve_dir: str) -> list:
    """Expand every ``image_path`` block in ``blocks`` into what the CLI
    understands, reordered so every image precedes every text block (see
    this section's header comment for why, citing fact 6).

    Runs on an executor thread (``submit_turn`` is the only caller): every
    check below except the pure string check in ``_images_rel`` touches the
    filesystem.

    Never raises for a bad, unreadable or oversized attachment, and never
    drops the turn: a rejected ``image_path`` becomes one ``text`` block
    stating why, so the human who attached a file that did not make it still
    learns something instead of the attachment silently vanishing. A
    text-only ``blocks`` (no ``image_path`` among them, the common case)
    returns with its text blocks in their original order and nothing else
    changed, since there is nothing here for this function to do.
    """
    images = [b for b in blocks if isinstance(b, dict) and b.get("type") == "image_path"]
    texts = [b for b in blocks
             if not (isinstance(b, dict) and b.get("type") == "image_path")
             and not _is_blank_text(b)]

    considered = images[:MAX_TURN_IMAGES]
    overflow = images[MAX_TURN_IMAGES:]

    expanded = []
    budget = MAX_TURN_IMAGE_BYTES
    for block in considered:
        path = block.get("path")
        try:
            media_type, data = _read_turn_image(serve_dir, path)
        except _ImageRejected as exc:
            expanded.append({"type": "text", "text": str(exc)})
            continue
        if len(data) > budget:
            expanded.append({"type": "text", "text": (
                "%s was not attached: this turn's %d byte image budget is "
                "already spent" % (path, MAX_TURN_IMAGE_BYTES))})
            continue
        budget -= len(data)
        expanded.extend(_image_pair(path, media_type, data))

    if overflow:
        named = ", ".join(_show_path(b.get("path"))
                          for b in overflow[:_PATHS_NAMED_IN_NOTE])
        rest = len(overflow) - min(len(overflow), _PATHS_NAMED_IN_NOTE)
        expanded.append({"type": "text", "text": (
            "%d more attachment(s) were dropped, over this turn's %d-attachment "
            "limit: %s%s" % (len(overflow), MAX_TURN_IMAGES, named,
                             ", and %d more" % rest if rest else ""))})

    out = expanded + texts
    # An empty content array is refused by the API exactly as an empty text
    # block is, so a frame whose every block was blank cannot be forwarded as
    # it stands. The composer will not send one (it requires text or an
    # attachment), which leaves a hand-assembled frame, and answering it with a
    # statement of what arrived is more use to both sides than a failed request.
    if not out:
        return [{"type": "text", "text": "(this message arrived empty)"}]
    return out


def _is_blank_text(block: Any) -> bool:
    """Whether ``block`` is a text block carrying nothing but whitespace.

    The API refuses an empty or whitespace-only text block outright and fails
    the request that carried it, so one reaching the transport costs the human
    the whole turn, image included. The composer sends an attachment with no
    typed message as exactly that shape, and a hand-assembled ``turn`` frame
    can too, which is why the check is here rather than only in the browser.
    """
    return (isinstance(block, dict) and block.get("type") == "text"
            and not str(block.get("text") or "").strip())


class SdkSession:
    """An ``AgentSession`` driving a real ``ClaudeSDKClient``.

    ``on_event`` is called with every ``AgentEvent`` this session produces, and
    is whatever the caller wired to append to an ``EventLog`` and broadcast
    through a ``ViewerRegistry``. This class never touches either, nor a
    socket; that separation is what lets the fake stand in for it.

    ``transport`` is passed through to ``ClaudeSDKClient`` untouched. It is the
    injection point ``tests/test_sdk_session.py`` uses to drive the real client
    with canned protocol dicts and no subprocess, which is the only way to
    notice an SDK upgrade breaking the control protocol.
    """

    def __init__(self, on_event, *, cwd, session_id, broker=None, model=None,
                 permission_mode=None, resume=None, sandbox=True,
                 mcp_servers=None, extra_allowed_tools=(), transport=None,
                 on_sdk_session_id=None, trusted_config_digest=None):
        self._on_event = on_event
        self.cwd = str(cwd)
        self.session_id = session_id
        self.sdk_session_id = None
        self._broker = broker
        self._model = model
        self._permission_mode = permission_mode
        self._resume = resume
        self._sandbox_requested = bool(sandbox)
        self._mcp_servers = mcp_servers or {}
        self._extra_allowed_tools = tuple(extra_allowed_tools)
        self._transport = transport
        self._on_sdk_session_id = on_sdk_session_id
        # The digest the startup gate accepted for this directory's Claude
        # configuration. None means no gate ran, which is the case for tests
        # that drive this class directly, and installs no tripwire.
        self._trusted_config_digest = trusted_config_digest
        self._config_change_reported = False

        self._status = AGENT_CONNECTING
        self._client = None
        self._pump_task = None
        self._closing = False
        self._turn = 0
        self._stderr_lines = []
        # Predicted now so the banner, printed once at startup, is right on a
        # machine that plainly cannot sandbox. Corrected by the child if it
        # reports its own list.
        self._sandbox_missing = (missing_sandbox_dependencies()
                                 if self._sandbox_requested else ())
        self._sandbox_child_reported = False
        self._viewers_seen = 0

    # -- AgentSession surface ------------------------------------------------

    def agent_status(self) -> str:
        return self._status

    def _set_status(self, status: str) -> None:
        """Record a status and announce the change, so a viewer already holding
        a ``hello`` snapshot of the old one is corrected.

        Announced only on a change, since the pane's status line is derived
        state and a repeat says nothing; announced *after* the field is set, so
        anything the callback does synchronously sees the new value.
        """
        if status == self._status:
            return
        self._status = status
        self._emit(AgentStatus(status=status))

    def sandbox_status(self) -> SandboxStatus:
        """The posture in effect, for the banner and ``doctor``."""
        return SandboxStatus(
            requested=self._sandbox_requested,
            active=self._sandbox_requested and not self._sandbox_missing,
            missing=self._sandbox_missing,
        )

    def on_viewer_presence(self, count: int) -> None:
        """Keep the broker's view of how many viewers exist in step with the
        registry's.

        The registry reports an absolute count, the broker counts connections
        up and down, and reconciling the two anywhere else would mean two
        places believing they know how many browsers are attached. Driving the
        broker to the registry's number, rather than passing the number
        through, keeps the broker's own ``viewer_disconnected`` side effect
        intact: reaching zero is what denies every request nobody is left to
        answer, and that has to fire exactly once, on the transition.
        """
        if self._broker is None:
            return
        while self._viewers_seen < count:
            self._broker.viewer_connected()
            self._viewers_seen += 1
        while self._viewers_seen > count:
            self._broker.viewer_disconnected()
            self._viewers_seen -= 1

    async def submit_turn(self, blocks: list, viewer: Optional[str] = None) -> None:
        """Send one user turn, given as content blocks, to the model.

        ``blocks`` is expanded by ``_expand_turn_blocks`` before it reaches the
        transport, not written verbatim: an ``image_path`` block is this
        project's own invention (``protocol.py``'s ``_BLOCK_SPECS``), unknown
        to the CLI, which understands only the Anthropic content-block shapes
        ``{"type": "text", ...}`` and ``{"type": "image", "source": {...}}``.
        The expansion is what turns an attached file into pixels the model
        actually sees; everything else about this method, one dict written as
        one JSON line with only ``session_id`` injected, is unchanged, which
        is what makes a text-only turn, still the common case, pass through
        untouched.

        The expansion is filesystem work (stat, read, sniff, base64), so it
        runs in an executor rather than blocking this coroutine or the pump
        reading the same loop.
        """
        if self._client is None or self._status == AGENT_UNAVAILABLE:
            self._emit(AgentError(
                stderr=self._recent_stderr(),
                remediation="the agent is not running, so this turn was not sent; "
                            "check the startup output for why and use Retry",
                viewer=viewer,
            ))
            return
        self._turn += 1
        try:
            loop = asyncio.get_running_loop()
            expanded = await loop.run_in_executor(
                None, _expand_turn_blocks, blocks, self.cwd)
            await self._client.query(_one_message({
                "type": "user",
                "message": {"role": "user", "content": expanded},
            }))
        except Exception as exc:
            self._fail(exc, viewer=viewer)

    async def decide_permission(self, request_id: str, decision: str,
                                message: str = "") -> None:
        """Route a human's decision to the broker.

        ``UnknownRequest`` propagates deliberately. Two tabs can hold one card
        and both answer it, and the one that lost has to be told: swallowing it
        here would make a human's Deny on an already-allowed request look
        exactly like a Deny that took effect, which is the one confusion this
        path must not create. ``ws.py`` catches it and answers the connection
        the losing frame arrived on.
        """
        if self._broker is None:
            return
        try:
            await self._broker.decide(request_id, decision, message)
        except UnknownRequest:
            raise
        except Exception as exc:
            sys.stderr.write("warning: permission decision %s was not applied: %r\n"
                             % (request_id, exc))

    async def interrupt(self) -> None:
        if self._client is None:
            return
        try:
            await self._client.interrupt()
        except Exception as exc:
            # An interrupt that fails is not worth an AgentError: the turn is
            # either already finished or the child is gone, and both of those
            # surface through the pump.
            sys.stderr.write("warning: interrupt failed: %r\n" % (exc,))

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        """Connect the client and start the pump; never raise.

        A failure here becomes an ``AgentError`` and leaves ``agent_status()``
        at unavailable, because the HTTP server starts first and independently
        and must keep serving the viewer whatever the agent does.
        """
        try:
            self._client = ClaudeSDKClient(options=self._build_options(),
                                           transport=self._transport)
            await self._client.connect()
        except Exception as exc:
            self._client = None
            self._fail(exc)
            return
        self._set_status(AGENT_READY)
        self._pump_task = asyncio.ensure_future(self._pump())

    async def close(self) -> None:
        self._closing = True
        if self._broker is not None:
            # Before the client goes: a request still awaiting a decision has to
            # be denied while there is still a socket to carry the event, and
            # while the SDK call it belongs to can still receive the result.
            self._broker.shutdown()
        if self._pump_task is not None and not self._pump_task.done():
            self._pump_task.cancel()
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception as exc:
                sys.stderr.write("warning: agent client did not disconnect cleanly: %r\n"
                                 % (exc,))
        self._client = None
        self._set_status(AGENT_UNAVAILABLE)

    def _build_options(self) -> ClaudeAgentOptions:
        allowed = list(PRE_ALLOWED_MESH_TOOLS) + list(self._extra_allowed_tools)
        kwargs = dict(
            cwd=self.cwd,
            setting_sources=list(SETTING_SOURCES),
            allowed_tools=allowed,
            # Token-level streaming. Without this only whole assistant
            # messages arrive, which is not a chat pane.
            include_partial_messages=True,
            stderr=self._note_stderr,
            mcp_servers=self._mcp_servers,
            # Only the servers named above. Without this the CLI also honours a
            # `.mcp.json` in the served directory, and an entry there names a
            # command to spawn, so an unreviewed file in a downloaded folder
            # would choose processes to run.
            strict_mcp_config=True,
            # Sized for the one line an attachment produces. The CLI echoes
            # each user message back whole, so a turn carrying an inlined image
            # is read back at roughly its base64 size, and the SDK's default
            # buffer would reject that line and end the session. See
            # TRANSPORT_BUFFER_BYTES.
            max_buffer_size=TRANSPORT_BUFFER_BYTES,
        )
        if self._sandbox_requested:
            kwargs["sandbox"] = dict(SANDBOX_SETTINGS)
        if self._trusted_config_digest is not None:
            kwargs["hooks"] = {
                "PreToolUse": [HookMatcher(matcher=None, hooks=[self._guard_config])],
            }
        if self._broker is not None:
            kwargs["can_use_tool"] = self._broker.ask
        if self._model:
            kwargs["model"] = self._model
        if self._permission_mode:
            kwargs["permission_mode"] = self._permission_mode
        if self._resume:
            # Always a resolved id, never continue_conversation: that option is
            # scoped to the CLI's notion of cwd, which is not necessarily this
            # project root, and an id stays inspectable on disk.
            kwargs["resume"] = self._resume
        return ClaudeAgentOptions(**kwargs)

    # -- the configuration tripwire ----------------------------------------

    async def _guard_config(self, hook_input, tool_use_id, context) -> dict:
        """Refuse every tool call once this directory's Claude configuration changes.

        Installed as a ``PreToolUse`` hook, which is the only place this check
        works. A settings file's allow rule is applied *before*
        ``can_use_tool``, so the broker never sees a call such a rule covers,
        and the CLI re-reads those files during the run, so a file written
        mid-session takes effect at once. A hook runs earlier than both: it is
        consulted for every call, including the ones an allow rule would have
        waved through and the ones the sandbox auto-approves without asking the
        broker.

        Returning ``{}`` expresses no opinion and leaves the permission flow
        exactly as it would be without this hook, which is what keeps the
        sandboxed-bash posture intact: a contained command still runs
        unprompted.

        The digest costs a read of a handful of small files per tool call.
        Watching the files rather than the ways they could be written is what
        makes this indifferent to whether a change arrived through ``Write``, a
        shell redirect, or a ``git checkout``.

        A failure to compute the digest denies, because a control that cannot
        tell whether configuration changed has to assume it did.
        """
        try:
            current = workspace_trust.config_digest(self.cwd)
            unchanged = current == self._trusted_config_digest
        except Exception as exc:
            sys.stderr.write("warning: could not check the served directory's "
                             "Claude configuration: %r\n" % (exc,))
            unchanged = False
        if unchanged:
            return {}
        if not self._config_change_reported:
            self._config_change_reported = True
            self._emit(AgentError(
                stderr="the Claude configuration under %s no longer matches what was "
                       "accepted when this session started" % self.cwd,
                remediation="tool calls are refused until this is resolved, because a "
                            "settings file there can grant tools without asking; "
                            "inspect .claude/ and .mcp.json, then restart mesh to "
                            "review and accept the current contents",
            ))
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    "Mesh refuses this call: the Claude configuration in the served "
                    "directory changed after the human accepted it, and configuration "
                    "there can grant tool permissions without asking. Do not attempt to "
                    "modify .claude/ or .mcp.json. Tell the human what you were trying "
                    "to do and that mesh must be restarted to review the change."),
            },
        }

    # -- stderr and the sandbox --------------------------------------------

    def _note_stderr(self, line: str) -> None:
        """Record a line of the child's stderr and watch it for the sandbox notice.

        Called by the SDK from whatever context it reads stderr on, so this
        does no IO and no awaiting: it appends to a bounded list and updates two
        fields.
        """
        self._stderr_lines.append(line)
        if len(self._stderr_lines) > _STDERR_KEEP_LINES:
            del self._stderr_lines[:-_STDERR_KEEP_LINES]
        if _SANDBOX_DISABLED_RE.search(line):
            match = _SANDBOX_MISSING_RE.search(line)
            reported = _parse_missing(match.group("items")) if match else ()
            # The child's own list replaces the PATH prediction, even when it is
            # empty: a disabled notice naming nothing still means disabled, and
            # an empty tuple would read as active, so a placeholder goes in.
            first_report = not self._sandbox_child_reported
            self._sandbox_missing = reported or ("an unreported dependency",)
            self._sandbox_child_reported = True
            if first_report:
                # The startup banner has already printed by the time a child
                # says this, and startup only checked that the binaries exist,
                # so this is the one path by which a run that was promised a
                # contained shell learns otherwise. It goes to the chat pane as
                # an error without marking the session unavailable: the agent
                # is working, it is the containment that is not.
                self._emit(AgentError(
                    stderr=line.strip(),
                    remediation="the agent's shell is NOT sandboxed for this run, so "
                                "bash will ask for approval instead of running "
                                "contained; restart once %s is available"
                                % ", ".join(self._sandbox_missing),
                ))

    def _recent_stderr(self) -> str:
        return "\n".join(self._stderr_lines[-40:])

    # -- the pump ------------------------------------------------------------

    async def _pump(self) -> None:
        """Consume SDK messages forever, turning each into an ``AgentEvent``.

        Runs whether or not a browser is attached. Cancellation is the ordinary
        end of this task, on ``close``; anything else is reported as an
        ``AgentError`` and leaves the session unavailable rather than
        propagating into whatever task happened to spawn this one.
        """
        parse_failures = 0
        while True:
            try:
                async for message in self._client.receive_messages():
                    parse_failures = 0
                    try:
                        self._handle(message)
                    except Exception as exc:
                        # One message this file cannot make sense of must not end
                        # the conversation.
                        sys.stderr.write("warning: could not handle %s: %r\n"
                                         % (type(message).__name__, exc))
            except asyncio.CancelledError:
                return
            except Exception as exc:
                if self._closing:
                    return
                # A message the SDK's own parser rejects raises here rather than
                # inside the guard above, because the parsing happens inside the
                # generator this loop iterates, one level up from _handle. That
                # kills the generator, so surviving it means starting a new one:
                # the underlying channel is not the generator, it persists, so
                # re-entering resumes consumption and loses only the message
                # that could not be parsed.
                if _is_parse_error(exc) and parse_failures < _MAX_CONSECUTIVE_PARSE_FAILURES:
                    parse_failures += 1
                    sys.stderr.write(
                        "warning: skipping a message the SDK could not parse (%d of %d "
                        "in a row allowed): %r\n"
                        % (parse_failures, _MAX_CONSECUTIVE_PARSE_FAILURES, exc))
                    continue
                self._fail(exc)
                return

    def _handle(self, message: Any) -> None:
        if isinstance(message, StreamEvent):
            self._handle_stream_event(message)
            return
        if isinstance(message, AssistantMessage):
            self._remember_sdk_session(getattr(message, "session_id", None))
            for block in message.content or []:
                if isinstance(block, ToolUseBlock):
                    self._emit(ToolUse(turn=self._turn, tool_use_id=block.id,
                                       name=block.name, input=block.input or {}))
                # A TextBlock here is the whole assistant text, which the
                # stream events have already delivered delta by delta; emitting
                # it again would double every reply in the pane.
            return
        if isinstance(message, UserMessage):
            for block in message.content or []:
                if isinstance(block, ToolResultBlock):
                    self._emit(ToolResult(
                        tool_use_id=block.tool_use_id,
                        is_error=bool(block.is_error),
                        text=_content_to_text(block.content),
                    ))
            return
        if isinstance(message, ResultMessage):
            self._remember_sdk_session(getattr(message, "session_id", None))
            self._emit(TurnEnd(
                turn=self._turn,
                stop_reason=message.stop_reason or message.subtype or "end_turn",
                cost_usd=message.total_cost_usd,
            ))
            return
        if isinstance(message, SystemMessage):
            self._handle_system(message)
            return

    def _handle_stream_event(self, message: StreamEvent) -> None:
        """Turn one raw API stream event into a ``TextDelta``, or ignore it.

        The event dict is the Anthropic API's own shape, so this reads it
        defensively: a kind this does not recognise is not an error, it is a
        part of the stream the chat pane has no use for.
        """
        self._remember_sdk_session(getattr(message, "session_id", None))
        event = message.event or {}
        if event.get("type") != "content_block_delta":
            return
        delta = event.get("delta") or {}
        if delta.get("type") != "text_delta":
            return
        text = delta.get("text") or ""
        if text:
            self._emit(TextDelta(turn=self._turn, text=text))

    def _handle_system(self, message: SystemMessage) -> None:
        """Report a system message that the human needs to see.

        The SDK's system messages are mostly bookkeeping. The one shape worth
        surfacing is an init that reports a different session id than the one
        being resumed, which means the resume did not take and the
        conversation the human expected to continue is not the one they have.
        """
        data = message.data or {}
        self._remember_sdk_session(data.get("session_id"))
        if message.subtype == "init" and self._resume:
            reported = data.get("session_id")
            if reported and reported != self._resume:
                self._emit(SessionReset(
                    reason="asked to resume %s and the agent started %s instead; "
                           "this is a new conversation" % (self._resume, reported)))

    def _remember_sdk_session(self, sdk_session_id) -> None:
        """Record the SDK's own session id, which is never fabricated: it stays
        None until the client reports one, because the hello frame publishes it
        and a made-up value there would be a lie the human could act on."""
        if sdk_session_id and sdk_session_id != self.sdk_session_id:
            self.sdk_session_id = sdk_session_id
            if self._on_sdk_session_id is not None:
                try:
                    self._on_sdk_session_id(sdk_session_id)
                except Exception as exc:
                    # Recording the id is what lets a later -c or -r resume this
                    # conversation; failing to record it costs that and nothing
                    # in the running session, so it is reported, not raised.
                    sys.stderr.write("warning: could not record the SDK session id: %r\n"
                                     % (exc,))

    # -- failure -------------------------------------------------------------

    def _fail(self, exc: BaseException, viewer: Optional[str] = None) -> None:
        """Report a failure as an event and mark the session unavailable.

        Never raises and never stops the server. The captured stderr goes with
        it, because for the common failures (no CLI, not authenticated) the
        child's own message is the only thing that tells the human what to do.
        """
        self._set_status(AGENT_UNAVAILABLE)
        self._emit(AgentError(
            stderr=(self._recent_stderr() or "%s: %s" % (type(exc).__name__, exc)),
            remediation=_remediation_for(exc),
            viewer=viewer,
        ))

    def _emit(self, event) -> None:
        try:
            self._on_event(event)
        except Exception as exc:
            # The callback appends to a log and broadcasts; a failure there must
            # not take down the pump, or one bad frame would end the session.
            sys.stderr.write("warning: could not deliver %s: %r\n"
                             % (type(event).__name__, exc))


# The two binaries the CLI's own disabled-sandbox message names, and the
# packages they usually come from. Both are required, unconditionally: the CLI
# puts a missing socat in its own errors list rather than its warnings, with no
# branch on whether network access was configured, so a Linux host without it
# gets no sandbox at all rather than a sandbox with no network.
SANDBOX_DEPENDENCIES = ("bwrap", "socat")
SANDBOX_PACKAGES = "bubblewrap socat"


def sandbox_needs_external_dependencies() -> bool:
    """Whether this platform's sandbox is built from separate binaries.

    Only Linux, which includes WSL. macOS sandboxes with ``sandbox-exec``,
    which ships with the OS, and Windows has a mechanism of its own; on neither
    is there anything for a user to install, so neither has a requirement to
    enforce.
    """
    return platform.system() == "Linux"


def missing_sandbox_dependencies() -> Tuple[str, ...]:
    """Which sandbox dependencies are absent from PATH.

    Best effort rather than authoritative: this checks that a binary of the
    right name is findable, and the CLI additionally checks that it is
    executable, so a present but broken binary passes here and fails there. That
    is why the child's own report still supersedes this whenever it arrives,
    even though startup refuses on this check alone.
    """
    if not sandbox_needs_external_dependencies():
        return ()
    return tuple(name for name in SANDBOX_DEPENDENCIES if shutil.which(name) is None)


def _parse_missing(items: str) -> Tuple[str, ...]:
    """Extract dependency names from the CLI's missing-dependencies list.

    The list reads like ``socat not installed`` and may hold several entries.
    Only the leading token of each entry is kept, since the rest is prose about
    the entry rather than part of its name.
    """
    found = []
    for chunk in items.split(","):
        words = chunk.strip().split()
        if words:
            found.append(words[0])
    return tuple(found)


async def _one_message(message: dict):
    """Yield one message, as the async iterable ``query()`` requires.

    ``ClaudeSDKClient.query()`` accepts a string or an async iterable of dicts,
    and tests only for the string case before doing ``async for`` on whatever
    else it was given. A plain dict therefore raises ``TypeError: 'async for'
    requires an object with __aiter__`` before anything reaches the transport,
    which this file's own error handling would then report as the agent having
    failed rather than as a turn never having been sent.
    """
    yield message


def _is_parse_error(exc: BaseException) -> bool:
    """Whether ``exc`` is the SDK failing to parse one message.

    Matched by class name rather than by importing ``MessageParseError``, which
    lives under ``_internal``: a rename there should cost this a skipped message
    and a warning, not an ImportError at startup.
    """
    return type(exc).__name__ in ("MessageParseError", "CLIJSONDecodeError")


def _content_to_text(content: Any) -> str:
    """Flatten a tool result's content to text for the chat pane.

    A tool result is a string, or a list of blocks that may be text, an image,
    or a shape this does not know. Only text is rendered; anything else is
    named rather than dropped silently, so a human reading the pane can tell
    that something came back and that it was not text.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts = []
    for block in content:
        if isinstance(block, TextBlock):
            parts.append(block.text)
        elif isinstance(block, dict):
            if block.get("type") == "text":
                parts.append(block.get("text") or "")
            else:
                parts.append("[%s]" % (block.get("type") or "non-text content"))
        else:
            text = getattr(block, "text", None)
            parts.append(text if text is not None else "[non-text content]")
    return "".join(parts)


def _remediation_for(exc: BaseException) -> str:
    """Text telling the human what to do about a failure, by its class name.

    Matched on the class name rather than by importing each exception, so a
    renamed or added SDK error degrades to the generic message instead of an
    ImportError at startup.
    """
    name = type(exc).__name__
    if name == "CLINotFoundError":
        return ("the claude CLI could not be found; the SDK normally bundles it, so "
                "this usually means the package was installed from its source "
                "distribution, and claude needs to be on PATH")
    if name == "CLIConnectionError":
        return ("the claude CLI could not be started or lost its connection; run "
                "annealage-mesh doctor, and check that it is authenticated")
    if name == "CLIJSONDecodeError":
        return ("the claude CLI sent something this build could not parse, which "
                "usually means a version mismatch; check the pinned SDK range")
    return "the agent is unavailable; the captured output above is what it reported"
