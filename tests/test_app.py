"""Tests for ``app.run`` that need a real bound socket rather than
microdot's in-process ``TestClient``.

Two properties only a real socket can show: that the listening socket is
actually accepting connections by the time ``on_ready`` fires, and that
shutdown on cancellation returns promptly even while a connection is still
open and its handler has not finished.
"""

import asyncio
import contextlib
import socket

import pytest

from annealage_mesh import app as app_module

pytestmark = pytest.mark.asyncio


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def test_run_is_accepting_connections_before_on_ready_fires(tmp_path):
    (tmp_path / "widget.stl").write_bytes(b"solid widget\nendsolid widget\n")
    port = _free_port()
    ready = asyncio.Event()
    probe_done = asyncio.Event()
    probe_result = {}
    # asyncio's event loop holds only a weak reference to a task once it is
    # scheduled; a task with no strong reference anywhere else can be
    # garbage-collected mid-execution (its coroutine closed with
    # GeneratorExit at its current suspension point) at any point the
    # interpreter happens to run a collection. probe_task holds that
    # reference for the test's duration so the probe always runs to
    # completion rather than sometimes being cut short by GC timing.
    probe_task = None

    def on_ready():
        # on_ready runs synchronously inside the server's own coroutine, so
        # a blocking socket.create_connection here would deadlock the loop
        # it is trying to connect to; the connect attempt is scheduled as a
        # separate task and the test awaits its own completion signal
        # (probe_done) rather than guessing how long it needs, since a fixed
        # sleep's margin depends on unrelated load elsewhere in the process
        # (e.g. how much the interpreter has already allocated by the time
        # this runs) and is not a property of this test.
        async def probe():
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.write(b"GET /widget.stl HTTP/1.0\r\n\r\n")
                await writer.drain()
                first_line = await reader.readline()
                probe_result["status_line"] = first_line
                writer.close()
                await writer.wait_closed()
            except OSError as exc:
                probe_result["error"] = exc
            finally:
                probe_done.set()
        nonlocal probe_task
        probe_task = asyncio.ensure_future(probe())
        ready.set()

    task = asyncio.ensure_future(
        app_module.run(tmp_path, "127.0.0.1", port, on_ready=on_ready))
    try:
        await asyncio.wait_for(ready.wait(), timeout=2.0)
        await asyncio.wait_for(probe_done.wait(), timeout=2.0)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2.0)
        if probe_task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait_for(probe_task, timeout=2.0)

    assert "error" not in probe_result
    assert probe_result.get("status_line", b"").startswith(b"HTTP/1.0 200")

    # Nothing is left listening on the port after shutdown.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", port))


async def test_run_returns_promptly_when_cancelled_with_a_connection_still_open(
        tmp_path, monkeypatch):
    # Bounds the shutdown drain wait tightly so the test itself stays fast;
    # the property under test is that the bound is honoured, not its value.
    monkeypatch.setattr(app_module, "SHUTDOWN_DRAIN_TIMEOUT", 0.1)
    (tmp_path / "widget.stl").write_bytes(b"solid widget\nendsolid widget\n")
    port = _free_port()
    ready = asyncio.Event()

    task = asyncio.ensure_future(
        app_module.run(tmp_path, "127.0.0.1", port, on_ready=ready.set))
    await asyncio.wait_for(ready.wait(), timeout=2.0)

    # A connection that never finishes sending its request headers: the
    # server's handler for it is genuinely in flight and will not complete
    # on its own, which is exactly the situation a slow model transfer or a
    # stalled client leaves behind.
    _, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b"GET /widget.stl HTTP/1.0\r\n")
    await writer.drain()

    loop = asyncio.get_running_loop()
    start = loop.time()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2.0)
    elapsed = loop.time() - start
    assert elapsed < 1.0, "shutdown did not honour SHUTDOWN_DRAIN_TIMEOUT"

    writer.close()
    with contextlib.suppress(OSError):
        await writer.wait_closed()

    # The listening socket itself is gone, even though the dangling
    # connection's handler was still in flight when shutdown returned and
    # its own socket may briefly still hold the port. SO_REUSEADDR is set
    # here for the same reason cli.port_in_use sets it: a new listener on
    # this port must not be refused just because one abandoned, non-listening
    # connection from the previous server has not yet been reaped.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", port))


# ---------------------------------------------------------------------------
# The event log reaches disk, so a session can be resumed and priced
# ---------------------------------------------------------------------------


async def test_agent_mode_writes_the_conversation_to_the_sessions_event_log(tmp_path):
    """The 500-event ring covers a browser reconnecting; only the file covers
    the process exiting. `-c` resumes from it and `-r` prices a session from it,
    so a log with nowhere to go leaves both reading an empty history and
    reporting every session as 0 turns and $0.00.
    """
    from annealage_mesh import sessions
    from annealage_mesh.session.base import TurnEnd
    from annealage_mesh.session.fake import FakeSession

    sid = sessions.create_session(tmp_path)
    built = []

    def build_session(on_event):
        session = FakeSession(on_event, session_id=sid)
        built.append(session)
        return session

    app_module.create_app(tmp_path, token="tok", mesh_session_id=sid,
                          build_session=build_session)
    built[0].emit(TurnEnd(turn=1, stop_reason="end_turn", cost_usd=0.0125))
    built[0].emit(TurnEnd(turn=2, stop_reason="end_turn", cost_usd=0.0075))

    assert sessions.events_path(tmp_path, sid).is_file()

    info = sessions.get_session_info(tmp_path, sid)
    assert info.turn_count == 2
    assert info.cost_usd == pytest.approx(0.02)


async def test_viewer_only_mode_writes_no_event_log(tmp_path):
    """There is no session and no conversation, so there is nothing to persist
    and nothing to create a session directory for."""
    from annealage_mesh import sessions

    app_module.create_app(tmp_path, token="tok")

    assert not sessions.sessions_dir(tmp_path).exists()
