"""Mesh's own contribution to ``GET /settings``.

The route itself (the token gate, provenance, pending, what ``PUT`` refuses
and writes) is the agent layer's, and is tested in that package's suite
against its toy product. What is Mesh's is the key it adds and the product
it reports: ``up_axis`` laid out in the Viewer section with its two choices,
and Mesh's own title and state directory.
"""

import json

import pytest
from conftest import TEST_HOST, make_test_client

from annealage_mesh.app import DEFAULT_PORT, create_app

pytestmark = pytest.mark.asyncio

TOKEN = "the-real-settings-token-Value_123"


def body_of(res):
    """The parsed JSON body of a response.

    ``Response`` carries no ``.json`` (only microdot's ``TestResponse`` does,
    and only when the route returned one), so the bytes are parsed here.
    """
    return json.loads(res.body.decode("utf-8"))


async def test_get_lays_out_the_window_from_each_keys_own_section(served_dir):
    """The window names no key itself: which keys it shows, under which
    heading and as which choices all come from here. Mesh's ``up_axis`` joins
    the generic Viewer section ahead of the chat pane's preference, a key with
    no section (the omp endpoint and its API key) is not laid out at all, and
    a key with a closed set of values carries it."""
    client = make_test_client(
        create_app(served_dir, token=TOKEN, host=TEST_HOST, port=DEFAULT_PORT)
    )
    payload = body_of(await client.get("/settings?t=%s" % TOKEN))
    assert payload["sections"] == [
        {"title": "Server", "keys": ["host", "port", "open_browser"]},
        {
            "title": "Agent",
            "keys": ["model", "effort", "permission_mode", "approval_timeout", "backend"],
        },
        {"title": "Viewer", "keys": ["up_axis", "tool_cards_collapsed"]},
    ]
    assert payload["settings"]["up_axis"]["choices"] == ["z", "y"]
    assert payload["settings"]["up_axis"]["nullable"] is False
    assert payload["settings"]["backend"]["choices"] == ["claude", "codex", "omp"]
    assert payload["settings"]["backend"]["nullable"] is True
    assert payload["product"]["title"] == "Mesh"
    assert payload["product"]["state_dirname"] == ".mesh"
