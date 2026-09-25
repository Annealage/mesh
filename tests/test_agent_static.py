"""Mesh's ``/static/`` tree beside the agent layer's ``/agent/static/``.

The agent layer's route, and the machinery every indexed route shares, are
tested in that package's suite, including that a product's tree registered
through ``static_tree`` and the agent tree never answer for each other. What
is Mesh's is that its own ``/static/`` route (``http/routes_viewer.py``) is
such a separate tree: a cross-tree name or validator must still miss when it
is Mesh's viewer modules on one side.
"""

import pytest
from annealage_agent.http import static as agent_static
from conftest import TEST_HOST, make_test_client

from annealage_mesh.app import DEFAULT_PORT, create_app
from annealage_mesh.http import routes_viewer

pytestmark = pytest.mark.asyncio


@pytest.fixture
def two_trees(tmp_path, monkeypatch):
    """A client whose product tree and agent tree are throwaway directories
    holding a file under the same relative name, with different bytes, plus
    one file only each tree has."""
    product_dir = tmp_path / "product_static"
    agent_dir = tmp_path / "agent_static"
    for tree, text in ((product_dir, "product"), (agent_dir, "agent layer")):
        (tree / "js").mkdir(parents=True)
        (tree / "js" / "ui.js").write_text("// the %s's ui.js\n" % text)
    (product_dir / "viewer.html").write_text("<html></html>")
    (agent_dir / "chat.js").write_text("// only the agent layer has this\n")
    monkeypatch.setattr(routes_viewer, "STATIC_DIR", product_dir)
    monkeypatch.setattr(agent_static, "AGENT_STATIC_DIR", agent_dir)
    served = tmp_path / "served"
    served.mkdir()
    client = make_test_client(create_app(served, host=TEST_HOST, port=DEFAULT_PORT))
    return client, product_dir, agent_dir


async def test_the_agent_tree_and_the_products_never_answer_for_each_other(two_trees):
    client, product_dir, agent_dir = two_trees

    agent_res = await client.get("/agent/static/js/ui.js")
    product_res = await client.get("/static/js/ui.js")
    assert agent_res.body == (agent_dir / "js" / "ui.js").read_bytes()
    assert product_res.body == (product_dir / "js" / "ui.js").read_bytes()
    assert agent_res.headers["ETag"] != product_res.headers["ETag"]

    # A validator one tree issued never confirms the other tree's file of the
    # same name: a browser that cached the product's module must still be
    # sent the agent layer's.
    crossed = await client.get(
        "/agent/static/js/ui.js", headers={"If-None-Match": product_res.headers["ETag"]}
    )
    assert crossed.status_code == 200
    assert crossed.body == agent_res.body
    crossed = await client.get(
        "/static/js/ui.js", headers={"If-None-Match": agent_res.headers["ETag"]}
    )
    assert crossed.status_code == 200
    assert crossed.body == product_res.body

    # And a name only one tree has is reachable only under that tree's prefix.
    assert (await client.get("/static/chat.js")).status_code == 404
    assert (await client.get("/agent/static/viewer.html")).status_code == 404
