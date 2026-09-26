"""Tests for how Mesh's CLI uses ``.mesh/lock``: the exclusivity guarantee
that stops a second agent-mode instance from ever running against one project
directory.

The lock itself (``annealage_agent/lock.py``: exclusive creation, stale-pid
reclaim, corrupt records, the creation race) is tested in the
``annealage-agent`` package's own suite. These tests drive the actual
``cli.main`` entry point so the assertion lands on what a human running a
second instance actually sees (the exit code, the message, whether a server
started) rather than on an internal call having returned the right exception
type.
"""

import json
import os

import pytest
from annealage_agent import lock, net

from annealage_mesh import cli


# ``cli.main`` is only exercised for the lock's externally observable
# behaviour (exit code, stderr, whether a lock file is left behind).
# ``annealage_mesh.app.run`` is replaced for every such test so a passing
# session-flag resolution never goes on to build a real ``SdkSession`` or
# bind a real listening port; ``session/sdk.py`` and the ``claude`` binary
# are never touched by this file.
def _make_stub_run(calls):
    async def _stub_run(
        serve_dir,
        host,
        port,
        on_ready=None,
        token=None,
        agent_token=None,
        login=None,
        extra_origins=(),
        build_session=None,
        mesh_session_id=None,
        settings=None,
    ):
        calls.append(
            {
                "serve_dir": serve_dir,
                "host": host,
                "port": port,
                "mesh_session_id": mesh_session_id,
            }
        )
        if on_ready is not None:
            await on_ready()

    return _stub_run


def _lock_argv(serve_dir):
    return ["--no-open", "--port", "0", str(serve_dir)]


@pytest.fixture(autouse=True)
def sandbox_requirement_satisfied(monkeypatch):
    """Tell the requirement check that this platform can sandbox.

    This file is about the lock, not about what the host has installed, and
    agent mode refuses to start without bubblewrap and socat. Without this the
    one test that drives a real agent-mode start would be refused before it ever
    reached the lock, on any machine lacking them, a stock CI runner included.
    """
    from annealage_agent.session import sdk

    monkeypatch.setattr(sdk, "missing_sandbox_dependencies", lambda: ())


# --------------------------------------------------------------------------
# cli.main, observable behaviour
# --------------------------------------------------------------------------


def test_second_instance_is_refused_with_exit_3_and_running_address(tmp_path, capsys):
    """A live lock in this project makes ``cli.main`` exit 3 and print the
    address of the instance that is already running, without starting a
    second server. The address carries no token, since the lock holds none,
    and the message says where the link that does is: the running instance's
    own banner. Pre-seeding the lock with this test process's own pid stands
    in for a genuinely separate running instance: the pid is real and live for
    exactly the same reason ``os.kill(pid, 0)`` would report it live if it
    belonged to a second process."""
    mesh_dir = tmp_path / ".mesh"
    mesh_dir.mkdir()
    lock.lock_path(mesh_dir).write_bytes(json.dumps({"pid": os.getpid(), "port": 9001}).encode())

    rc = cli.main(_lock_argv(tmp_path))

    assert rc == 3
    err = capsys.readouterr().err
    assert "already running" in err
    assert "it is serving: %s\n" % net.server_url(net.resolve_bind(None), 9001) in err
    assert "#t=" not in err
    assert "startup banner" in err
    # The refused start must not have clobbered the running instance's record.
    assert json.loads(lock.lock_path(mesh_dir).read_bytes()) == {"pid": os.getpid(), "port": 9001}


def test_an_agent_mode_run_writes_neither_token_to_its_lock(tmp_path, monkeypatch):
    """What the running instance leaves in the served directory, read while it
    runs: pid and port only. The browser token (given here by flag so the test
    knows it) and the agent token are both absent, because the agent's shell
    can read this file."""
    seen = {}

    async def _stub_run(serve_dir, host, port, on_ready=None, **kwargs):
        seen["record"] = lock.lock_path(tmp_path / ".mesh").read_text(encoding="utf-8")
        seen["agent_token"] = kwargs["agent_token"]

    monkeypatch.setattr(cli.app_module, "run", _stub_run)
    rc = cli.main(_lock_argv(tmp_path) + ["--no-git", "--token", "browser-secret"])

    assert rc == 0
    record = json.loads(seen["record"])
    assert (record["pid"], record["port"]) == (os.getpid(), 0)
    # Beside pid and port only the boot id and start time the agent package
    # adds where /proc has them: nothing a token could be.
    assert set(record) <= {"pid", "port", "boot_id", "start_time"}
    assert "browser-secret" not in seen["record"]
    assert seen["agent_token"] not in seen["record"]


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
