"""One real, on-demand integration test for ``session/sdk.py``: proves the
real ``claude_agent_sdk`` package, given mesh's actual generated options and
no ``transport=`` override, can authenticate against the live Anthropic API
and complete one trivial text-only turn.

``tests/test_sdk_session.py`` drives the same class through a hand-written
``FakeTransport`` and pins protocol shape; it proves nothing about whether a
real ``claude`` subprocess actually starts, authenticates, and replies. This
file closes that gap, gated behind the ``integration`` marker (opt-in only,
excluded from every default run by ``pyproject.toml``'s own `addopts`) and a
``pytest.mark.skipif`` on its own required credential, so a default
``pytest`` invocation never depends on it and a developer with no Anthropic
account never has it silently run.

``MESH_LIVE_ANTHROPIC_API_KEY``, not the bare ``ANTHROPIC_API_KEY`` a shell
commonly has set for unrelated reasons, is what gates this test on
purpose - see the phase 7 ticket's "Env vars" section. The real
``ANTHROPIC_API_KEY`` the CLI subprocess needs is set via ``monkeypatch`` for
the duration of the test only, from that value.

Verified against ``claude_agent_sdk``'s installed
``_internal/transport/subprocess_cli.py`` before writing this: the real
subprocess transport builds its child's environment as
``{**inherited_env, "CLAUDE_CODE_ENTRYPOINT": ..., **self._options.env, ...}``
where ``inherited_env`` is ``os.environ`` (minus ``CLAUDECODE``) copied
wholesale, and ``SdkSession`` never passes a custom ``env`` into
``ClaudeAgentOptions``. So a plain ``monkeypatch.setenv`` on this test
process, with no separate passthrough, is sufficient for the child to see
``ANTHROPIC_API_KEY``.
"""

import asyncio
import os

import pytest

from annealage_mesh.session.base import AGENT_READY, AgentError, TextDelta, TurnEnd
from annealage_mesh.session.permissions import PermissionBroker
from annealage_mesh.session.sdk import SdkSession

LIVE_PROMPT = "Reply with exactly the single word PONG and nothing else, no punctuation."


async def _wait_for_turn_end(events: list, timeout: float) -> None:
    """Poll ``events`` until a ``TurnEnd`` arrives, failing fast on an
    ``AgentError`` instead of waiting out the full timeout for one."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        for event in events:
            if isinstance(event, TurnEnd):
                return
            if isinstance(event, AgentError):
                raise AssertionError("agent reported an error:\n%s" % _diagnose(events))
        await asyncio.sleep(0.05)
    raise AssertionError("timed out waiting for turn_end:\n%s" % _diagnose(events))


def _reply_text(events: list) -> str:
    """The model's full reply, as the concatenation of every ``TextDelta``."""
    return "".join(event.text for event in events if isinstance(event, TextDelta))


def _diagnose(events: list) -> str:
    """A compact dump of every event seen so far, for an assertion failure
    message."""
    return "\n".join("%s: %r" % (type(event).__name__, event) for event in events)


@pytest.mark.integration
@pytest.mark.skipif(
    not os.environ.get("MESH_LIVE_ANTHROPIC_API_KEY"),
    # `.get(...)` (a falsy check), not `"X" not in os.environ`: GitHub
    # Actions expands an unset secret to an empty string rather than
    # omitting the env var entirely, so a presence check alone would let
    # a dispatch with no MESH_LIVE_ANTHROPIC_API_KEY secret configured
    # attempt a real run with an empty key instead of skipping.
    reason="set MESH_LIVE_ANTHROPIC_API_KEY to run the live Claude test",
)
@pytest.mark.asyncio
async def test_real_claude_backend_completes_a_turn(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", os.environ["MESH_LIVE_ANTHROPIC_API_KEY"])
    # `or "haiku"`, not `.get(..., "haiku")`: the same empty-string-from-
    # GitHub-Actions concern applies to the optional override, not just
    # the required key above.
    model = os.environ.get("MESH_LIVE_CLAUDE_MODEL") or "haiku"
    events: list = []
    session = SdkSession(
        events.append,
        cwd=str(tmp_path),
        session_id="live-claude",
        broker=PermissionBroker(events.append, timeout=30.0, no_viewer_grace=0.05),
        model=model,
        sandbox=False,  # no bash/tool use in this prompt; do not require
        # bwrap/socat just to prove a live turn works
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
