"""Mesh's CLI choosing an agent backend when no setting names one, and
``--save-default``.

How a backend is detected and chosen is the agent layer's
(``annealage_agent.backends``), tested in annealage-agent's own suite. What
stays here is Mesh's command line: its exit status and message when it cannot
choose, ``--save-default`` writing the user file a later run reads, the prompt
reached through a real terminal, and ``view`` refusing the flag.
"""

import pytest
from annealage_agent import backends, settings

from annealage_mesh import cli


def _answers(*replies):
    replies = list(replies)

    def ask(_prompt):
        if not replies:
            raise EOFError
        return replies.pop(0)

    return ask


@pytest.fixture
def served(monkeypatch, tmp_path):
    """Agent mode up to ``app.run``, which records the settings it was given."""
    from annealage_agent.session import sdk

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
