"""The agent layer's own front end over HTTP: ``GET /agent/static/<rel>``
(``agent/http/static.py``), served from ``agent/static/`` through an index of
its own, apart from the product's ``/static/`` tree.

The machinery the route shares with every other indexed route (the extension
allowlist, symlink refusal, rescan on a replaced file, conditional requests)
is covered once, through Mesh's ``/static/`` route, in
``test_routes_viewer.py`` and ``test_paths.py``. What is covered here is what
is particular to this route: that it serves the real tree, that the agent
tree and the product's never answer for each other, and that nothing of the
Python package the tree sits inside is reachable through it.
"""

from pathlib import Path

import pytest
from conftest import TEST_HOST, make_test_client

from annealage_mesh.agent import files
from annealage_mesh.agent.http import static as agent_static
from annealage_mesh.app import DEFAULT_PORT, create_app
from annealage_mesh.http import routes_viewer

pytestmark = pytest.mark.asyncio


async def test_every_file_of_the_real_tree_is_served_and_revalidatable(client):
    # Enumerates the tree rather than naming files, so a module added later is
    # covered without anyone remembering to add it here.
    entries, _ = files.scan_static(agent_static.AGENT_STATIC_DIR)
    assert {"chat.js", "store.js", "ws.js", "agent.css"} <= {e["rel"] for e in entries}
    for entry in entries:
        rel = entry["rel"]
        res = await client.get("/agent/static/" + rel)
        assert res.status_code == 200, rel
        assert res.body == Path(entry["path"]).read_bytes(), rel
        assert res.headers.get("Content-Type") == files.StaticIndex.content_type_of(rel), rel
        assert res.headers.get("Cache-Control") == "no-cache", rel
        again = await client.get(
            "/agent/static/" + rel, headers={"If-None-Match": res.headers["ETag"]}
        )
        assert again.status_code == 304, rel


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
    # Beside both trees, with an extension either tree would serve.
    (tmp_path / "outside.js").write_text("// OUTSIDE-BOTH-TREES\n")
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


async def test_no_file_outside_the_agent_tree_is_reachable_through_it(two_trees):
    """Containment is the index's, not the extension allowlist's: a ``.js``
    file one directory up, and the product's own tree beside it, are both
    servable extensions and both unreachable under /agent/static/."""
    client, _product_dir, _agent_dir = two_trees
    payloads = [
        "/agent/static/%2e%2e/outside.js",
        "/agent/static/..%2foutside.js",
        "/agent/static/../outside.js",
        "/agent/static/%2e%2e/product_static/js/ui.js",
        "/agent/static/..%2fproduct_static%2fviewer.html",
    ]
    for path in payloads:
        res = await client.get(path)
        assert res.status_code == 404, path
        assert b"OUTSIDE-BOTH-TREES" not in (res.body or b""), path
        assert b"product's ui.js" not in (res.body or b""), path


async def test_nothing_beside_the_tree_is_reachable_through_it(client):
    # agent/static/ sits inside the Python package, next to its source, and
    # two levels up from it is Mesh's own static/ tree, whose files carry
    # extensions the allowlist accepts.
    payloads = [
        "/agent/static/../app.py",
        "/agent/static/%2e%2e/app.py",
        "/agent/static/..%2fapp.py",
        "/agent/static/%2e%2e/http/static.py",
        "/agent/static//../product.py",
        "/agent/static/%2e%2e/%2e%2e/static/js/main.js",
        "/agent/static/..%2f..%2fstatic%2fviewer.html",
    ]
    for path in payloads:
        res = await client.get(path)
        assert res.status_code == 404, path
        assert b"def " not in (res.body or b""), path
        assert b"import" not in (res.body or b""), path
        assert b"<html" not in (res.body or b""), path
