"""Tests for ``session/codex.py``, driving ``CodexSession`` through a fake
``CodexClient`` (plan: ``phase3_codex-session.md``'s acceptance criteria).

``CodexClient`` has no injectable ``Transport`` the way ``ClaudeSDKClient``
does (``tests/test_sdk_session.py``'s ``FakeTransport``): it hardcodes a real
subprocess and a real reader thread internally. ``CodexSession``'s own
injection seam is one level up instead -- ``client_factory``, the callable
that would otherwise be ``CodexClient`` itself -- and ``FakeCodexClient``
below stands in for it, implementing the same public method surface
(``start``, ``initialize``, ``account_read``, ``thread_start``,
``turn_start``, ``register_turn_notifications``, ``next_turn_notification``,
``unregister_turn_notifications``, ``turn_interrupt``, ``close``) backed by
plain in-process ``queue.Queue`` objects instead of a stdio pipe.

``FakeCodexClient`` stores the ``approval_handler`` ``CodexSession`` passes
it, exactly as the real ``CodexClient.__init__`` does, so a test can invoke
it directly to play the part of the SDK's own internal reader thread. That
call must happen off the test's own event-loop thread (via
``loop.run_in_executor``), never awaited directly: the handler blocks on
``asyncio.run_coroutine_threadsafe(...).result()``, and calling it from the
loop thread itself would deadlock the one loop it needs to schedule the
broker's coroutine on -- the same real cross-thread behaviour production
code exercises against ``CodexClient``'s genuine reader thread.
"""

import asyncio
import queue
from types import SimpleNamespace

import pytest
from openai_codex.errors import TransportClosedError
from openai_codex.generated.v2_all import (
    AgentMessageDeltaNotification,
    ApprovalsReviewer,
    AskForApproval,
    AskForApprovalValue,
    CommandExecutionSource,
    CommandExecutionStatus,
    CommandExecutionThreadItem,
    ItemStartedNotification,
    SandboxMode,
    ThreadItem,
    Turn,
    TurnCompletedNotification,
    TurnStatus,
)
from openai_codex.models import Notification

from annealage_mesh.session.base import (
    AGENT_READY,
    AGENT_UNAVAILABLE,
    AgentError,
    AgentStatus,
    PermissionRequest,
    PermissionResolved,
    ToolUse,
    TurnEnd,
)
from annealage_mesh.session.codex import CodexSession
from annealage_mesh.session.permissions import PermissionBroker


class FakeCodexClient:
    """Stands in for ``openai_codex.client.CodexClient``. See module
    docstring for why this, not a lower-level transport, is the fake seam.
    """

    def __init__(self, *, config, approval_handler):
        self.config = config
        self.approval_handler = approval_handler
        self.started = False
        self.initialized = False
        self.closed = False
        self.account = SimpleNamespace(account=SimpleNamespace(type="chatgpt"), requires_openai_auth=False)
        self.thread_start_params = None
        self.turn_start_calls = []
        self.interrupt_calls = []
        self._next_turn = 0
        self._turn_queues = {}

    def start(self):
        self.started = True

    def initialize(self):
        self.initialized = True
        return SimpleNamespace(serverInfo=None, userAgent=None)

    def account_read(self, params=None):
        return self.account

    def thread_start(self, params):
        self.thread_start_params = params
        return SimpleNamespace(thread=SimpleNamespace(id="thread-1"))

    def thread_resume(self, thread_id, params=None):
        return SimpleNamespace(thread=SimpleNamespace(id=thread_id))

    def turn_start(self, thread_id, input_items, params=None):
        self._next_turn += 1
        turn_id = "turn-%d" % self._next_turn
        self.turn_start_calls.append(
            SimpleNamespace(thread_id=thread_id, input_items=input_items, params=params, turn_id=turn_id)
        )
        self._turn_queues[turn_id] = queue.Queue()
        return SimpleNamespace(turn=SimpleNamespace(id=turn_id))

    def register_turn_notifications(self, turn_id):
        self._turn_queues.setdefault(turn_id, queue.Queue())

    def unregister_turn_notifications(self, turn_id):
        pass

    def next_turn_notification(self, turn_id):
        item = self._turn_queues[turn_id].get()
        if isinstance(item, BaseException):
            raise item
        return item

    def turn_interrupt(self, thread_id, turn_id):
        self.interrupt_calls.append((thread_id, turn_id))
        return SimpleNamespace()

    def close(self):
        self.closed = True
        # Mirrors the real CodexClient's own fail_all(): closing the
        # transport wakes every blocked next_turn_notification() caller
        # with an exception rather than leaving it blocked forever. Without
        # this, a test whose own assertion fails before pushing
        # turn/completed would hang the whole run in
        # session.close()'s executor shutdown instead of failing cleanly.
        for turn_queue in self._turn_queues.values():
            turn_queue.put(TransportClosedError("fake client closed"))

    # -- test helpers --------------------------------------------------------

    def push_notification(self, turn_id, notification):
        self._turn_queues[turn_id].put(notification)

    def push_turn_completed(self, turn_id, status=TurnStatus.completed):
        self.push_notification(
            turn_id,
            Notification(
                method="turn/completed",
                payload=TurnCompletedNotification(
                    thread_id="thread-1",
                    turn=Turn(id=turn_id, items=[], status=status),
                ),
            ),
        )


def _command_execution_item(item_id="item-1", status=CommandExecutionStatus.in_progress):
    return ThreadItem(
        root=CommandExecutionThreadItem(
            id=item_id,
            command="rm -rf /",
            command_actions=[],
            cwd="/proj/root",
            source=CommandExecutionSource.agent,
            status=status,
            type="commandExecution",
        )
    )


class EventRecorder:
    """Collects every ``AgentEvent`` a ``CodexSession`` emits, and lets a
    test await the next one deterministically. Mirrors
    ``tests/test_sdk_session.py``'s recorder of the same name."""

    def __init__(self):
        self._queue: asyncio.Queue = asyncio.Queue()
        self.all = []

    def __call__(self, event) -> None:
        self.all.append(event)
        self._queue.put_nowait(event)

    async def next(self, timeout: float = 2.0, include_status: bool = False):
        while True:
            event = await asyncio.wait_for(self._queue.get(), timeout)
            if include_status or not isinstance(event, AgentStatus):
                return event


async def _started_session(*, viewer_count=1, **kwargs):
    """A ready ``CodexSession`` over a fresh ``FakeCodexClient``, plus the
    recorder and the fake client itself (for assertions and for driving
    notifications/approvals).

    Connects one viewer by default, since most tests exercise the ordinary
    "a human is here to answer" approval flow; the no-viewer tests pass
    ``viewer_count=0`` explicitly.
    """
    fake = FakeCodexClient(config=None, approval_handler=None)

    def _client_factory(*, config, approval_handler):
        # CodexSession only ever constructs one client per session, but the
        # real handler it passes is bound the first time start() runs;
        # record it on the same fake instance the test already holds a
        # reference to, rather than building a second object.
        fake.approval_handler = approval_handler
        return fake

    recorder = EventRecorder()
    broker = kwargs.pop("broker", "__default__")
    if broker == "__default__":
        broker = PermissionBroker(recorder, timeout=2.0, no_viewer_grace=0.05)
    session = CodexSession(
        recorder,
        cwd="/proj/root",
        session_id="mesh-sess-1",
        broker=broker,
        client_factory=_client_factory,
        **kwargs,
    )
    await session.start()
    assert session.agent_status() == AGENT_READY
    session.on_viewer_presence(viewer_count)
    return session, fake, recorder, broker


# ---------------------------------------------------------------------------
# initialize/initialized handshake
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_runs_the_initialize_handshake_and_becomes_ready():
    session, fake, recorder, broker = await _started_session()
    try:
        assert fake.started
        assert fake.initialized
        assert session.sdk_session_id == "thread-1"
        statuses = [e.status for e in recorder.all if isinstance(e, AgentStatus)]
        assert statuses == [AGENT_READY]
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_thread_start_uses_the_human_approvals_reviewer_never_auto_review():
    """The load-bearing safety assertion this ticket exists to pin: every
    write-class Codex call must reach a human, which requires
    ``approvals_reviewer=ApprovalsReviewer.user`` and the explicit
    ``on-request`` policy, constructed directly rather than through the
    curated ``ApprovalMode`` enum (whose ``auto_review`` member routes to an
    AI reviewer instead -- see
    ``planning/20260919_codex-approval-handler-finding.md``)."""
    session, fake, recorder, broker = await _started_session()
    try:
        params = fake.thread_start_params
        assert params.approvals_reviewer == ApprovalsReviewer.user
        assert params.approval_policy == AskForApproval(root=AskForApprovalValue.on_request)
        assert params.sandbox == SandboxMode.workspace_write
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_no_account_configured_fails_closed_without_starting_a_thread():
    """``account_read`` reporting no account must refuse to proceed to
    ``thread_start`` at all, not merely warn: starting a thread against an
    unauthenticated app-server would fail anyway, with a far less
    actionable error."""
    fake = FakeCodexClient(config=None, approval_handler=None)
    fake.account = SimpleNamespace(account=None, requires_openai_auth=True)
    recorder = EventRecorder()
    session = CodexSession(
        recorder,
        cwd="/proj/root",
        session_id="s",
        client_factory=lambda **kw: fake,
    )
    await session.start()

    assert session.agent_status() == AGENT_UNAVAILABLE
    assert fake.thread_start_params is None
    error = next(e for e in recorder.all if isinstance(e, AgentError))
    assert "codex login" in error.remediation


# ---------------------------------------------------------------------------
# turn/start -> commandExecution approval -> approval-handler -> turn/completed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approved_command_execution_reaches_turn_completed():
    session, fake, recorder, broker = await _started_session()
    try:
        await session.submit_turn([{"type": "text", "text": "delete everything"}])
        call = fake.turn_start_calls[0]
        assert call.input_items == [{"type": "text", "text": "delete everything"}]
        turn_id = call.turn_id

        fake.push_notification(
            turn_id,
            Notification(
                method="item/started",
                payload=ItemStartedNotification(
                    item=_command_execution_item(),
                    thread_id="thread-1",
                    turn_id=turn_id,
                    started_at_ms=0,
                ),
            ),
        )
        tool_use = await recorder.next()
        assert isinstance(tool_use, ToolUse)
        assert tool_use.name == "Bash"
        assert tool_use.input["command"] == "rm -rf /"

        # The reader thread's own synchronous call into the approval
        # handler, off the event loop, exactly as production code sees it.
        loop = asyncio.get_running_loop()
        approval_future = loop.run_in_executor(
            None,
            fake.approval_handler,
            "item/commandExecution/requestApproval",
            {"command": "rm -rf /", "cwd": "/proj/root"},
        )

        request = await recorder.next()
        assert isinstance(request, PermissionRequest)
        assert request.tool == "Bash"
        assert request.input == {"command": "rm -rf /", "cwd": "/proj/root"}

        await session.decide_permission(request.request_id, "allow")
        decision = await approval_future
        assert decision == {"decision": "accept"}

        resolved = await recorder.next()
        assert isinstance(resolved, PermissionResolved)
        assert resolved.outcome == "allow"

        fake.push_turn_completed(turn_id)
        end = await recorder.next()
        assert isinstance(end, TurnEnd)
        assert end.stop_reason == "completed"
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_allow_always_on_a_file_change_maps_to_accept_for_session():
    """``FileChange``, unlike ``Bash``, is not in the broker's
    ``NEVER_REMEMBERED`` set, so an ``allow_always`` decision for it is
    genuinely remembered and must map to ``acceptForSession``."""
    session, fake, recorder, broker = await _started_session()
    try:
        await session.submit_turn([{"type": "text", "text": "go"}])
        turn_id = fake.turn_start_calls[0].turn_id

        loop = asyncio.get_running_loop()
        approval_future = loop.run_in_executor(
            None, fake.approval_handler, "item/fileChange/requestApproval", {"path": "main.py"}
        )
        request = await recorder.next()
        assert request.tool == "FileChange"
        await session.decide_permission(request.request_id, "allow_always")
        decision = await approval_future
        assert decision == {"decision": "acceptForSession"}

        fake.push_turn_completed(turn_id)
        await recorder.next()
        await recorder.next()  # PermissionResolved, then TurnEnd; order not asserted here
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_allow_always_on_bash_is_downgraded_to_a_one_time_accept():
    """``Bash`` is deliberately in the broker's ``NEVER_REMEMBERED`` set
    (this file reuses Claude's own ``Bash`` name for the commandExecution
    approval precisely so that invariant covers Codex too): an
    ``allow_always`` decision for it is downgraded to a one-time allow, not
    remembered, so it must map to a plain ``accept``, never
    ``acceptForSession``."""
    session, fake, recorder, broker = await _started_session()
    try:
        await session.submit_turn([{"type": "text", "text": "go"}])
        turn_id = fake.turn_start_calls[0].turn_id

        loop = asyncio.get_running_loop()
        approval_future = loop.run_in_executor(
            None, fake.approval_handler, "item/commandExecution/requestApproval", {"command": "ls"}
        )
        request = await recorder.next()
        await session.decide_permission(request.request_id, "allow_always")
        decision = await approval_future
        assert decision == {"decision": "accept"}

        fake.push_turn_completed(turn_id)
        await recorder.next()
        await recorder.next()
    finally:
        await session.close()


def test_to_codex_decision_allow_with_no_remembered_tool_is_accept():
    from annealage_mesh.session.codex import _to_codex_decision
    from annealage_mesh.session.permissions import Decision

    assert _to_codex_decision(Decision(allow=True)) == {"decision": "accept"}


def test_to_codex_decision_allow_with_a_remembered_tool_is_accept_for_session():
    from annealage_mesh.session.codex import _to_codex_decision
    from annealage_mesh.session.permissions import Decision

    assert _to_codex_decision(Decision(allow=True, remember_tool="FileChange")) == {
        "decision": "acceptForSession"
    }


def test_to_codex_decision_deny_is_decline():
    from annealage_mesh.session.codex import _to_codex_decision
    from annealage_mesh.session.permissions import Decision

    assert _to_codex_decision(Decision(allow=False, message="no")) == {"decision": "decline"}


# ---------------------------------------------------------------------------
# decline path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_denied_command_execution_declines_and_the_turn_still_completes():
    session, fake, recorder, broker = await _started_session()
    try:
        await session.submit_turn([{"type": "text", "text": "delete everything"}])
        turn_id = fake.turn_start_calls[0].turn_id

        loop = asyncio.get_running_loop()
        approval_future = loop.run_in_executor(
            None,
            fake.approval_handler,
            "item/commandExecution/requestApproval",
            {"command": "rm -rf /"},
        )
        request = await recorder.next()
        await session.decide_permission(request.request_id, "deny", "not on my machine")
        decision = await approval_future
        assert decision == {"decision": "decline"}

        resolved = await recorder.next()
        assert isinstance(resolved, PermissionResolved)
        assert resolved.outcome == "deny"

        fake.push_turn_completed(turn_id)
        end = await recorder.next()
        assert isinstance(end, TurnEnd)
    finally:
        await session.close()


# ---------------------------------------------------------------------------
# no-viewer grace path (unchanged Phase 1 broker behaviour, exercised here)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_viewer_denies_a_command_execution_approval_without_a_card():
    """``on_viewer_presence(0)`` (no browser ever connected) must deny the
    very first approval request outright, before a ``PermissionRequest`` is
    ever created: nobody exists to answer a card."""
    session, fake, recorder, broker = await _started_session(viewer_count=0)
    try:
        loop = asyncio.get_running_loop()
        decision = await loop.run_in_executor(
            None,
            fake.approval_handler,
            "item/commandExecution/requestApproval",
            {"command": "rm -rf /"},
        )
        assert decision == {"decision": "decline"}
        assert not any(isinstance(e, PermissionRequest) for e in recorder.all)
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_a_viewer_that_connects_and_then_disconnects_still_denies():
    """``on_viewer_presence`` reaching zero after having been above zero
    goes through the broker's timed grace path rather than the immediate
    "never connected" branch; both must end in the same decline."""
    session, fake, recorder, broker = await _started_session()  # one viewer connected
    try:
        session.on_viewer_presence(0)
        await asyncio.sleep(0.1)  # past the 0.05s no_viewer_grace set in _started_session

        loop = asyncio.get_running_loop()
        decision = await loop.run_in_executor(
            None,
            fake.approval_handler,
            "item/commandExecution/requestApproval",
            {"command": "rm -rf /"},
        )
        assert decision == {"decision": "decline"}
    finally:
        await session.close()


# ---------------------------------------------------------------------------
# interrupt: the concurrency correction this file's docstring documents --
# an interrupt must not be queued behind the very turn it means to cut short.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_interrupt_reaches_the_client_while_a_turn_is_still_draining():
    session, fake, recorder, broker = await _started_session()
    try:
        await session.submit_turn([{"type": "text", "text": "go"}])
        turn_id = fake.turn_start_calls[0].turn_id

        # The drain thread is now blocked in next_turn_notification(turn_id)
        # on session._drain_executor. If interrupt() shared that executor
        # (the bug this file's docstring corrects), this call would queue
        # behind the drain and never reach the fake client until after
        # turn/completed was pushed below -- defeating the assertion order.
        await session.interrupt()
        assert fake.interrupt_calls == [("thread-1", turn_id)]

        fake.push_turn_completed(turn_id, status=TurnStatus.interrupted)
        end = await recorder.next()
        assert isinstance(end, TurnEnd)
        assert end.stop_reason == "interrupted"
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_text_delta_becomes_one_text_delta_event():
    from annealage_mesh.session.base import TextDelta

    session, fake, recorder, broker = await _started_session()
    try:
        await session.submit_turn([{"type": "text", "text": "hi"}])
        turn_id = fake.turn_start_calls[0].turn_id
        fake.push_notification(
            turn_id,
            Notification(
                method="item/agentMessage/delta",
                payload=AgentMessageDeltaNotification(
                    delta="hello", item_id="item-1", thread_id="thread-1", turn_id=turn_id
                ),
            ),
        )
        delta = await recorder.next()
        assert isinstance(delta, TextDelta)
        assert delta.text == "hello"

        fake.push_turn_completed(turn_id)
        await recorder.next()
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_close_shuts_the_client_and_marks_unavailable():
    session, fake, recorder, broker = await _started_session()
    await session.close()
    assert fake.closed
    assert session.agent_status() == AGENT_UNAVAILABLE
