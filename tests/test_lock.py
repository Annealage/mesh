"""Tests for ``.mesh/lock``: the exclusivity guarantee that stops a second
agent-mode instance from ever running against one project directory.

The pure ``lock.acquire``/``Lock.release`` tests exercise the module against
a real filesystem, since the whole point of ``O_CREAT | O_EXCL`` is a kernel
guarantee that no amount of mocking can stand in for. The two
``cli.main``-level tests drive the actual CLI entry point so the assertion
lands on what a human running a second instance actually sees (the exit
code, the message, whether a server started) rather than on an internal
call having returned the right exception type.
"""

import errno
import json
import os
import stat
import threading

import pytest

from annealage_mesh import cli, lock, net


# ``cli.main`` is only exercised for the lock's externally observable
# behaviour (exit code, stderr, whether a lock file is left behind).
# ``annealage_mesh.app.run`` is replaced for every such test so a passing
# session-flag resolution never goes on to build a real ``SdkSession`` or
# bind a real listening port; ``session/sdk.py`` and the ``claude`` binary
# are never touched by this file.
def _make_stub_run(calls):
    async def _stub_run(serve_dir, host, port, on_ready=None, token=None,
                         extra_origins=(), build_session=None,
                         mesh_session_id=None):
        calls.append({
            "serve_dir": serve_dir, "host": host, "port": port,
            "mesh_session_id": mesh_session_id,
        })
        if on_ready is not None:
            await on_ready()
    return _stub_run


def _lock_argv(serve_dir):
    return ["--no-open", "--port", "0", str(serve_dir)]


# --------------------------------------------------------------------------
# lock.py, direct
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def sandbox_requirement_satisfied(monkeypatch):
    """Tell the requirement check that this platform can sandbox.

    This file is about the lock, not about what the host has installed, and
    agent mode refuses to start without bubblewrap and socat. Without this the
    one test that drives a real agent-mode start would be refused before it ever
    reached the lock, on any machine lacking them, a stock CI runner included.
    """
    from annealage_mesh.session import sdk
    monkeypatch.setattr(sdk, "missing_sandbox_dependencies", lambda: ())


def test_exclusive_creation_writes_pid_port_token(tmp_path):
    """A fresh ``acquire`` creates the file exactly once, with the caller's
    pid, port and token, mode 0600, and ``release`` removes it again."""
    mesh_dir = tmp_path / ".mesh"
    held = lock.acquire(mesh_dir, 4242, "tok-abc")

    path = lock.lock_path(mesh_dir)
    assert path.exists()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    record = json.loads(path.read_bytes())
    assert record == {"pid": os.getpid(), "port": 4242, "token": "tok-abc"}

    held.release()
    assert not path.exists()
    # Idempotent: a second release (a shutdown path racing an explicit one)
    # must not raise on either the already-closed fd or the already-gone file.
    held.release()


def test_live_pid_refused_raises_lock_held_with_holder_details(tmp_path):
    """A lock file naming a pid this process can see raises ``LockHeld``
    carrying the holder's own pid/port/token, never silently reclaimed."""
    mesh_dir = tmp_path / ".mesh"
    mesh_dir.mkdir()
    # This test process's own pid is guaranteed live for the test's duration,
    # so no mocking of os.kill is needed to pin this path deterministically.
    lock.lock_path(mesh_dir).write_bytes(
        json.dumps({"pid": os.getpid(), "port": 9001, "token": "held-tok"}).encode())

    with pytest.raises(lock.LockHeld) as exc:
        lock.acquire(mesh_dir, 4242, "new-tok")
    assert exc.value.pid == os.getpid()
    assert exc.value.port == 9001
    assert exc.value.token == "held-tok"
    # Refused, not reclaimed: the file on disk is still the live holder's.
    assert json.loads(lock.lock_path(mesh_dir).read_bytes())["token"] == "held-tok"


def test_stale_pid_is_reclaimed(tmp_path, monkeypatch, capsys):
    """A lock file naming a pid that no longer exists is unlinked and the
    create retried, so the caller gets back a ``Lock`` rather than a refusal.

    ``os.kill`` is monkeypatched for one specific fake pid rather than
    finding a real dead process, so the "which pid is actually dead right
    now" question never depends on process-table timing; every other pid
    (this test's own, and anything else on the machine) still goes through
    the real syscall.
    """
    dead_pid = 999999

    real_kill = os.kill

    def fake_kill(pid, sig):
        if pid == dead_pid:
            raise OSError(errno.ESRCH, "No such process")
        return real_kill(pid, sig)

    monkeypatch.setattr(lock.os, "kill", fake_kill)

    mesh_dir = tmp_path / ".mesh"
    mesh_dir.mkdir()
    path = lock.lock_path(mesh_dir)
    path.write_bytes(json.dumps({"pid": dead_pid, "port": 1, "token": "stale"}).encode())

    held = lock.acquire(mesh_dir, 4242, "fresh-tok")
    try:
        record = json.loads(path.read_bytes())
        assert record == {"pid": os.getpid(), "port": 4242, "token": "fresh-tok"}
        assert "reclaiming stale lock" in capsys.readouterr().err
    finally:
        held.release()


def test_garbage_lock_file_raises_lock_corrupt_and_is_left_in_place(tmp_path):
    """A lock file that exists but does not parse as a pid/port/token record
    is reported, never guessed at: reclaiming an unreadable file on the
    assumption it must be stale is the exact silent-corruption failure the
    lock exists to prevent."""
    mesh_dir = tmp_path / ".mesh"
    mesh_dir.mkdir()
    path = lock.lock_path(mesh_dir)
    path.write_bytes(b"not json at all")

    with pytest.raises(lock.LockCorrupt):
        lock.acquire(mesh_dir, 4242, "tok")
    # Left exactly as it was: no reclaim on a guess.
    assert path.read_bytes() == b"not json at all"


def test_garbage_lock_file_missing_keys_also_raises_lock_corrupt(tmp_path):
    """Valid JSON that is missing a required field is corrupt the same way
    unparseable bytes are: a partial record proves nothing about liveness
    either."""
    mesh_dir = tmp_path / ".mesh"
    mesh_dir.mkdir()
    lock.lock_path(mesh_dir).write_bytes(json.dumps({"pid": 1}).encode())

    with pytest.raises(lock.LockCorrupt):
        lock.acquire(mesh_dir, 4242, "tok")


def test_unwritable_mesh_dir_raises_instead_of_silently_succeeding(tmp_path):
    """A ``.mesh`` directory that exists but this user cannot write into
    cannot hold a new lock file; ``acquire`` must surface that as an error
    rather than report success for a lock it never actually created."""
    mesh_dir = tmp_path / ".mesh"
    mesh_dir.mkdir()
    mesh_dir.chmod(0o500)
    try:
        with pytest.raises(OSError):
            lock.acquire(mesh_dir, 4242, "tok")
        assert not lock.lock_path(mesh_dir).exists()
    finally:
        # Restored so pytest's own tmp_path cleanup (which needs to remove
        # this directory) is not the thing that fails instead of the test.
        mesh_dir.chmod(0o700)


def test_two_threads_racing_one_create_exactly_one_wins(tmp_path):
    """Two callers hitting ``acquire`` at the same instant, simulating two
    processes starting against the same project at once: the kernel's
    ``O_CREAT | O_EXCL`` arbitrates a single winner, and the loser sees that
    winner's record as a live holder, never a corrupt or a doubly-created
    file.

    Real threads, not a mocked interleaving, because the property under
    test is the kernel's own atomicity guarantee on the ``open()`` call, not
    anything this project's code arbitrates itself.
    """
    mesh_dir = tmp_path / ".mesh"
    barrier = threading.Barrier(2)
    results = [None, None]

    def attempt(i, port, token):
        barrier.wait()
        try:
            results[i] = ("ok", lock.acquire(mesh_dir, port, token))
        except lock.LockHeld as exc:
            results[i] = ("held", exc)
        except lock.LockCorrupt as exc:
            results[i] = ("corrupt", exc)

    t1 = threading.Thread(target=attempt, args=(0, 1111, "tok-1"))
    t2 = threading.Thread(target=attempt, args=(1, 2222, "tok-2"))
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)

    outcomes = [r[0] for r in results]
    assert outcomes.count("ok") == 1, "exactly one racer must win the create: got %r" % outcomes
    assert outcomes.count("held") == 1, "the loser must see the winner as a live holder: got %r" % outcomes

    winner = next(r[1] for r in results if r[0] == "ok")
    loser_exc = next(r[1] for r in results if r[0] == "held")
    # The loser's LockHeld must describe the winner's own record, not a
    # third, phantom holder.
    winner_record = json.loads(lock.lock_path(mesh_dir).read_bytes())
    assert loser_exc.pid == winner_record["pid"] == os.getpid()
    assert loser_exc.port == winner_record["port"]
    assert loser_exc.token == winner_record["token"]
    winner.release()


# --------------------------------------------------------------------------
# cli.main, observable behaviour
# --------------------------------------------------------------------------

def test_second_instance_is_refused_with_exit_3_and_running_url(tmp_path, capsys):
    """A live lock in this project makes ``cli.main`` exit 3 and print the
    URL of the instance that is already running, without starting a second
    server. Pre-seeding the lock with this test process's own pid stands in
    for a genuinely separate running instance: the pid is real and live for
    exactly the same reason ``os.kill(pid, 0)`` would report it live if it
    belonged to a second process."""
    mesh_dir = tmp_path / ".mesh"
    mesh_dir.mkdir()
    lock.lock_path(mesh_dir).write_bytes(
        json.dumps({"pid": os.getpid(), "port": 9001, "token": "running-tok"}).encode())

    rc = cli.main(_lock_argv(tmp_path))

    assert rc == 3
    err = capsys.readouterr().err
    assert "already running" in err
    expected_url = net.viewer_url(net.resolve_bind(None), 9001, "running-tok")
    assert expected_url in err
    # The refused start must not have clobbered the running instance's record.
    assert json.loads(lock.lock_path(mesh_dir).read_bytes())["token"] == "running-tok"


def test_viewer_only_mode_never_locks_and_a_second_one_also_starts(tmp_path, monkeypatch):
    """``--no-agent`` acquires no lock, so a lock left behind by nothing (no
    prior agent-mode run at all) is not what stops a second viewer-only
    invocation; two of them must both be able to start against the same
    directory, which is the documented, supported case."""
    calls = []
    monkeypatch.setattr(cli.app_module, "run", _make_stub_run(calls))

    argv = ["--no-agent", "--no-open", "--port", "0", str(tmp_path)]
    rc1 = cli.main(argv)
    rc2 = cli.main(argv)

    assert rc1 == 0
    assert rc2 == 0
    assert len(calls) == 2
    assert not lock.lock_path(tmp_path / ".mesh").exists()
