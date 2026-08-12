"""Tests for ``session/permissions.py``: the ``can_use_tool`` broker.

Every test asserts on the ``PermissionResultAllow``/``PermissionResultDeny``
object ``ask`` returns, never on an internal predicate such as
``_granted_tools`` or ``_viewer_count`` in isolation: a predicate can be
correct while the code path that is supposed to consult it is wired wrong,
and the SDK rejects anything that is not one of those two types outright
(plan section 2a, fact 14), so that object is the only contract worth
pinning.

Each test builds its own broker via ``_broker()``, which records every
emitted event in a plain list rather than wiring a real ``EventLog`` or
``ViewerRegistry``, matching ``session/base.py``'s own description of
``on_event`` as a call-and-return callback with no reference back into the
broker. A request's id is read off the recorded ``PermissionRequest``
event, never assumed from the allocator's own counting scheme, so a test
does not silently start pinning ``"pr_1"`` as part of the contract.

Nothing here sleeps out a real timeout: the timeout path uses a broker
built with a timeout of a few hundredths of a second, so ``asyncio.wait_for``
itself is what elapses, not a test-authored delay racing against it.
"""

import asyncio

import pytest
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny, PermissionUpdate
from claude_agent_sdk.types import PermissionRuleValue

from annealage_mesh.session.base import PermissionRequest, PermissionResolved
from annealage_mesh.session.permissions import (
    DEFAULT_DENY_MESSAGE,
    NEVER_REMEMBERED,
    PermissionBroker,
    UnknownRequest,
)

pytestmark = pytest.mark.asyncio


def _broker(**kwargs):
    """A broker recording every emitted event in ``events``, returned
    alongside it so a test can read a request's id off the event the
    broker itself produced rather than assuming an id format."""
    events = []
    broker = PermissionBroker(events.append, **kwargs)
    return broker, events


async def _pending_request(broker, events, tool_name="Edit", input_data=None):
    """Start ``ask`` as a background task, yield once so it reaches its
    own ``await`` inside ``asyncio.wait_for``, and return the task plus the
    ``PermissionRequest`` it emitted. Requires a viewer already connected
    and the tool not already granted, or ``ask`` would resolve immediately
    and there would be nothing pending to return."""
    before = len(events)
    task = asyncio.ensure_future(broker.ask(tool_name, input_data or {}, None))
    await asyncio.sleep(0)
    assert not task.done()
    assert len(events) == before + 1
    request = events[-1]
    assert isinstance(request, PermissionRequest)
    assert request.tool == tool_name
    return task, request


def _assert_session_rule(result, tool_name):
    """A remembered grant's ``updated_permissions`` is one ``PermissionUpdate``
    that adds a session-scoped allow rule for the whole tool (plan section
    5: per tool, never per input), which is what lets the SDK's own rule
    matcher, not this module's memory, cover the rest of the running
    session."""
    assert result.updated_permissions is not None
    assert len(result.updated_permissions) == 1
    update = result.updated_permissions[0]
    assert isinstance(update, PermissionUpdate)
    assert update.type == "addRules"
    assert update.behavior == "allow"
    assert update.destination == "session"
    assert len(update.rules) == 1
    rule = update.rules[0]
    assert isinstance(rule, PermissionRuleValue)
    assert rule.tool_name == tool_name
    assert rule.rule_content is None


# ---------------------------------------------------------------------------
# decide(): allow, deny
# ---------------------------------------------------------------------------


async def test_decide_allow_resolves_ask_to_an_allow_result():
    broker, events = _broker()
    broker.viewer_connected()
    task, request = await _pending_request(broker, events, tool_name="Write")

    await broker.decide(request.request_id, "allow")
    result = await task

    assert isinstance(result, PermissionResultAllow)
    assert result.updated_permissions is None


async def test_decide_deny_carries_the_given_message_to_the_model():
    broker, events = _broker()
    broker.viewer_connected()
    task, request = await _pending_request(broker, events, tool_name="Write")

    await broker.decide(request.request_id, "deny", "not touching that file")
    result = await task

    assert isinstance(result, PermissionResultDeny)
    assert result.message == "not touching that file"


async def test_decide_deny_with_no_message_falls_back_to_a_default():
    """The pane's deny button may be clicked with the reason field left
    blank; the model must still receive an explanation, per fact 2, rather
    than an empty string reading as a fresh kind of infrastructure error."""
    broker, events = _broker()
    broker.viewer_connected()
    task, request = await _pending_request(broker, events, tool_name="Write")

    await broker.decide(request.request_id, "deny")
    result = await task

    assert isinstance(result, PermissionResultDeny)
    assert result.message == DEFAULT_DENY_MESSAGE
    assert result.message != ""


# ---------------------------------------------------------------------------
# allow_always: persistence and the Bash carve-out
# ---------------------------------------------------------------------------


async def test_allow_always_persists_and_grants_a_later_request_without_asking(tmp_path):
    permissions_path = tmp_path / ".mesh" / "permissions.toml"
    broker, events = _broker(permissions_path=permissions_path)
    broker.viewer_connected()
    task, request = await _pending_request(broker, events, tool_name="Write")

    await broker.decide(request.request_id, "allow_always")
    result = await task

    assert isinstance(result, PermissionResultAllow)
    _assert_session_rule(result, "Write")
    assert permissions_path.exists()
    assert "Write" in permissions_path.read_text(encoding="utf-8")

    # Same broker, same process: a later request for the same tool is
    # granted from the in-memory set with no viewer needed at all, since
    # the granted-tool check in ``ask`` runs before the no-viewer check.
    broker.viewer_disconnected()
    second = await broker.ask("Write", {}, None)
    assert isinstance(second, PermissionResultAllow)
    _assert_session_rule(second, "Write")
    # A grant answered from memory opens no request, so it announces neither a
    # request nor a resolution: the only pair on the wire is the first one.
    assert [type(e).__name__ for e in events] == [
        "PermissionRequest", "PermissionResolved"]

    # A fresh broker over the same file, simulating a process restart,
    # loads the grant from disk and grants it without ever creating a
    # request either.
    reopened, reopened_events = _broker(permissions_path=permissions_path)
    third = await reopened.ask("Write", {}, None)
    assert isinstance(third, PermissionResultAllow)
    _assert_session_rule(third, "Write")
    assert reopened_events == []


async def test_allow_always_for_bash_is_downgraded_and_never_persisted(tmp_path, capsys):
    assert "Bash" in NEVER_REMEMBERED
    permissions_path = tmp_path / ".mesh" / "permissions.toml"
    broker, events = _broker(permissions_path=permissions_path)
    broker.viewer_connected()
    task, request = await _pending_request(broker, events, tool_name="Bash")

    await broker.decide(request.request_id, "allow_always")
    result = await task

    assert isinstance(result, PermissionResultAllow)
    assert result.updated_permissions is None
    assert not permissions_path.exists()
    assert "downgraded" in capsys.readouterr().err

    # Not remembered even in this process: a second Bash call on the same
    # broker still has to go through a full request, proving the earlier
    # click bought exactly one approved call and nothing standing.
    second_task, second_request = await _pending_request(broker, events, tool_name="Bash")
    await broker.decide(second_request.request_id, "deny", "no")
    second_result = await second_task
    assert isinstance(second_result, PermissionResultDeny)
    assert second_result.message == "no"


# ---------------------------------------------------------------------------
# viewer presence
# ---------------------------------------------------------------------------


async def test_no_viewer_connected_denies_without_ever_creating_a_request():
    broker, events = _broker(viewer_url="http://127.0.0.1:8765/?t=abc")
    result = await broker.ask("Write", {}, None)

    assert isinstance(result, PermissionResultDeny)
    assert "http://127.0.0.1:8765/?t=abc" in result.message
    assert events == []
    assert broker.pending_requests() == []


async def test_last_viewer_disconnecting_denies_every_outstanding_request():
    broker, events = _broker()
    broker.viewer_connected()
    broker.viewer_connected()  # a second tab, so the count does not hit zero yet
    task, request = await _pending_request(broker, events, tool_name="Write")

    broker.viewer_disconnected()
    await asyncio.sleep(0)
    assert not task.done()  # one viewer (the second tab) is still connected

    broker.viewer_disconnected()
    result = await task

    assert isinstance(result, PermissionResultDeny)
    assert "disconnected" in result.message


# ---------------------------------------------------------------------------
# timeout
# ---------------------------------------------------------------------------


async def test_timeout_denies_when_no_decision_ever_arrives():
    broker, events = _broker(timeout=0.02)
    broker.viewer_connected()

    result = await broker.ask("Write", {}, None)

    assert isinstance(result, PermissionResultDeny)
    assert "0.02" in result.message
    assert broker.pending_requests() == []


# ---------------------------------------------------------------------------
# shutdown
# ---------------------------------------------------------------------------


async def test_shutdown_denies_a_request_already_in_flight():
    broker, events = _broker()
    broker.viewer_connected()
    task, request = await _pending_request(broker, events, tool_name="Write")

    broker.shutdown()
    result = await task

    assert isinstance(result, PermissionResultDeny)
    assert "shutting down" in result.message


async def test_shutdown_latches_every_later_ask_to_deny_immediately():
    broker, events = _broker()
    broker.viewer_connected()
    broker.shutdown()

    result = await broker.ask("Write", {}, None)

    assert isinstance(result, PermissionResultDeny)
    assert "shutting down" in result.message
    assert events == []  # never got as far as emitting a permission_request

    # Idempotent: a second shutdown with nothing pending changes nothing
    # observable about the next ask() either.
    broker.shutdown()
    result_again = await broker.ask("Write", {}, None)
    assert isinstance(result_again, PermissionResultDeny)


# ---------------------------------------------------------------------------
# decide() on an id that is not answerable
# ---------------------------------------------------------------------------


async def test_decide_on_an_unknown_id_raises():
    broker, _events = _broker()

    with pytest.raises(UnknownRequest):
        await broker.decide("pr_no_such_request", "allow")


async def test_decide_on_an_id_already_answered_raises_and_the_first_answer_stands():
    broker, events = _broker()
    broker.viewer_connected()
    task, request = await _pending_request(broker, events, tool_name="Write")

    await broker.decide(request.request_id, "allow")
    result = await task
    assert isinstance(result, PermissionResultAllow)

    with pytest.raises(UnknownRequest):
        await broker.decide(request.request_id, "deny", "too late")

    # The already-delivered result is untouched by the losing decide().
    assert isinstance(result, PermissionResultAllow)


async def test_two_concurrent_decides_on_one_id_leave_exactly_one_winner():
    """Two tabs holding the same permission card and both clicking is the
    race the module docstring names directly: scheduled as two tasks
    rather than two sequential ``await``s, so the event loop, not test
    ordering, decides who runs first."""
    broker, events = _broker()
    broker.viewer_connected()
    task, request = await _pending_request(broker, events, tool_name="Write")

    first = asyncio.ensure_future(broker.decide(request.request_id, "allow"))
    second = asyncio.ensure_future(broker.decide(request.request_id, "deny", "too late"))
    outcomes = await asyncio.gather(first, second, return_exceptions=True)

    successes = [o for o in outcomes if o is None]
    failures = [o for o in outcomes if isinstance(o, UnknownRequest)]
    assert len(successes) == 1
    assert len(failures) == 1

    result = await task
    assert isinstance(result, PermissionResultAllow)


async def test_replayed_request_is_pending_until_answered_then_not_answerable_twice():
    """``pending_requests()`` is what a reconnecting viewer replays a card
    from (plan section 3.4). Two tabs shown the same replayed card must
    not both be able to answer it: the first ``decide`` wins, and the
    second is told, via ``UnknownRequest``, that it already lost."""
    broker, events = _broker()
    broker.viewer_connected()
    task, request = await _pending_request(broker, events, tool_name="Write")

    assert broker.pending_requests() == [request]

    await broker.decide(request.request_id, "allow")
    await task

    assert broker.pending_requests() == []
    with pytest.raises(UnknownRequest):
        await broker.decide(request.request_id, "allow")


# ---------------------------------------------------------------------------
# on_event failing
# ---------------------------------------------------------------------------


async def test_on_event_raising_denies_and_leaves_nothing_pending(capsys):
    def _broken_on_event(event):
        raise RuntimeError("broadcast exploded")

    broker = PermissionBroker(_broken_on_event)
    broker.viewer_connected()

    result = await broker.ask("Write", {}, None)

    assert isinstance(result, PermissionResultDeny)
    assert broker.pending_requests() == []
    assert "broadcast exploded" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# permission_resolved: every request ends in exactly one announcement
# ---------------------------------------------------------------------------


def _resolutions(events):
    return [e for e in events if isinstance(e, PermissionResolved)]


async def test_a_decision_announces_the_resolution_with_its_outcome():
    """Without this event a browser that did not send the deciding frame has
    no way to learn the card is answered."""
    broker, events = _broker()
    broker.viewer_connected()
    task, request = await _pending_request(broker, events)

    await broker.decide(request.request_id, "allow")
    await task

    assert [(r.request_id, r.outcome) for r in _resolutions(events)] == [
        (request.request_id, "allow")]


@pytest.mark.parametrize("decision", ["allow", "allow_always", "deny"])
async def test_the_outcome_names_the_decision_that_applied(decision):
    """A pane that sent a different decision has to be able to tell that its
    own did not apply, which needs the outcome rather than a bare 'resolved'."""
    broker, events = _broker()
    broker.viewer_connected()
    task, request = await _pending_request(broker, events)

    await broker.decide(request.request_id, decision)
    await task

    assert _resolutions(events)[0].outcome == decision


async def test_a_timeout_announces_itself_as_a_timeout():
    broker, events = _broker(timeout=0.01)
    broker.viewer_connected()
    task, request = await _pending_request(broker, events)

    result = await task

    assert isinstance(result, PermissionResultDeny)
    assert _resolutions(events)[0].outcome == "timeout"


async def test_the_last_viewer_leaving_announces_no_viewer():
    broker, events = _broker()
    broker.viewer_connected()
    task, _request = await _pending_request(broker, events)

    broker.viewer_disconnected()
    await task

    assert _resolutions(events)[0].outcome == "no_viewer"


async def test_shutdown_announces_shutdown():
    broker, events = _broker()
    broker.viewer_connected()
    task, _request = await _pending_request(broker, events)

    broker.shutdown()
    await task

    assert _resolutions(events)[0].outcome == "shutdown"


async def test_exactly_one_resolution_per_request_however_it_ended():
    """The announcement is emitted from the one place every resolution passes
    through, so no path can emit it twice and none can skip it."""
    broker, events = _broker()
    broker.viewer_connected()
    first_task, first = await _pending_request(broker, events, tool_name="Edit")
    second_task, _second = await _pending_request(broker, events, tool_name="Write")

    await broker.decide(first.request_id, "deny")
    await first_task
    broker.shutdown()
    await second_task

    assert len(_resolutions(events)) == 2
    assert len({r.request_id for r in _resolutions(events)}) == 2


async def test_the_resolution_is_announced_after_the_request_stops_being_pending():
    """A viewer that reconnects on the strength of this event and asks what is
    still outstanding must not be told about the request it just closed."""
    seen = []
    broker = PermissionBroker(
        lambda event: seen.append((event, [r.request_id for r in broker.pending_requests()])))
    broker.viewer_connected()
    task = asyncio.ensure_future(broker.ask("Edit", {}, None))
    await asyncio.sleep(0)
    request_id = seen[0][0].request_id

    await broker.decide(request_id, "allow")
    await task

    resolution = [entry for entry in seen if isinstance(entry[0], PermissionResolved)][0]
    assert resolution[1] == []


async def test_a_failure_to_announce_does_not_break_the_decision(capsys):
    """The tool call is already answered by then, so a broadcast failure costs
    a card left on screen until the next reload, not a wrong decision."""
    events = []

    def _on_event(event):
        events.append(event)
        if isinstance(event, PermissionResolved):
            raise RuntimeError("broadcast exploded")

    broker = PermissionBroker(_on_event)
    broker.viewer_connected()
    task = asyncio.ensure_future(broker.ask("Edit", {}, None))
    await asyncio.sleep(0)

    await broker.decide(events[0].request_id, "allow")
    result = await task

    assert isinstance(result, PermissionResultAllow)
    assert broker.pending_requests() == []
    assert "broadcast exploded" in capsys.readouterr().err
