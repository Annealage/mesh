"""Tests for Mesh's ``doctor`` command against the agent layer's diagnostics
collector (``annealage_agent.diagnostics``).

The collector itself, and ``GET /settings`` shipping what it collects, are
tested in annealage-agent's own suite. What stays here is Mesh's report: the
lines ``doctor`` prints from those facts, and that ``doctor`` and Mesh's
``GET /settings`` resolve the same project's settings into the same facts.

The real environment this suite runs in has an actual bundled ``claude``
binary (``claude-agent-sdk`` is a base dependency), so every test here
monkeypatches ``diagnostics._bundled_claude_path`` to ``None``, keeping the
report independent of what the machine running the suite has installed.
"""

import json
import os

import pytest
from annealage_agent import diagnostics, settings
from conftest import TEST_HOST, make_test_client

from annealage_mesh import cli
from annealage_mesh.app import DEFAULT_PORT, create_app


def test_doctor_command_reports_misconfigured_local_endpoint(monkeypatch, tmp_path, capsys):
    """The doctor report's local-endpoint line distinguishes a real
    misconfiguration from the healthy "no omp_base_url, using omp's own
    providers" state -- a human reading `doctor` output must see this
    before agent startup fails on it, not after."""
    monkeypatch.setattr(diagnostics, "_bundled_claude_path", lambda: None)
    config_path = tmp_path / ".mesh" / "config.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text('backend = "omp"\nomp_api_key = "sk-example"\n')
    assert cli.doctor_command([str(tmp_path)]) == 0
    report = capsys.readouterr().out.splitlines()
    assert any(
        line.startswith("  omp endpoint     : MISCONFIGURED (") and "omp_api_key" in line
        for line in report
    )


# --- doctor's stdout and GET /settings's diagnostics JSON must agree ------


TOKEN = "the-real-diagnostics-token-Value_123"


def _make_executable(path, banner):
    """A real, directly spawnable script printing ``banner`` for any
    arguments and exiting 0, so ``_tool_version`` gets a real subprocess
    result rather than a stubbed ``run``."""
    path.write_text("#!/bin/sh\necho '%s'\n" % banner)
    path.chmod(0o755)


@pytest.mark.asyncio
async def test_doctor_report_and_settings_payload_agree_for_backend_codex(
    monkeypatch, tmp_path, capsys
):
    """``doctor_command`` and ``GET /settings`` each do their own settings
    resolution and their own call into ``diagnostics.collect``; a regression
    that dropped ``backend``/``omp_base_url`` from either real resolution
    path, or that stopped the route from shipping ``diagnostics`` at all,
    must fail here. Driven through the real CLI entry point and a real
    request via microdot's ``TestClient``
    (``tests/test_settings_routes.py``'s own ``make_client`` pattern), never
    by calling ``collect`` twice by hand and comparing hand-built dicts."""
    monkeypatch.setattr(diagnostics, "_bundled_claude_path", lambda: None)
    import codex_cli_bin

    settings.apply(tmp_path, {"backend": "codex"})

    bundled_path = tmp_path / "fake-codex"
    _make_executable(bundled_path, "0.21.0")
    monkeypatch.setattr(codex_cli_bin, "bundled_codex_path", lambda: bundled_path)

    # The real `annealage-mesh doctor` invocation: parses argv, resolves this
    # project's settings itself (cli.py:564-566), and prints the report.
    assert cli.doctor_command([str(tmp_path)]) == 0
    report = capsys.readouterr().out.splitlines()

    # The real GET /settings route, same project, same fresh settings
    # resolution (routes_settings.py:69-70, 81-89).
    client = make_test_client(create_app(tmp_path, token=TOKEN, host=TEST_HOST, port=DEFAULT_PORT))
    res = await client.get("/settings?t=%s" % TOKEN)
    settings_body = json.loads(res.body.decode("utf-8"))

    expected_codex_cli = {"path": str(bundled_path), "version": "0.21.0", "source": "bundled"}
    assert settings_body["diagnostics"]["codex_cli"] == expected_codex_cli

    expected_line = "  codex CLI        : 0.21.0  (%s, bundled with the SDK)" % str(bundled_path)
    assert expected_line in report


@pytest.mark.asyncio
async def test_doctor_report_and_settings_payload_agree_for_backend_omp(
    monkeypatch, tmp_path, capsys
):
    """Same parity claim as the codex test, for the omp backend's richer
    ``omp_cli`` shape (binary, python client and endpoint reachability all
    reported together), again through the real ``doctor_command`` and a real
    ``GET /settings`` request rather than two hand-built ``collect`` calls."""
    monkeypatch.setattr(diagnostics, "_bundled_claude_path", lambda: None)
    monkeypatch.setattr(diagnostics.importlib.util, "find_spec", lambda name: object())

    settings.apply(tmp_path, {"backend": "omp"})

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    omp_script = bin_dir / "omp"
    _make_executable(omp_script, "omp 0.9.0")
    # `_omp_info` receives `which`/`run` as `collect`'s own unstubbed
    # defaults (`shutil.which`/`subprocess.run`) from both real call sites,
    # so PATH, not a monkeypatched function, is the seam that reaches them.
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))

    assert cli.doctor_command([str(tmp_path)]) == 0
    report = capsys.readouterr().out.splitlines()

    client = make_test_client(create_app(tmp_path, token=TOKEN, host=TEST_HOST, port=DEFAULT_PORT))
    res = await client.get("/settings?t=%s" % TOKEN)
    settings_body = json.loads(res.body.decode("utf-8"))

    expected_omp_cli = {
        "path": str(omp_script),
        "version": "0.9.0",
        "source": "path",
        "python_client_installed": True,
        "endpoint": {
            "configured": False,
            "reachable": False,
            "status": None,
            "error": "omp_base_url is not set",
            "misconfigured": False,
        },
    }
    assert settings_body["diagnostics"]["omp_cli"] == expected_omp_cli

    assert ("  omp CLI          : 0.9.0  (%s)" % str(omp_script)) in report
    assert (
        "  omp endpoint     : omp_base_url not set; using omp's own "
        "already-configured providers directly"
    ) in report
    assert not any(line.startswith("  omp_rpc package  :") for line in report)
