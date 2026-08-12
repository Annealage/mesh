"""AgentSession protocol and the AgentEvent dataclasses a session emits.

``AgentSession`` is the seam between the transport (``http/ws.py``) and
whatever is actually driving a conversation. Neither ``protocol.py`` nor
``viewers.py`` imports a concrete session; they, and ``ws.py``, see only
this Protocol, which is what lets the WebSocket layer be built and tested
in M4 before an agent exists. ``session/fake.py`` is the one concrete
implementation this milestone ships; ``session/sdk.py``, wrapping the real
``claude-agent-sdk`` client, is M5's.

A session owns its turn and keeps producing events regardless of whether a
browser is attached (plan section 3.4): it does not read from or write to
a WebSocket itself, and does not hold an ``EventLog`` or a
``ViewerRegistry``. It only calls the ``on_event`` callback given to its
constructor for every event it produces; whatever wires a session together
with an event log and a viewer registry (``http/ws.py`` or ``app.py``, not
this module) decides what that callback does. Keeping the session ignorant
of both is what lets ``session/fake.py`` cover every WebSocket and viewer
test with no event log and no registry in play at all, and what will let
``session/sdk.py`` be swapped in later without touching either.
"""

from __future__ import annotations

import dataclasses
from typing import ClassVar, Optional, Protocol, runtime_checkable

# session.agent values for the hello frame (plan section 3.3): the SDK
# client is not yet constructed, is constructed and answering, or failed
# to start. Viewer-only behaviour (pins, /submit, /callouts) never depends
# on this value; only the chat pane (M5) does, per plan section 3.4's
# "agent health never gates viewer health".
AGENT_CONNECTING = "connecting"
AGENT_READY = "ready"
AGENT_UNAVAILABLE = "unavailable"


class UnknownRequest(Exception):
    """Raised when a ``permission`` frame names no request awaiting a decision:
    never asked, or already resolved by another connection's decision, a
    timeout, the last viewer leaving, or shutdown.

    Lives here rather than beside the broker that raises it because
    ``http/ws.py`` has to catch it to answer the connection the losing frame
    arrived on, and ``ws.py`` must keep working with no agent SDK installed;
    importing the broker's module to name its exception would give the
    WebSocket layer an SDK dependency in viewer-only mode.

    Two tabs answering one card is the ordinary way this happens, so it is not
    an error in the sense of something being broken: the loser is owed the
    reply that their click did not decide anything, which is the one outcome a
    silently swallowed exception makes indistinguishable from success.
    """


class AgentEvent:
    """Base for every server-originated event kind.

    ``kind`` is the wire discriminator inside the ``event`` object of a
    server ``event`` frame; ``protocol.build_event`` wraps whatever
    ``to_wire()`` returns. It is a plain class attribute rather than a
    dataclass field, via the ``ClassVar`` annotation subclasses repeat, so
    each subclass fixes its own ``kind`` without every instance carrying
    a redundant copy of it.

    ``viewer`` is the forward seam plan section 3.3 asks for: the
    originating tab id when a later, collaborative mode traces an event
    back to the browser tab that caused it, and unset (``None``) when the
    server originated the event on its own, which is every event this
    milestone produces. It is the only place identity of any kind attaches
    to an event, matching the decision that multi-viewer here is one
    person's several devices, not several people: nothing else about an
    event ever carries authorship.

    Every subclass is a frozen dataclass, immutable once built: an event
    already appended to an ``EventLog`` and already sitting in another
    connection's outbound queue must not change out from under either.
    """

    kind: ClassVar[str] = ""

    def to_wire(self) -> dict:
        """The JSON-able object this event appears as inside an ``event`` frame.

        Every dataclass field on the concrete subclass, plus ``kind``,
        with ``None`` values dropped: none of the wire examples in plan
        section 3.3 show a field written out as null, and an *absent*
        ``viewer`` is exactly how a client tells a server-originated event
        apart from one attributed to a tab.
        """
        data = {"kind": self.kind}
        for field in dataclasses.fields(self):
            value = getattr(self, field.name)
            if value is not None:
                data[field.name] = value
        return data


@dataclasses.dataclass(frozen=True)
class TextDelta(AgentEvent):
    kind: ClassVar[str] = "text_delta"
    turn: int
    text: str
    viewer: Optional[str] = None


@dataclasses.dataclass(frozen=True)
class ToolUse(AgentEvent):
    kind: ClassVar[str] = "tool_use"
    turn: int
    tool_use_id: str
    name: str
    input: dict
    viewer: Optional[str] = None


@dataclasses.dataclass(frozen=True)
class ToolResult(AgentEvent):
    kind: ClassVar[str] = "tool_result"
    tool_use_id: str
    is_error: bool
    text: str
    viewer: Optional[str] = None


@dataclasses.dataclass(frozen=True)
class PermissionRequest(AgentEvent):
    kind: ClassVar[str] = "permission_request"
    request_id: str
    tool: str
    input: dict
    suggestions: list = dataclasses.field(default_factory=list)
    viewer: Optional[str] = None


@dataclasses.dataclass(frozen=True)
class PermissionResolved(AgentEvent):
    """A permission request is no longer awaiting a decision.

    Emitted exactly once per ``PermissionRequest``, whatever ended it, which is
    what makes a permission card's lifetime a pair of events rather than an
    event and an assumption. Two things depend on that.

    A browser that did not send the deciding frame has no other way to learn
    the card is answered, so without this a request answered on a phone stays
    clickable on a laptop, and the second click is refused for reasons that
    look like a bug.

    Replay reconstructs a reconnecting viewer's pane from the event log, so a
    request with no resolution in the log comes back as a live card on every
    reload, however long ago it was answered.

    ``outcome`` says how it ended, not merely that it did: the human's own
    ``allow``, ``allow_always`` or ``deny``, or one of the resolutions nobody
    clicked, ``timeout``, ``no_viewer`` and ``shutdown``. A pane that submitted
    a decision and sees a different outcome can then say so, rather than
    silently showing the human's deny as if it had taken effect.
    """

    kind: ClassVar[str] = "permission_resolved"
    request_id: str
    outcome: str
    viewer: Optional[str] = None


@dataclasses.dataclass(frozen=True)
class TurnEnd(AgentEvent):
    kind: ClassVar[str] = "turn_end"
    turn: int
    stop_reason: str
    cost_usd: float
    viewer: Optional[str] = None


@dataclasses.dataclass(frozen=True)
class CalloutsChanged(AgentEvent):
    """The callouts watcher's push, replacing the browser's 1.5 s poll.

    Carries no payload: the browser refetches ``GET /callouts`` and hands
    the result to ``store.setCallouts``, the single writer of that state
    (M3's store contract). Putting the changed content in this event
    instead would give that state a second writer.
    """

    kind: ClassVar[str] = "callouts_changed"
    viewer: Optional[str] = None


@dataclasses.dataclass(frozen=True)
class ViewerPrimary(AgentEvent):
    """Announces which connection now receives ``call`` frames.

    ``primary`` is the newly primary connection's tab id, or ``None`` when
    the last viewer disconnected and no connection holds the role. Every
    connection is broadcast this event on a change, including the
    connection that just became primary, so a chat pane can show whether
    the tab it is running in is the one driving tool calls.
    """

    kind: ClassVar[str] = "viewer_primary"
    primary: Optional[str] = None
    viewer: Optional[str] = None


@dataclasses.dataclass(frozen=True)
class SessionReset(AgentEvent):
    """Emitted when a requested resume (``-c``/``-r``) fails and the
    session falls back to starting fresh instead (plan section 3.4)."""

    kind: ClassVar[str] = "session_reset"
    reason: str
    viewer: Optional[str] = None


@dataclasses.dataclass(frozen=True)
class AgentError(AgentEvent):
    """Emitted when the CLI is missing, unauthenticated, or its child dies
    (plan section 3.4): the viewer, pins and ``/submit`` keep working,
    and this is how the chat pane learns to show a Retry affordance
    instead of hanging."""

    kind: ClassVar[str] = "agent_error"
    stderr: str
    remediation: str
    viewer: Optional[str] = None


@runtime_checkable
class AgentSession(Protocol):
    """What ``http/ws.py`` needs from whatever is driving a conversation.

    ``session_id``, ``sdk_session_id`` and ``cwd`` are read once per
    connection to build the ``hello`` frame's ``session`` object.
    ``sdk_session_id`` is ``None`` until the real SDK client has one to
    report (M5); it is never fabricated. The four coroutine methods are
    where ``ws.py`` dispatches an inbound ``turn``, ``permission`` or
    ``interrupt`` frame; there is deliberately no method for an inbound
    ``state`` frame, because a browser's camera/visibility snapshot is
    viewer state, not agent state, and belongs wherever a tool call reads
    "the current view" from, not here.

    A session never touches a WebSocket, an ``EventLog`` or a
    ``ViewerRegistry`` directly; it only calls ``on_event`` from its
    constructor for every event it produces. Whatever builds a session
    (``http/ws.py`` or ``app.py``) is responsible for making that callback
    append to an ``EventLog`` and broadcast through a ``ViewerRegistry``.
    """

    session_id: str
    sdk_session_id: Optional[str]
    cwd: str

    def agent_status(self) -> str:
        """One of AGENT_CONNECTING, AGENT_READY, AGENT_UNAVAILABLE."""
        ...

    async def submit_turn(self, blocks: list, viewer: Optional[str] = None) -> None:
        """Handle an inbound ``turn`` frame's ``blocks``.

        ``viewer`` is the tab id of the connection the frame arrived on,
        for a session that wants to stamp the events a turn produces
        (it is not required to)."""
        ...

    async def decide_permission(
        self, request_id: str, decision: str, message: str = ""
    ) -> None:
        """Handle an inbound ``permission`` frame."""
        ...

    async def interrupt(self) -> None:
        """Handle an inbound ``interrupt`` frame."""
        ...
