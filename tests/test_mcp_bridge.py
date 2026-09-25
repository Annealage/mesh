"""Mesh's side of the Codex tool-exposure bridge's two-token rule
(``planning/tickets/phase3_codex-tool-mcp-bridge.md``, D5).

``/mcp`` itself, the stdio proxy and the agent layer's own browser routes are
tested in annealage-agent's suite. What is Mesh's is ``/submit``
(``http/routes_viewer.py``), the one route Mesh adds that takes the browser
token, which must refuse the agent token just as the agent layer's routes do.
"""

import pytest
from annealage_agent.session.fake import FakeSession
from conftest import make_test_client

from annealage_mesh import app as app_module

pytestmark = pytest.mark.asyncio

TOKEN = "mcp-bridge-test-token"
BROWSER_TOKEN = "mcp-bridge-browser-token"


def _agent_app(project):
    """An agent-mode Mesh app with both tokens set, a real ``MeshTools`` and a
    ``FakeSession``, so its routes are mounted exactly as a real run mounts
    them."""
    from annealage_agent import sessions as sessions_module

    return app_module.create_app(
        project,
        token=BROWSER_TOKEN,
        agent_token=TOKEN,
        mesh_session_id=sessions_module.create_session(project),
        build_session=lambda on_event, *, bus: FakeSession(on_event),
    )


async def test_submit_refuses_the_agent_token(tmp_path):
    """The comment submission refuses the agent token, and is shown to accept
    the browser token, so the refusal is the token check and not the route
    being unreachable."""
    client = make_test_client(_agent_app(tmp_path))
    res = await client.post(
        "/submit?t=%s" % TOKEN,
        headers={"Content-Type": "application/json"},
        body=b"{}",
    )
    assert res.status_code == 403
    res = await client.post(
        "/submit?t=%s" % BROWSER_TOKEN,
        headers={"Content-Type": "application/json"},
        body=b"{}",
    )
    assert res.status_code != 403
