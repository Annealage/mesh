"""The callouts push, as Mesh runs it: the agent layer's review watcher over
Mesh's review store (``review.py``), publishing ``review_changed``.

The watcher's state machine (priming, the digest, the parse-readiness rule and
its deferral bound) is the agent layer's and is tested there. What is Mesh's
is what the watcher samples: the two published files, in the served
directory, written by writers Mesh does not control. So these tests write
``mesh-callouts.json`` directly, the way a separately running agent does under
the published contract, and drive ``tick`` with the time they want to pretend
it is, rather than starting ``run`` and sleeping.
"""

import asyncio
import json

import pytest
from annealage_agent import sessions
from annealage_agent.review import ReviewWatcher
from annealage_agent.session.fake import FakeSession
from conftest import TEST_HOST

from annealage_mesh import paths
from annealage_mesh.app import DEFAULT_PORT, create_app
from annealage_mesh.review import MeshFilesStore

pytestmark = pytest.mark.asyncio


def _watcher(serve_dir, max_defer=5.0):
    events = []
    return ReviewWatcher(MeshFilesStore(serve_dir), events.append, max_defer=max_defer), events


async def _primed(tmp_path, max_defer=5.0):
    watcher, events = _watcher(tmp_path, max_defer=max_defer)
    assert await watcher.tick(-1.0) is False, "the priming tick must not announce"
    return watcher, events


def _write_callouts(serve_dir, annotations):
    path = serve_dir / paths.CALLOUTS_JSON_NAME
    path.write_text(json.dumps({"annotations": annotations}))
    return path


async def test_no_comment_files_announce_nothing(tmp_path):
    watcher, events = _watcher(tmp_path)
    assert await watcher.tick(0.0) is False
    assert await watcher.tick(1.0) is False
    assert events == []


async def test_a_callouts_file_written_directly_is_announced_once(tmp_path):
    watcher, events = _watcher(tmp_path)
    await watcher.tick(0.0)  # absent, nothing to say
    _write_callouts(tmp_path, [{"id": 1, "point": [0, 0, 0], "comment": "thin"}])

    assert await watcher.tick(1.0) is True
    # The event names no content: the browser refetches /callouts for itself,
    # so the page keeps exactly one writer of that state.
    assert [event.to_wire() for event in events] == [{"kind": "review_changed"}]
    assert await watcher.tick(2.0) is False


async def test_a_half_written_callouts_file_is_not_announced_until_it_parses(tmp_path):
    # The published skill does not require an agent to write this file
    # atomically, so a change can be observed mid-write; whether the bytes
    # parse is what says the write finished.
    watcher, events = await _primed(tmp_path)
    path = tmp_path / paths.CALLOUTS_JSON_NAME
    path.write_text('{"annotations": [{"id": 1, "comm')
    assert await watcher.tick(0.0) is False
    assert await watcher.tick(0.1) is False
    path.write_text('{"annotations": [{"id": 1, "comment": "thin"}]}')
    assert await watcher.tick(0.2) is True
    assert len(events) == 1


async def test_a_submit_is_announced_as_well(tmp_path):
    """The human's Submit changes the review as much as a callout does. The
    viewer's own reaction (a /callouts refetch that finds nothing new) is a
    no-op; a page that shows submitted comments needs it."""
    watcher, events = await _primed(tmp_path)
    (tmp_path / paths.COMMENTS_JSON_NAME).write_text(
        json.dumps({"submitted_at": "t", "count": 0, "annotations": []})
    )
    assert await watcher.tick(0.0) is True
    assert len(events) == 1


async def test_a_comments_file_that_does_not_parse_does_not_hold_back_a_callout(tmp_path):
    """Mesh tolerates a mesh-comments.json that is not JSON (it reads as
    nothing submitted), so it must not make every callout change look
    half-written: the callout is announced on the first tick, as the
    callouts-only watcher before the shared model announced it."""
    (tmp_path / paths.COMMENTS_JSON_NAME).write_text("{ half")
    watcher, events = await _primed(tmp_path, max_defer=5.0)
    _write_callouts(tmp_path, [{"id": 1, "point": [0, 0, 0], "comment": "here"}])
    assert await watcher.tick(0.25) is True
    assert len(events) == 1


async def test_a_symlinked_callouts_file_is_refused_and_never_announced(tmp_path):
    # Same rule the /callouts route applies: the name is fixed but its
    # directory entry is not, and a symlink left there by a reviewed bundle
    # would otherwise have its target read and announced.
    secret = tmp_path.parent / ("secret-%s.json" % tmp_path.name)
    secret.write_text('{"annotations": [{"comment": "TOPSECRET"}]}')
    (tmp_path / paths.CALLOUTS_JSON_NAME).symlink_to(secret)

    watcher, events = _watcher(tmp_path)
    assert await watcher.tick(0.0) is False
    secret.write_text('{"annotations": [{"comment": "CHANGED"}]}')
    assert await watcher.tick(1.0) is False
    assert events == []


# --- as the app runs it ---------------------------------------------------------


def _kinds(app):
    return [wire["kind"] for _seq, wire in app.agent_event_log.replay(0).events]


async def _wait_for_push(app, count=1):
    for _ in range(300):
        if _kinds(app).count("review_changed") >= count:
            break
        await asyncio.sleep(0.01)
    return _kinds(app)


async def test_the_app_pushes_a_direct_write_through_its_own_event_log(tmp_path):
    """The seq a reconnecting page resyncs from comes from the one log, so
    the push has to be appended to it, which is what the app's watcher does."""
    app = create_app(tmp_path, host=TEST_HOST, port=DEFAULT_PORT)
    app.agent_review_watcher._interval = 0.02
    task = asyncio.ensure_future(app.agent_review_watcher.run())
    try:
        await asyncio.sleep(0.05)
        _write_callouts(tmp_path, [{"id": 1, "point": [0, 0, 0], "comment": "external"}])
        assert await _wait_for_push(app) == ["review_changed"]
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_a_callout_from_the_tool_is_pushed_without_waiting_for_a_sample(tmp_path):
    """The tool server is built over the app's own store, so its write wakes
    the watcher directly; the sampling interval here is longer than the test."""
    app = create_app(
        tmp_path,
        host=TEST_HOST,
        port=DEFAULT_PORT,
        token="browser-token",
        agent_token="agent-token",
        mesh_session_id=sessions.create_session(tmp_path),
        build_session=lambda on_event, *, bus: FakeSession(on_event),
    )
    app.agent_review_watcher._interval = 60.0
    task = asyncio.ensure_future(app.agent_review_watcher.run())
    try:
        await asyncio.sleep(0.05)
        handler = {t.name: t.handler for t in app.agent_tools.tools}["add_callout"]
        result = await handler({"point": [1, 2, 3], "comment": "here"})
        assert "is_error" not in result
        assert await _wait_for_push(app) == ["review_changed"]
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
