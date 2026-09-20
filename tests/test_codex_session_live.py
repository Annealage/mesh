"""On-demand live integration test for ``session/codex.py``'s ``CodexSession``
against the real ``openai_codex.client.CodexClient`` (plan:
``planning/tickets/phase7_live-integration-tests.md``).

Unlike ``tests/test_codex_session.py``, which drives ``CodexSession`` through
``FakeCodexClient``, this file constructs the real driver with no
``client_factory`` override at all: a real ``codex app-server`` subprocess,
a real login, and one real (cheap, text-only) model turn. It proves nothing
that the fake-transport suite does not already prove about approval-gating
or protocol shape (out of scope here, see the ticket) -- it proves only that
the real SDK, given mesh's actual construction of ``CodexConfig``/
``CodexClient``, can authenticate and complete a turn at all.

Gated behind the ``integration`` marker (``pyproject.toml``'s
``[tool.pytest.ini_options]``, excluded from every default run via
``addopts``) and this file's own ``MESH_LIVE_OPENAI_API_KEY`` requirement,
so it skips cleanly -- never errors, never runs silently -- in every
environment without that credential deliberately exported.

``CodexSession.start()`` calls ``account_read()`` and fails closed if no
account is configured (``_fail_not_authenticated()``); there is no in-band
login inside ``start()``. This file therefore performs a real login first,
as setup, with a throwaway raw ``CodexClient`` built the same way
``CodexSession.start()`` builds its own (same ``CodexConfig(cwd=...,
client_name=..., client_title=...)`` shape) -- never by shelling out to the
bundled ``codex`` binary's ``login --with-api-key``, and never against the
shared harness's own ``~/.codex``: ``CODEX_HOME`` is monkeypatched to a
fresh ``tmp_path`` subdirectory before that throwaway client is even
constructed, so both it and the real ``CodexSession`` afterwards (which
inherits the same env var through ``CodexConfig.env``'s
``os.environ.copy()`` merge) only ever touch the isolated directory.
"""

import asyncio
import dataclasses
import os

import pytest
from openai_codex.client import CodexClient, CodexConfig
from openai_codex.generated.v2_all import (
    ApiKeyLoginAccountParams,
    ApiKeyLoginAccountResponse,
    LoginAccountParams,
)

from annealage_mesh.session.base import (
    AGENT_READY,
    AgentError,
    TextDelta,
    TurnEnd,
)
from annealage_mesh.session.codex import CodexSession
from annealage_mesh.session.permissions import PermissionBroker

LIVE_PROMPT = "Reply with exactly the single word PONG and nothing else, no punctuation."


def _diagnose(events: list) -> str:
    """A short dump of every event's class name and key fields, attached to
    assertion failures so a real failure (bad model name, expired key,
    unreachable endpoint) is diagnosable from CI output alone -- never
    including the API key, which this file never puts on an event or in an
    assertion string in the first place."""
    lines = []
    for event in events:
        fields = ", ".join(
            "%s=%r" % (field.name, getattr(event, field.name))
            for field in dataclasses.fields(event)
        )
        lines.append("%s(%s)" % (type(event).__name__, fields))
    return "\n".join(lines) if lines else "(no events recorded)"


async def _wait_for_turn_end(events: list, timeout: float) -> None:
    """Poll ``events`` until a ``TurnEnd`` appears, or fail fast (rather than
    waiting out the full timeout) the moment an ``AgentError`` appears."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    seen = 0
    while True:
        while seen < len(events):
            event = events[seen]
            seen += 1
            if isinstance(event, AgentError):
                pytest.fail(
                    "agent reported an error instead of completing the turn: "
                    "stderr=%r remediation=%r" % (event.stderr, event.remediation)
                )
            if isinstance(event, TurnEnd):
                return
        if loop.time() >= deadline:
            pytest.fail("timed out waiting for TurnEnd:\n%s" % _diagnose(events))
        await asyncio.sleep(0.05)


def _reply_text(events: list) -> str:
    return "".join(event.text for event in events if isinstance(event, TextDelta))


@pytest.mark.integration
@pytest.mark.skipif(
    not os.environ.get("MESH_LIVE_OPENAI_API_KEY"),
    # `.get(...)` (a falsy check), not `"X" not in os.environ`: GitHub
    # Actions expands an unset secret to an empty string rather than
    # omitting the env var entirely, so a presence check alone would let
    # a dispatch with no MESH_LIVE_OPENAI_API_KEY secret configured attempt
    # a real login with an empty key instead of skipping.
    reason="set MESH_LIVE_OPENAI_API_KEY to run the live Codex test",
)
@pytest.mark.asyncio
async def test_real_codex_backend_completes_a_turn(tmp_path, monkeypatch):
    codex_home = tmp_path / "codex-home"  # isolated from ~/.codex
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    api_key = os.environ["MESH_LIVE_OPENAI_API_KEY"]
    # `or "gpt-5.6-luna"`, not `.get(..., "gpt-5.6-luna")`: the same
    # empty-string-from-GitHub-Actions concern applies to the optional
    # override, not just the required key above.
    model = os.environ.get("MESH_LIVE_CODEX_MODEL") or "gpt-5.6-luna"

    # A throwaway login, built exactly the way CodexSession.start() builds
    # its own client (session/codex.py), so the real CodexSession below
    # finds an already-configured account when it calls account_read().
    # approval_handler=None is safe here specifically because a login call
    # never executes a command or applies a patch -- there is nothing for
    # it to auto-accept (contrast session/codex.py's own module docstring,
    # which is about turns, not login).
    login_client = CodexClient(
        config=CodexConfig(
            cwd=str(tmp_path),
            client_name="annealage_mesh_live_test",
            client_title="Annealage Mesh Live Test",
        ),
        approval_handler=None,
    )
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, login_client.start)
    try:
        await loop.run_in_executor(None, login_client.initialize)
        response = await loop.run_in_executor(
            None,
            login_client.account_login_start,
            LoginAccountParams(root=ApiKeyLoginAccountParams(type="apiKey", api_key=api_key)),
        )
        assert isinstance(response.root, ApiKeyLoginAccountResponse), response
    finally:
        await loop.run_in_executor(None, login_client.close)

    events = []
    session = CodexSession(
        events.append,
        cwd=str(tmp_path),
        session_id="live-codex",
        broker=PermissionBroker(events.append, timeout=30.0, no_viewer_grace=0.05),
        model=model,
    )
    session.on_viewer_presence(1)
    try:
        await session.start()
        assert session.agent_status() == AGENT_READY, _diagnose(events)
        await session.submit_turn([{"type": "text", "text": LIVE_PROMPT}])
        await _wait_for_turn_end(events, timeout=60.0)
    finally:
        await session.close()
    assert "pong" in _reply_text(events).lower(), _diagnose(events)
