"""Choosing an agent backend when no setting names one, and ``--save-default``.

No backend is a default. What is installed decides, a person with several is
asked, and nobody at the terminal means an error rather than a guess.
"""

import pytest

from annealage_mesh import backends, cli, settings
from annealage_mesh.backends import detect as real_detect


def _answers(*replies):
    replies = list(replies)

    def ask(_prompt):
        if not replies:
            raise EOFError
        return replies.pop(0)

    return ask


def test_detect_needs_the_cli_on_path_and_its_python_client():
    on_path = {"claude", "omp"}
    importable = {"claude_agent_sdk", "openai_codex"}
    found = real_detect(
        which=lambda name: "/bin/" + name if name in on_path else None,
        find_spec=lambda module: object() if module in importable else None,
    )
    # codex has its client but no CLI; omp has its CLI but no client.
    assert found == ("claude",)


def test_no_backend_installed_is_an_error_naming_all_three():
    with pytest.raises(backends.NoBackend) as exc:
        backends.choose((), interactive=True, ask=_answers())
    assert all(name in str(exc.value) for name in ("claude", "codex", "omp"))


def test_one_backend_installed_is_used_without_asking():
    assert backends.choose(("omp",), interactive=True, ask=_answers()) == ("omp", False)


def test_several_installed_with_nobody_at_the_terminal_refuses_to_guess():
    with pytest.raises(backends.NoBackend) as exc:
        backends.choose(("claude", "codex"), interactive=False)
    assert "--backend" in str(exc.value) and "--save-default" in str(exc.value)


def test_several_installed_asks_and_reasks_until_the_answer_is_valid():
    ask = _answers("9", "banana", "2", "")
    choice = backends.choose(
        ("claude", "codex", "omp"), interactive=True, ask=ask, write=lambda _: None
    )
    assert choice == ("codex", False)


def test_the_answer_can_be_a_name_and_can_be_saved():
    ask = _answers("omp", "y")
    choice = backends.choose(("claude", "omp"), interactive=True, ask=ask, write=lambda _: None)
    assert choice == ("omp", True)


def test_a_config_file_still_saying_local_is_refused(tmp_path):
    path = settings.project_config_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text('backend = "local"\n')
    with pytest.raises(settings.SettingsError) as exc:
        settings.resolve(tmp_path)
    assert "'omp'" in str(exc.value)


@pytest.fixture
def served(monkeypatch, tmp_path):
    """Agent mode up to ``app.run``, which records the settings it was given."""
    from annealage_mesh.session import sdk

    monkeypatch.setattr(sdk, "missing_sandbox_dependencies", lambda: ())
    runs = []

    async def _stub_run(serve_dir, host, port, **kwargs):
        runs.append(kwargs["settings"])

    monkeypatch.setattr(cli.app_module, "run", _stub_run)
    return runs


def test_cli_with_several_installed_and_no_terminal_exits_2(monkeypatch, tmp_path, served, capsys):
    monkeypatch.setattr(backends, "detect", lambda **_kw: ("claude", "codex"))
    rc = cli.main([str(tmp_path), "--no-open", "--no-git", "--port", "0"])
    assert rc == 2
    assert served == []
    assert "claude, codex" in capsys.readouterr().err


def test_cli_save_default_writes_the_user_file_and_later_runs_use_it(monkeypatch, tmp_path, served):
    monkeypatch.setattr(backends, "detect", lambda **_kw: ("claude", "codex", "omp"))
    argv = [str(tmp_path), "--no-open", "--no-git", "--port", "0"]

    assert cli.main(argv + ["--backend", "codex", "--save-default"]) == 0
    assert settings.resolve(None)["backend"] == "codex"
    assert settings.resolve(None).provenance("backend") == settings.USER

    # No flag now, several installed, no terminal: the saved default decides.
    assert cli.main(argv) == 0
    assert served[-1]["backend"] == "codex"
    assert served[-1].provenance("backend") == settings.USER


def test_cli_choice_made_at_the_prompt_is_used_and_saved_when_asked(monkeypatch, tmp_path, served):
    monkeypatch.setattr(backends, "detect", lambda **_kw: ("claude", "omp"))
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", _answers("2", "y"))

    assert cli.main([str(tmp_path), "--no-open", "--no-git", "--port", "0"]) == 0
    assert served[-1]["backend"] == "omp"
    assert settings.resolve(None)["backend"] == "omp"


def test_save_default_is_refused_in_viewer_mode(tmp_path, served, capsys):
    rc = cli.main(["view", str(tmp_path), "--no-open", "--backend", "omp", "--save-default"])
    assert rc == 2
    assert "--save-default" in capsys.readouterr().err
