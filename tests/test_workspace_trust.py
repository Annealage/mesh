"""Tests for Mesh's CLI enforcing the workspace-trust gate.

The property under test is that Claude configuration in the served directory
cannot take effect without a human having accepted its exact content, as seen
by someone running ``annealage-mesh``: agent mode refuses such a directory
(exit 2, naming the files and both ways forward), ``--trust-project-config``
records acceptance and starts, and viewer-only mode needs no decision. What
the digest distinguishes, the trust record and the refusal text are tested
in the ``annealage-agent`` package's own suite.
"""

import pytest
from annealage_agent.session import workspace_trust as wt

from annealage_mesh import cli


@pytest.fixture(autouse=True)
def sandbox_requirement_satisfied(monkeypatch):
    """Agent mode refuses to start without the sandbox binaries; this file is
    about the trust gate, which sits behind that refusal, so the requirement is
    reported satisfied rather than depending on what the host has installed."""
    from annealage_agent.session import sdk

    monkeypatch.setattr(sdk, "missing_sandbox_dependencies", lambda: ())


def _settings(root, name="settings.json", body='{"permissions":{"allow":["Read"]}}'):
    (root / ".claude").mkdir(exist_ok=True)
    (root / ".claude" / name).write_text(body)


# -- the gate, through the CLI ---------------------------------------------


def _run_cli(monkeypatch, args):
    """Run ``cli.main`` with the server stubbed out, returning its exit code."""
    started = []

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
        started.append(serve_dir)

    monkeypatch.setattr(cli.app_module, "run", _stub_run)
    return cli.main(args), started


def test_agent_mode_refuses_a_directory_whose_config_was_never_accepted(
    tmp_path, monkeypatch, capsys
):
    _settings(tmp_path)
    code, started = _run_cli(monkeypatch, [str(tmp_path), "--no-open"])
    assert code == 2
    assert started == []
    err = capsys.readouterr().err
    assert "settings.json" in err
    assert "--trust-project-config" in err
    assert "annealage-mesh view" in err


def test_the_refusal_names_every_guarded_file_present(tmp_path, monkeypatch, capsys):
    _settings(tmp_path, "settings.json")
    _settings(tmp_path, "settings.local.json")
    (tmp_path / ".mcp.json").write_text("{}")
    _run_cli(monkeypatch, [str(tmp_path), "--no-open"])
    err = capsys.readouterr().err
    assert "settings.json" in err and "settings.local.json" in err
    assert ".mcp.json" in err


def test_viewer_only_mode_needs_no_trust_decision(tmp_path, monkeypatch):
    """No agent CLI starts, so nothing in the directory is read as configuration."""
    _settings(tmp_path)
    code, started = _run_cli(monkeypatch, [str(tmp_path), "--no-open", "--no-agent"])
    assert code == 0
    assert started == [tmp_path]


def test_a_plain_directory_starts_with_no_prompting(tmp_path, monkeypatch):
    (tmp_path / "widget.stl").write_bytes(b"solid widget\nendsolid widget\n")
    code, started = _run_cli(monkeypatch, [str(tmp_path), "--no-open"])
    assert code == 0
    assert started == [tmp_path]


def test_accepting_records_it_and_starts(tmp_path, monkeypatch):
    _settings(tmp_path)
    code, started = _run_cli(monkeypatch, [str(tmp_path), "--no-open", "--trust-project-config"])
    assert code == 0
    assert started == [tmp_path]
    assert wt.TrustStore().accepted(tmp_path, wt.config_digest(tmp_path))


def test_an_accepted_directory_starts_again_without_the_flag(tmp_path, monkeypatch):
    _settings(tmp_path)
    _run_cli(monkeypatch, [str(tmp_path), "--no-open", "--trust-project-config"])
    code, started = _run_cli(monkeypatch, [str(tmp_path), "--no-open"])
    assert code == 0
    assert started == [tmp_path]


def test_editing_an_accepted_config_asks_again(tmp_path, monkeypatch, capsys):
    """The agent writing a settings file, or a pulled update changing one, both
    land here: acceptance is against content, not against the path."""
    _settings(tmp_path)
    _run_cli(monkeypatch, [str(tmp_path), "--no-open", "--trust-project-config"])
    _settings(tmp_path, body='{"permissions":{"allow":["Bash"]}}')
    code, started = _run_cli(monkeypatch, [str(tmp_path), "--no-open"])
    assert code == 2
    assert started == []
    assert "--trust-project-config" in capsys.readouterr().err


def test_a_settings_file_added_after_acceptance_asks_again(tmp_path, monkeypatch):
    _settings(tmp_path, "settings.json")
    _run_cli(monkeypatch, [str(tmp_path), "--no-open", "--trust-project-config"])
    _settings(tmp_path, "settings.local.json")
    code, _ = _run_cli(monkeypatch, [str(tmp_path), "--no-open"])
    assert code == 2
