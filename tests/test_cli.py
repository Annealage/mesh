"""Tests for the command surface: subcommand dispatch, the three-layer
settings the flags feed, and the two commands that start no server.

``app.run`` is stubbed wherever a test gets far enough to reach it, so no test
here binds a port or builds a real ``SdkSession``. What is under test is what a
person typing the command sees: the return code and the text written.

Three behaviours here exist because of failure modes that are invisible from
inside a single invocation. A subcommand name is recognised only as an exact
first argument, so a directory called ``doctor`` stays servable. ``CLAUDECODE``
flips the bare form to viewer-only, which is what stops a skill running this
tool inside Claude Code from starting an agent within an agent; this file sets
that variable itself, while ``tests/conftest.py`` clears it everywhere else.
And ``init`` and ``doctor`` must take no lock and bind no port, because both
are things people run against a directory that already has a server on it.
"""

import pytest

from annealage_mesh import cli, lock, sessions, settings

# Synchronous tests, unlike most of this suite: ``cli.main`` calls
# ``asyncio.run`` itself, and that raises when it is entered from inside a loop
# that a ``pytest.mark.asyncio`` test would already be running in.


@pytest.fixture(autouse=True)
def sandbox_requirement_satisfied(monkeypatch):
    """Agent mode refuses to start without bubblewrap and socat, which is
    asserted in ``tests/test_session_flags.py``; every test here is about the
    command surface instead, so the requirement is satisfied for all of them
    rather than each one passing or failing on what the host has installed."""
    from annealage_mesh.session import sdk

    monkeypatch.setattr(sdk, "missing_sandbox_dependencies", lambda: ())


@pytest.fixture
def stub_run(monkeypatch):
    """Replace ``app.run`` and record the keywords each invocation reached it
    with, so a test can assert the resolved port, bind and settings without a
    socket."""
    calls = []

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
        calls.append(
            {
                "serve_dir": serve_dir,
                "host": host,
                "port": port,
                "mesh_session_id": mesh_session_id,
                "settings": settings,
            }
        )
        if on_ready is not None:
            await on_ready()

    monkeypatch.setattr(cli.app_module, "run", _stub_run)
    return calls


@pytest.fixture
def no_git(monkeypatch):
    """Make every scaffold in this file skip git.

    Agent mode scaffolds, and a real ``git init`` in a temporary directory
    would make these tests depend on the developer's git configuration,
    including whether a commit can be signed. What git does is tested against
    an injected ``run`` in ``tests/test_project.py``.
    """
    calls = []
    real = cli.project_module.ensure_project

    def _ensure(project_dir, *, git=True, force=False, **kwargs):
        calls.append({"project_dir": project_dir, "git": git, "force": force})
        return real(project_dir, git=False, force=force)

    monkeypatch.setattr(cli.project_module, "ensure_project", _ensure)
    return calls


# --- subcommand dispatch -----------------------------------------------------


def test_view_subcommand_runs_the_viewer_with_no_lock(tmp_path, stub_run):
    rc = cli.main(["view", str(tmp_path), "--no-open", "--port", "0"])

    assert rc == 0
    assert len(stub_run) == 1
    assert stub_run[0]["mesh_session_id"] is None
    assert not lock.lock_path(sessions.mesh_dir(tmp_path)).exists()


def test_view_subcommand_scaffolds_nothing(tmp_path, stub_run):
    """Viewer-only mode is for looking at a folder of models, which may not be
    a mesh project at all and must not be turned into one by being looked at."""
    cli.main(["view", str(tmp_path), "--no-open", "--port", "0"])

    assert not (tmp_path / "models").exists()
    assert not (tmp_path / "CLAUDE.md").exists()
    assert not (tmp_path / ".gitignore").exists()


def test_a_directory_named_like_a_subcommand_is_still_servable(tmp_path, stub_run, no_git):
    """``view`` names the subcommand, ``./view`` names the directory. Anything
    else would make a folder of models unusable because of what it is called."""
    directory = tmp_path / "view"
    directory.mkdir()

    rc = cli.main([str(directory), "--no-open", "--port", "0"])

    assert rc == 0
    assert stub_run[0]["serve_dir"] == directory
    # Agent mode, not the view subcommand: a session was resolved.
    assert stub_run[0]["mesh_session_id"] is not None


def test_subcommand_is_only_recognised_as_the_first_argument(tmp_path, stub_run, no_git):
    """A later ``view`` is not a subcommand, so it is read as the directory
    positional and fails as one rather than silently changing the mode."""
    rc = cli.main(["--no-open", "view"])

    assert rc == 2


# --- CLAUDECODE: no agent inside an agent ------------------------------------


def test_bare_invocation_inside_claude_code_is_viewer_only(tmp_path, stub_run, monkeypatch, capsys):
    monkeypatch.setenv("CLAUDECODE", "1")

    rc = cli.main([str(tmp_path), "--no-open", "--port", "0"])

    assert rc == 0
    assert stub_run[0]["mesh_session_id"] is None
    assert not lock.lock_path(sessions.mesh_dir(tmp_path)).exists()
    out = capsys.readouterr().out
    # Says so rather than silently doing something else than what was typed.
    assert "CLAUDECODE" in out
    assert "view" in out


def test_agent_only_flags_are_tolerated_when_claude_code_forced_the_mode(
    tmp_path, stub_run, monkeypatch
):
    """A flag combination that is an error when viewer-only was *asked* for is
    not an error when the environment chose viewer-only on the caller's behalf:
    the caller asked for an agent and got told why they did not get one, and
    refusing their flags as well would make the fallback useless."""
    monkeypatch.setenv("CLAUDECODE", "1")

    rc = cli.main([str(tmp_path), "--no-open", "--port", "0", "--model", "some-model"])

    assert rc == 0
    assert stub_run[0]["mesh_session_id"] is None


def test_view_subcommand_refuses_agent_only_flags(tmp_path, stub_run):
    """Asking for the viewer and passing agent configuration is a contradiction
    worth reporting, since silently ignoring --model would look like the flag
    had been applied."""
    rc = cli.main(["view", str(tmp_path), "--no-open", "--model", "some-model"])

    assert rc == 2
    assert stub_run == []


# --- settings: the three layers, seen through the CLI ------------------------


def test_port_comes_from_user_settings_when_no_flag_is_given(tmp_path, stub_run, no_git):
    settings.apply(tmp_path, {"port": 9123})

    cli.main([str(tmp_path), "--no-open"])

    assert stub_run[0]["port"] == 9123


def test_a_port_flag_outranks_user_settings(tmp_path, stub_run, no_git):
    settings.apply(tmp_path, {"port": 9123})

    cli.main([str(tmp_path), "--no-open", "--port", "9456"])

    assert stub_run[0]["port"] == 9456


def test_project_config_outranks_user_settings(tmp_path, stub_run, no_git):
    settings.user_settings_path().parent.mkdir(parents=True, exist_ok=True)
    settings.user_settings_path().write_text("port = 9123\n", encoding="utf-8")
    config = settings.project_config_path(tmp_path)
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text('model = "from-the-project"\n', encoding="utf-8")

    cli.main([str(tmp_path), "--no-open"])

    resolved = stub_run[0]["settings"]
    assert resolved["port"] == 9123
    assert resolved["model"] == "from-the-project"
    assert resolved.provenance("model") == settings.PROJECT
    assert resolved.provenance("port") == settings.USER


def test_open_browser_setting_is_honoured_without_the_flag(tmp_path, stub_run, no_git, monkeypatch):
    """``--no-open`` is a flag with no positive counterpart, so the only way to
    ask for a browser is to leave it out; a saved false must still suppress it."""
    opened = []
    monkeypatch.setattr(cli.webbrowser, "open", lambda url: opened.append(url))
    settings.apply(tmp_path, {"open_browser": False})

    cli.main([str(tmp_path), "--port", "0"])

    assert opened == []


def test_a_malformed_config_file_is_a_clean_startup_error(tmp_path, stub_run):
    config = settings.project_config_path(tmp_path)
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("port = \n", encoding="utf-8")

    rc = cli.main([str(tmp_path), "--no-open"])

    assert rc == 2
    assert stub_run == []


def test_settings_flag_prints_provenance_and_starts_nothing(tmp_path, stub_run, capsys):
    settings.apply(tmp_path, {"port": 9123})

    rc = cli.main([str(tmp_path), "--settings"])

    assert rc == 0
    assert stub_run == []
    out = capsys.readouterr().out
    assert "9123" in out
    # Names the file, because provenance is only actionable once you know which
    # file to go and edit.
    assert str(settings.user_settings_path()) in out
    assert "built-in default" in out
    assert not lock.lock_path(sessions.mesh_dir(tmp_path)).exists()


def test_settings_flag_reports_a_flag_as_this_run_only(tmp_path, stub_run, capsys):
    cli.main([str(tmp_path), "--settings", "--port", "9456"])

    out = capsys.readouterr().out
    assert "9456" in out
    assert "this run only" in out


# --- init --------------------------------------------------------------------


def test_init_scaffolds_and_starts_no_server(tmp_path, stub_run, capsys):
    rc = cli.main(["init", str(tmp_path), "--no-git"])

    assert rc == 0
    assert stub_run == []
    assert (tmp_path / "models").is_dir()
    assert (tmp_path / "images").is_dir()
    assert (tmp_path / "CLAUDE.md").is_file()
    assert (tmp_path / ".gitignore").is_file()
    assert not lock.lock_path(sessions.mesh_dir(tmp_path)).exists()
    assert "created" in capsys.readouterr().out


def test_init_is_idempotent_and_says_so(tmp_path, capsys):
    cli.main(["init", str(tmp_path), "--no-git"])
    capsys.readouterr()

    rc = cli.main(["init", str(tmp_path), "--no-git"])

    assert rc == 0
    assert "already set up" in capsys.readouterr().out


def test_init_never_clobbers_a_hand_written_claude_md(tmp_path):
    written = "# my own instructions\n"
    (tmp_path / "CLAUDE.md").write_text(written, encoding="utf-8")

    cli.main(["init", str(tmp_path), "--no-git"])

    assert (tmp_path / "CLAUDE.md").read_text(encoding="utf-8") == written


def test_init_force_regenerates_the_generated_files(tmp_path, capsys):
    cli.main(["init", str(tmp_path), "--no-git"])
    (tmp_path / ".gitignore").write_text("# edited\n", encoding="utf-8")
    capsys.readouterr()

    rc = cli.main(["init", str(tmp_path), "--no-git", "--force"])

    assert rc == 0
    assert ".mesh/" in (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert "regenerated" in capsys.readouterr().out


def test_force_is_rejected_outside_init(tmp_path, capsys):
    """``--force`` means one specific thing, rewriting generated files, and it
    is only meaningful where scaffolding is the whole command."""
    with pytest.raises(SystemExit) as exit_info:
        cli.main([str(tmp_path), "--force", "--no-open"])

    assert exit_info.value.code == 2
    assert "--force" in capsys.readouterr().err


def test_init_on_a_missing_directory_exits_2(tmp_path, capsys):
    rc = cli.main(["init", str(tmp_path / "nope")])

    assert rc == 2
    assert "does not exist" in capsys.readouterr().err


# --- doctor ------------------------------------------------------------------


def test_doctor_reports_and_starts_nothing(tmp_path, stub_run, capsys):
    rc = cli.main(["doctor", str(tmp_path)])

    assert rc == 0
    assert stub_run == []
    out = capsys.readouterr().out
    assert "python" in out
    assert "claude CLI" in out
    assert "sandbox" in out
    assert str(tmp_path) in out
    assert not lock.lock_path(sessions.mesh_dir(tmp_path)).exists()


def test_doctor_names_the_settings_files_and_whether_they_exist(tmp_path, capsys):
    rc = cli.main(["doctor", str(tmp_path)])

    assert rc == 0
    out = capsys.readouterr().out
    assert str(settings.user_settings_path()) in out
    assert "not written yet" in out


def test_doctor_reports_a_held_lock_rather_than_being_blocked_by_it(tmp_path, capsys):
    """Doctor is what someone runs *because* something is already running, so a
    live lock is a fact to report, never a reason to refuse."""
    held = lock.acquire(sessions.mesh_dir(tmp_path), 8765, "tok")
    try:
        rc = cli.main(["doctor", str(tmp_path)])
    finally:
        held.release()

    assert rc == 0
    out = capsys.readouterr().out
    assert "8765" in out
    assert "held by pid" in out


def test_doctor_scaffolds_nothing(tmp_path):
    cli.main(["doctor", str(tmp_path)])

    assert not (tmp_path / "models").exists()
    assert not (tmp_path / "CLAUDE.md").exists()


# --- agent mode scaffolds, and reports what it did ---------------------------


def test_agent_mode_scaffolds_the_project(tmp_path, stub_run, no_git, capsys):
    rc = cli.main([str(tmp_path), "--no-open", "--port", "0"])

    assert rc == 0
    assert (tmp_path / "models").is_dir()
    assert (tmp_path / "CLAUDE.md").is_file()
    assert "created" in capsys.readouterr().out


def test_agent_mode_scaffold_happens_while_the_lock_is_held(tmp_path, stub_run, monkeypatch):
    """Two starts against one project must not both be writing these files, so
    the scaffold runs after the lock rather than before it. Asserted through
    ``ensure_project``'s call order relative to the lock file existing."""
    seen = []
    real = cli.project_module.ensure_project

    def _watching(project_dir, *, git=True, force=False, **kwargs):
        seen.append(lock.lock_path(sessions.mesh_dir(project_dir)).exists())
        return real(project_dir, git=False)

    monkeypatch.setattr(cli.project_module, "ensure_project", _watching)
    cli.main([str(tmp_path), "--no-open", "--port", "0"])

    assert seen == [True]


def test_no_git_flag_reaches_the_scaffold(tmp_path, stub_run, no_git):
    cli.main([str(tmp_path), "--no-open", "--port", "0", "--no-git"])

    assert no_git[0]["git"] is False
