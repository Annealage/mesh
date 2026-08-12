"""Tests for the callouts watcher: the push that replaces the browser's poll.

Every test drives ``CalloutsWatcher.tick`` directly with the time it wants to
pretend it is, rather than starting ``run`` and sleeping. Nothing here waits on
a real clock, so the deferral rule is asserted rather than approximated, and
the suite stays fast.

The watcher's change signal is a digest of the bytes it read, not the file's
size and modification time. Two of the tests below only mean anything because
of that choice: a rewrite that preserves both size and mtime is indetectable
to a stat-based watcher, and a stat-based readiness rule cannot tell a
finished write from a stalled one.
"""

import asyncio
import json

import pytest

from annealage_mesh import paths
from annealage_mesh.app import CalloutsWatcher
from annealage_mesh.session.events import EventLog

pytestmark = pytest.mark.asyncio


class _RecordingRegistry:
    """Records broadcast frames instead of writing them to a socket."""

    def __init__(self):
        self.frames = []

    async def broadcast(self, frame):
        self.frames.append(frame)


def _watcher(serve_dir, max_defer=5.0):
    registry = _RecordingRegistry()
    log = EventLog()
    return CalloutsWatcher(serve_dir, registry, log, max_defer=max_defer), registry


async def _primed_watcher(tmp_path, max_defer=5.0):
    """A watcher that has already taken its priming sample of ``tmp_path``.

    The first tick records what it finds and announces nothing, because the
    page's own fetch on load already covers the state the watcher starts in.
    Every test about change detection therefore has to get past that first
    tick before it means anything, and doing it here keeps each test's own
    timeline starting at 0.0.
    """
    watcher, registry = _watcher(tmp_path, max_defer=max_defer)
    assert await watcher.tick(-1.0) is False, "the priming tick must not announce"
    return watcher, registry


def _write_callouts(serve_dir, annotations):
    path = serve_dir / paths.CALLOUTS_JSON_NAME
    path.write_text(json.dumps({"annotations": annotations}))
    return path


async def test_no_callouts_file_broadcasts_nothing(tmp_path):
    watcher, registry = _watcher(tmp_path)
    assert await watcher.tick(0.0) is False
    assert registry.frames == []


async def test_a_new_callouts_file_broadcasts_once(tmp_path):
    watcher, registry = _watcher(tmp_path)
    await watcher.tick(0.0)  # absent, nothing to say
    _write_callouts(tmp_path, [{"id": 1, "point": [0, 0, 0], "comment": "thin"}])

    assert await watcher.tick(1.0) is True
    assert len(registry.frames) == 1
    frame = registry.frames[0]
    assert frame["type"] == "event"
    assert frame["event"]["kind"] == "callouts_changed"
    # The event names no content: the browser refetches /callouts for itself,
    # so the page keeps exactly one writer of that state.
    assert "annotations" not in json.dumps(frame)


async def test_unchanged_content_does_not_broadcast_again(tmp_path):
    watcher, registry = await _primed_watcher(tmp_path)
    _write_callouts(tmp_path, [{"id": 1}])
    assert await watcher.tick(0.0) is True

    for t in (1.0, 2.0, 3.0):
        assert await watcher.tick(t) is False
    assert len(registry.frames) == 1


async def test_rewriting_identical_bytes_does_not_broadcast(tmp_path):
    watcher, registry = await _primed_watcher(tmp_path)
    _write_callouts(tmp_path, [{"id": 1}])
    await watcher.tick(0.0)

    _write_callouts(tmp_path, [{"id": 1}])  # same content, new mtime
    assert await watcher.tick(1.0) is False, (
        "a rewrite with identical content is not a change the browser needs to "
        "hear about; watching mtime rather than content would push here")
    assert len(registry.frames) == 1


async def test_a_same_length_edit_is_still_detected(tmp_path):
    # The digest is what makes this work. A watcher keyed on (size, mtime)
    # can miss this entirely when the filesystem's timestamp granularity is
    # coarser than the gap between the two writes, and the two files here are
    # byte-for-byte the same length by construction.
    watcher, registry = await _primed_watcher(tmp_path)
    path = tmp_path / paths.CALLOUTS_JSON_NAME
    path.write_text('{"annotations": [{"id": 1}]}')
    await watcher.tick(0.0)
    path.write_text('{"annotations": [{"id": 2}]}')
    assert len(path.read_text()) == 28

    assert await watcher.tick(1.0) is True
    assert len(registry.frames) == 2


async def test_deleting_the_file_is_itself_a_change(tmp_path):
    watcher, registry = await _primed_watcher(tmp_path)
    _write_callouts(tmp_path, [{"id": 1}])
    await watcher.tick(0.0)

    (tmp_path / paths.CALLOUTS_JSON_NAME).unlink()
    assert await watcher.tick(1.0) is True, (
        "a deleted callouts file leaves the viewer showing pins that are gone")
    assert len(registry.frames) == 2


async def test_a_half_written_file_is_not_announced_until_it_parses(tmp_path):
    # The readiness test is whether the bytes parse, not whether a stat
    # signature held still: an incomplete write can be sampled twice with the
    # same size and mtime, so stability proves nothing about completeness.
    watcher, registry = await _primed_watcher(tmp_path)
    path = tmp_path / paths.CALLOUTS_JSON_NAME
    path.write_text('{"annotations": [{"id": 1, "comm')

    assert await watcher.tick(0.0) is False, "changed but does not parse yet"
    assert await watcher.tick(0.1) is False, "still the same unparseable bytes"
    assert registry.frames == []

    path.write_text('{"annotations": [{"id": 1, "comment": "thin"}]}')
    assert await watcher.tick(0.2) is True
    assert len(registry.frames) == 1


async def test_a_file_that_never_parses_is_announced_once_past_max_defer(tmp_path):
    # Without this bound, a file being rewritten continuously never looks
    # settled and the watcher never fires at all, which is worse than pushing
    # an event the browser may find unparseable: the browser tolerates that
    # and the next change produces another event.
    watcher, registry = await _primed_watcher(tmp_path, max_defer=1.0)
    path = tmp_path / paths.CALLOUTS_JSON_NAME

    path.write_text("{not json 1")
    assert await watcher.tick(10.0) is False
    path.write_text("{not json 22")
    assert await watcher.tick(10.5) is False
    path.write_text("{not json 333")
    assert await watcher.tick(11.5) is True, (
        "a file that keeps changing without ever parsing must still be "
        "announced once the deferral bound has passed")
    assert len(registry.frames) == 1


async def test_deferral_clock_starts_at_the_first_unparseable_sample(tmp_path):
    watcher, _registry = await _primed_watcher(tmp_path, max_defer=2.0)
    path = tmp_path / paths.CALLOUTS_JSON_NAME
    path.write_text("{partial")

    assert await watcher.tick(100.0) is False
    assert await watcher.tick(101.9) is False, "still inside the deferral window"
    assert await watcher.tick(102.0) is True, "the window has now elapsed"


async def test_the_deferral_clock_resets_once_a_change_settles(tmp_path):
    watcher, registry = await _primed_watcher(tmp_path, max_defer=2.0)
    path = tmp_path / paths.CALLOUTS_JSON_NAME

    path.write_text("{partial")
    assert await watcher.tick(0.0) is False
    path.write_text('{"annotations": []}')
    assert await watcher.tick(0.5) is True

    # A later unparseable write must get its own full window, not inherit the
    # elapsed time from the earlier one.
    path.write_text("{partial again")
    assert await watcher.tick(1.0) is False
    assert await watcher.tick(2.5) is False, (
        "the second deferral window started at 1.0, so 2.5 is still inside it")
    assert await watcher.tick(3.0) is True
    assert len(registry.frames) == 2


async def test_a_symlinked_callouts_file_is_refused_and_never_announced(tmp_path):
    # Same rule the /callouts route applies: the name is fixed but its
    # directory entry is not, and a symlink left there by a reviewed bundle
    # would otherwise have its target read and announced.
    secret = tmp_path.parent / "secret-callouts.json"
    secret.write_text('{"annotations": [{"comment": "TOPSECRET"}]}')
    (tmp_path / paths.CALLOUTS_JSON_NAME).symlink_to(secret)

    watcher, registry = _watcher(tmp_path)
    assert await watcher.tick(0.0) is False
    assert registry.frames == []


async def test_seq_comes_from_the_shared_event_log(tmp_path):
    # The seq a client resyncs from has to come from the one monotonic stream,
    # or a reconnecting browser replaying from last_seq would skip this event
    # or replay it twice.
    registry = _RecordingRegistry()
    log = EventLog()
    watcher = CalloutsWatcher(tmp_path, registry, log)
    assert await watcher.tick(-1.0) is False

    _write_callouts(tmp_path, [{"id": 1}])
    await watcher.tick(0.0)
    _write_callouts(tmp_path, [{"id": 2}])
    await watcher.tick(1.0)

    seqs = [frame["seq"] for frame in registry.frames]
    assert seqs == [1, 2]
    assert log.current_seq == 2


async def test_run_polls_on_its_interval_and_can_be_cancelled(tmp_path):
    # The one test that does use the real loop clock, because it is the only
    # way to show that `run` actually calls `tick` and that cancelling it does
    # not leave the task wedged. The interval is tiny and the assertion is
    # "at least one", so it cannot become a timing-sensitive failure.
    registry = _RecordingRegistry()
    watcher = CalloutsWatcher(tmp_path, registry, EventLog(), interval=0.01)

    task = asyncio.ensure_future(watcher.run())
    # Written after run() has started, so the change happens on a tick this
    # task actually takes rather than being folded into its priming sample.
    await asyncio.sleep(0.05)
    _write_callouts(tmp_path, [{"id": 1}])
    for _ in range(50):
        await asyncio.sleep(0.01)
        if registry.frames:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(registry.frames) >= 1
