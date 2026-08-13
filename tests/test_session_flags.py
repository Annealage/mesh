"""Tests for the ``-c``/``--continue`` and ``-r``/``--resume`` session-control
flags on ``cli.main`` (plan section 3.4).

``annealage_mesh.app.run`` is replaced in every test that reaches past flag
resolution, so a passing resolution never goes on to build a real
``SdkSession`` (which would import ``claude_agent_sdk`` and, once started,
try to spawn the ``claude`` binary) or bind a real listening port. Only the
resolution logic in ``cli.main`` and ``sessions.py`` is under test here;
every assertion is against ``cli.main``'s return code and the text it wrote,
which is what a person running the command actually sees, not against
whether some internal helper was called.
"""

import json

import pytest

from annealage_mesh import cli, sessions


@pytest.fixture(autouse=True)
def sandbox_requirement_satisfied(monkeypatch):
    """Tell the requirement check that this platform can sandbox.

    Autouse for this whole file, because every test here is about what the
    session flags mean, not about what the host has installed. Agent mode
    refuses to start when bubblewrap and socat are absent, which is correct and
    is asserted directly further down; without this fixture that refusal would
    pre-empt every flag test on any machine lacking them, which includes a
    stock CI runner, and the file would pass or fail according to what happened
    to be installed rather than according to the code under test.
    """
    from annealage_mesh.session import sdk

    monkeypatch.setattr(sdk, "missing_sandbox_dependencies", lambda: ())


def _make_stub_run(calls):
    async def _stub_run(
        serve_dir,
        host,
        port,
        on_ready=None,
        token=None,
        extra_origins=(),
        build_session=None,
        mesh_session_id=None,
        settings=None,
    ):
        calls.append({"mesh_session_id": mesh_session_id, "port": port})
        if on_ready is not None:
            await on_ready()

    return _stub_run


def _install_stub_run(monkeypatch):
    calls = []
    monkeypatch.setattr(cli.app_module, "run", _make_stub_run(calls))
    return calls


def _write_session(serve_dir, sid, started_at, first_user_text=None, turn_events=()):
    """Build one ``.mesh/sessions/<sid>/`` directly, with a caller-chosen
    ``started_at``, so ordering between several sessions in one test is
    pinned by an explicit value rather than by how fast ``new_session_id``
    and real wall-clock timestamps happen to separate three back-to-back
    calls within the same test.
    """
    directory = sessions.session_dir(serve_dir, sid)
    directory.mkdir(parents=True)
    meta = {
        "session_id": sid,
        "sdk_session_id": None,
        "started_at": started_at,
        "project_key": sessions.project_key_for_directory(serve_dir),
        "first_user_text": first_user_text,
    }
    sessions.meta_path(serve_dir, sid).write_text(json.dumps(meta), encoding="utf-8")
    if turn_events:
        with open(sessions.events_path(serve_dir, sid), "w", encoding="utf-8") as f:
            for cost in turn_events:
                f.write(json.dumps({"event": {"kind": "turn_end", "cost_usd": cost}}) + "\n")


def _argv(serve_dir, *flags):
    return list(flags) + ["--no-open", "--port", "0", str(serve_dir)]


# --------------------------------------------------------------------------
# -c / --continue
# --------------------------------------------------------------------------


def test_continue_with_no_prior_session_exits_1_naming_the_directory(tmp_path, capsys):
    rc = cli.main(_argv(tmp_path, "-c"))

    assert rc == 1
    err = capsys.readouterr().err
    assert "no prior session" in err
    assert str(tmp_path.resolve()) in err
    # No lock left behind by a run that never got as far as acquiring one.
    assert not (tmp_path / ".mesh" / "lock").exists()


def test_continue_picks_the_most_recently_started_of_several(tmp_path, monkeypatch, capsys):
    _write_session(tmp_path, "sid-old", "2026-08-01T00:00:00Z")
    _write_session(tmp_path, "sid-mid", "2026-08-05T00:00:00Z")
    _write_session(tmp_path, "sid-new", "2026-08-10T00:00:00Z")
    calls = _install_stub_run(monkeypatch)

    rc = cli.main(_argv(tmp_path, "-c"))

    assert rc == 0
    assert calls == [{"mesh_session_id": "sid-new", "port": 0}]
    out = capsys.readouterr().out
    assert "sid-new" in out
    assert "(resumed)" in out
    assert "sid-old" not in out
    assert "sid-mid" not in out


# --------------------------------------------------------------------------
# -r / --resume SID
# --------------------------------------------------------------------------


def test_resume_unknown_id_exits_1_naming_id_and_directory(tmp_path, capsys):
    rc = cli.main(_argv(tmp_path, "-r", "no-such-session"))

    assert rc == 1
    err = capsys.readouterr().err
    assert "no-such-session" in err
    assert str(tmp_path.resolve()) in err


def test_resume_known_id_resumes_it(tmp_path, monkeypatch, capsys):
    _write_session(tmp_path, "sid-target", "2026-08-01T00:00:00Z")
    calls = _install_stub_run(monkeypatch)

    rc = cli.main(_argv(tmp_path, "-r", "sid-target"))

    assert rc == 0
    assert calls == [{"mesh_session_id": "sid-target", "port": 0}]
    out = capsys.readouterr().out
    assert "sid-target" in out
    assert "(resumed)" in out


def test_resume_unknown_id_never_reaches_app_run(tmp_path, monkeypatch):
    """An unresolved ``-r SID`` must fail before anything is built: no
    server started, no lock taken."""
    calls = _install_stub_run(monkeypatch)

    rc = cli.main(_argv(tmp_path, "-r", "no-such-session"))

    assert rc == 1
    assert calls == []
    assert not (tmp_path / ".mesh" / "lock").exists()


# --------------------------------------------------------------------------
# bare -r: list and exit, never resume, never start anything
# --------------------------------------------------------------------------


def test_bare_resume_lists_sessions_and_exits_0(tmp_path, monkeypatch, capsys):
    _write_session(
        tmp_path,
        "sid-a",
        "2026-08-01T00:00:00Z",
        first_user_text="please check this bracket",
        turn_events=(0.5, 1.25),
    )
    _write_session(tmp_path, "sid-b", "2026-08-02T00:00:00Z")
    calls = _install_stub_run(monkeypatch)

    # dir precedes -r so the directory is not swallowed as -r's own SID
    # argument (argparse's nargs='?' claims the very next token
    # unconditionally); this is the ordering the parser's own epilog
    # recommends for exactly this reason.
    rc = cli.main([str(tmp_path), "-r"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "sid-a" in out
    assert "sid-b" in out
    assert "2 turn" in out
    assert "$1.75" in out
    assert "please check this bracket" in out
    # Bare -r lists; it starts nothing and resumes nothing.
    assert calls == []
    assert not (tmp_path / ".mesh" / "lock").exists()


def test_bare_resume_with_no_sessions_says_so_and_exits_0(tmp_path, capsys):
    rc = cli.main([str(tmp_path), "-r"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "no sessions recorded" in out


# --------------------------------------------------------------------------
# mutual exclusion
# --------------------------------------------------------------------------


def test_continue_and_resume_together_is_rejected_by_the_parser(tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(_argv(tmp_path, "-c", "-r", "some-sid"))

    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "not allowed" in err


# --------------------------------------------------------------------------
# --no-agent conflicts with either flag
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "flags",
    [
        ("--no-agent", "-c"),
        ("--no-agent", "-r", "some-sid"),
        ("--no-agent", "-r"),  # bare -r must also be caught, not fall through
        # to the list-and-exit-0 branch that handles it
        # when --no-agent is absent.
    ],
    ids=["continue", "resume-sid", "resume-bare"],
)
def test_no_agent_with_either_session_flag_exits_2(tmp_path, flags, capsys):
    rc = cli.main(_argv(tmp_path, *flags))

    assert rc == 2
    err = capsys.readouterr().err
    # Names the flag that was rejected and the mode that rejected it, since
    # "invalid combination" would leave the reader to work out which of the
    # two flags they typed was the problem.
    assert "--no-agent" in err
    assert "has none" in err


# --------------------------------------------------------------------------
# The sandbox requirement itself, which the fixture above suppresses for every
# other test in this file.
# --------------------------------------------------------------------------


def test_agent_mode_refuses_to_start_without_the_sandbox_dependencies(
    tmp_path, monkeypatch, capsys
):
    from annealage_mesh.session import sdk

    monkeypatch.setattr(sdk, "missing_sandbox_dependencies", lambda: ("bwrap", "socat"))

    rc = cli.main([str(tmp_path), "--no-open"])

    assert rc == 2
    err = capsys.readouterr().err
    # Names what is missing and what to install, since a refusal that does not
    # is a dead end for whoever hits it.
    assert "bwrap" in err and "socat" in err
    assert "apt install bubblewrap socat" in err
    # And names the way to get a viewer without installing anything, so the
    # refusal is not a dead end for someone who only wants to look at a model.
    assert "annealage-mesh view" in err
    # Nothing was created for a run that never started.
    assert not (tmp_path / ".mesh").exists()


def test_viewer_only_mode_starts_without_the_sandbox_dependencies(tmp_path, monkeypatch):
    """Viewer-only runs no agent, so it carries no sandbox requirement at all.

    Asserted by getting past the check to the point where app.run is invoked,
    which the stub below records instead of serving.
    """
    from annealage_mesh.session import sdk

    monkeypatch.setattr(sdk, "missing_sandbox_dependencies", lambda: ("bwrap", "socat"))
    calls = _install_stub_run(monkeypatch)

    rc = cli.main([str(tmp_path), "--no-agent", "--no-open"])

    assert rc == 0
    assert len(calls) == 1
    assert not (tmp_path / ".mesh" / "lock").exists()
