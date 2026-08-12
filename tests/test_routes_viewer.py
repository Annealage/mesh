"""In-process HTTP contract tests for Annealage Mesh's microdot routes.

Exercises the routes registered by ``http/routes_viewer.py`` through
microdot's ``TestClient``, which dispatches a request straight into the app
object with no socket and no thread. Covers the documented route contract
for the packaged viewer, manifest and submit endpoints; the flat manifest
scan with its exclusions and its handling of symlinks; the
manifest-index-only ``/model``, ``/model_alias`` and ``/asset`` routes; and
the request-shape and disclosure controls that keep those routes from
serving anything outside their documented contract.
"""

import asyncio
import json
import os
import time
from pathlib import Path

import pytest
from microdot import Request
from microdot.test_client import TestClient

from annealage_mesh import paths
from annealage_mesh.app import MAX_REQUEST_BODY, create_app
from annealage_mesh.http import routes_viewer

pytestmark = pytest.mark.asyncio


@pytest.fixture
def served_dir(tmp_path):
    (tmp_path / "widget.stl").write_bytes(b"solid widget\nendsolid widget\n")
    return tmp_path


@pytest.fixture
def client(served_dir):
    return TestClient(create_app(served_dir))


# --- the packaged viewer page -----------------------------------------------

async def test_index_serves_viewer_html(client):
    for path in ("/", "/index.html"):
        res = await client.get(path)
        assert res.status_code == 200, path
        assert "text/html" in res.headers.get("Content-Type", "")
        assert 'id="topbar"' in res.text
        assert "/manifest" in res.text


# --- /manifest ---------------------------------------------------------------

async def test_manifest_lists_stl(client, served_dir):
    res = await client.get("/manifest")
    assert res.status_code == 200
    assert "application/json" in res.headers.get("Content-Type", "")
    data = res.json
    assert data["dir"] == str(served_dir)
    models = data["models"]
    assert len(models) == 1
    model = models[0]
    # The documented contract the shipped viewer consumes. Asserted as a
    # subset, and "rel" checked separately, so an additive key does not
    # break this test.
    expected = {"name": "widget", "file": "widget.stl", "path": str(served_dir / "widget.stl")}
    assert expected.items() <= model.items()
    assert model["rel"] == "widget.stl"
    assert "ambiguous" not in model
    assert "unreachable_by_filename" not in data
    assert "truncated" in data


async def test_manifest_scan_is_flat_nested_model_not_listed_or_fetchable(client, served_dir):
    """A model in a subdirectory is absent from the manifest and unreachable by rel or alias."""
    (served_dir / "parts").mkdir()
    (served_dir / "parts" / "nested.stl").write_bytes(b"solid n\nendsolid n\n")

    res = await client.get("/manifest")
    rels = {m["rel"] for m in res.json["models"]}
    assert "parts/nested.stl" not in rels

    res = await client.get("/model/parts/nested.stl")
    assert res.status_code == 404

    res = await client.get("/nested.stl")
    assert res.status_code == 404


@pytest.mark.parametrize("dirname", [".hidden", ".git", ".mesh"])
async def test_manifest_excludes_dot_directories(client, served_dir, dirname):
    excluded_dir = served_dir / dirname
    excluded_dir.mkdir()
    (excluded_dir / "excluded.stl").write_bytes(b"solid x\nendsolid x\n")

    res = await client.get("/manifest")
    rels = {m["rel"] for m in res.json["models"]}
    excluded_rel = "%s/excluded.stl" % dirname
    assert excluded_rel not in rels
    assert not any(r.startswith(".") for r in rels)

    # Not just absent from the listing: genuinely unreachable.
    res = await client.get("/model/" + excluded_rel)
    assert res.status_code == 404


async def test_manifest_cap_truncates(client, served_dir, monkeypatch):
    monkeypatch.setattr(paths, "MAX_INDEXED_FILES", 2)
    for i in range(5):
        (served_dir / ("extra%d.stl" % i)).write_bytes(b"solid e\nendsolid e\n")

    res = await client.get("/manifest")
    data = res.json
    assert data["truncated"] is True
    assert len(data["models"]) == 2


# --- /model/<rel> --------------------------------------------------------------

async def test_model_route_serves_exact_bytes(client, served_dir):
    res = await client.get("/model/widget.stl")
    assert res.status_code == 200
    assert res.body == (served_dir / "widget.stl").read_bytes()


async def test_model_route_content_type_is_not_octet_stream(client):
    # mimetypes has no entry for .stl; the route must use paths.CONTENT_TYPES
    # rather than falling back to the generic default.
    res = await client.get("/model/widget.stl")
    assert res.headers.get("Content-Type") == paths.CONTENT_TYPES[".stl"]
    assert res.headers.get("Content-Type") != "application/octet-stream"


async def test_model_route_refuses_unlisted_path(client):
    res = await client.get("/model/does-not-exist.stl")
    assert res.status_code == 404


async def test_model_scan_refuses_a_symlink_even_when_it_resolves_inside(client, served_dir):
    """A symlink is not indexed, whatever it points at.

    Validating where one points means resolving a path, resolving is several
    lookups, and anything able to write to the served directory can change the
    entry between them, so a link aimed outside the tree can pass a check
    applied to what the name meant a moment earlier. Refusing symlinks removes
    that class rather than narrowing the window, and a directory of models to
    review does not need them.
    """
    (served_dir / "alias.stl").symlink_to("widget.stl")

    res = await client.get("/manifest")
    rels = {m["rel"] for m in res.json["models"]}
    assert "alias.stl" not in rels
    assert "widget.stl" in rels

    for path in ("/model/alias.stl", "/alias.stl"):
        res = await client.get(path)
        assert res.status_code == 404, path


async def test_model_scan_refuses_a_hardlinked_model(client, served_dir, tmp_path_factory):
    """A hardlink publishes a file from outside the served directory, and the
    name gives no sign of it, so an entry with more than one link is skipped."""
    outside = tmp_path_factory.mktemp("outside")
    secret = outside / "secret.stl"
    secret.write_bytes(b"solid secret\nendsolid secret\n")
    try:
        os.link(secret, served_dir / "linked.stl")
    except OSError as exc:
        pytest.skip("cannot hardlink across these directories: %s" % exc)

    res = await client.get("/manifest")
    assert "linked.stl" not in {m["rel"] for m in res.json["models"]}

    for path in ("/model/linked.stl", "/linked.stl"):
        res = await client.get(path)
        assert res.status_code == 404, path
        assert b"secret" not in (res.body or b"")


# --- /<name>.stl compatibility alias ------------------------------------------

async def test_stl_alias_serves_exact_bytes(client, served_dir):
    res = await client.get("/widget.stl")
    assert res.status_code == 200
    assert res.body == (served_dir / "widget.stl").read_bytes()


async def test_stl_alias_content_type_is_not_octet_stream(client):
    res = await client.get("/widget.stl")
    assert res.headers.get("Content-Type") == paths.CONTENT_TYPES[".stl"]


async def test_stl_alias_refuses_unlisted_name(client):
    res = await client.get("/does-not-exist.stl")
    assert res.status_code == 404


# --- /asset/<rel> --------------------------------------------------------------

async def test_asset_route_serves_from_images(client, served_dir):
    images = served_dir / "images"
    images.mkdir()
    (images / "photo.png").write_bytes(b"\x89PNG fake bytes")

    res = await client.get("/asset/photo.png")
    assert res.status_code == 200
    assert res.body == b"\x89PNG fake bytes"
    assert res.headers.get("Content-Type") == paths.CONTENT_TYPES[".png"]


async def test_asset_route_404_when_images_dir_absent(client):
    res = await client.get("/asset/photo.png")
    assert res.status_code == 404


async def test_asset_route_refuses_file_outside_images(client, served_dir):
    (served_dir / "images").mkdir()
    (served_dir / "sibling.png").write_bytes(b"outside images/")

    res = await client.get("/asset/../sibling.png")
    assert res.status_code == 404


# --- /callouts, /callouts.json --------------------------------------------------

async def test_callouts_empty_when_absent(client):
    res = await client.get("/callouts")
    assert res.status_code == 200
    assert res.json == {"annotations": []}


async def test_callouts_served_when_present(client, served_dir):
    payload = {"annotations": [{"id": 1, "author": "agent", "part": "widget",
                                 "label": "+Z", "point": [1, 2, 3], "comment": "hi"}]}
    (served_dir / "mesh-callouts.json").write_text(json.dumps(payload))

    res = await client.get("/callouts")
    assert res.status_code == 200
    assert res.json == payload

    res = await client.get("/callouts.json")
    assert res.status_code == 200
    assert res.json == payload


# --- POST /submit --------------------------------------------------------------

async def test_submit_writes_comments(client, served_dir):
    pins = [{"id": 1, "part": "widget", "label": "+X", "point": [1.0, 2.0, 3.0],
             "normal": [1.0, 0.0, 0.0], "faceIndex": 4, "comment": "looks thin here"}]

    res = await client.post("/submit", body=pins)
    assert res.status_code == 200
    assert res.json["ok"] is True
    assert res.json["count"] == 1

    comments_file = served_dir / "mesh-comments.json"
    assert comments_file.is_file()
    record = json.loads(comments_file.read_text())
    assert record["count"] == 1
    assert record["annotations"] == pins
    assert "submitted_at" in record

    log_file = served_dir / "mesh-comments.log"
    assert log_file.is_file()
    log_lines = log_file.read_text().strip().splitlines()
    assert len(log_lines) == 1
    assert json.loads(log_lines[0])["annotations"] == pins


async def test_submit_invalid_json_400(client):
    res = await client.post("/submit", body=b"{not json")
    assert res.status_code == 400
    assert res.json["ok"] is False


async def test_submit_non_array_400(client):
    res = await client.post("/submit", body={"not": "an array"})
    assert res.status_code == 400
    assert res.json["ok"] is False


# --- disclosure hole closed: nothing but manifest-listed models and -----------
# --- images/-scoped assets is reachable, from any route, by any path shape ---

async def test_secret_text_file_not_reachable_by_any_route(client, served_dir):
    (served_dir / "secret.txt").write_text("do not leak")

    for path in ("/secret.txt", "/model/secret.txt", "/asset/secret.txt"):
        res = await client.get(path)
        assert res.status_code == 404, path
        assert b"do not leak" not in (res.body or b"")


async def test_git_directory_not_reachable_by_any_route(client, served_dir):
    git_dir = served_dir / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text("do not leak git config")

    for path in ("/.git/config", "/model/.git/config", "/asset/.git/config"):
        res = await client.get(path)
        assert res.status_code == 404, path
        assert b"do not leak git config" not in (res.body or b"")


async def test_model_route_immune_to_traversal_shapes(client, served_dir):
    # /model/<rel> resolves through ModelIndex.by_rel, an allowlist dict
    # built from a real scan: there is no per-request filesystem join to
    # trick, so every payload shape below must simply miss the dict, whether
    # or not percent-decoding turns it into a literal ".." shape first.
    sibling_secret = served_dir.parent / "sibling-secret.stl"
    sibling_secret.write_bytes(b"solid leak\nendsolid leak\n")

    payloads = [
        "/model/../sibling-secret.stl",           # ".." traversal
        "/model/%2e%2e%2fsibling-secret.stl",     # URL-encoded traversal
        "/model//../sibling-secret.stl",          # doubled slash
        "/model/..\\sibling-secret.stl",          # backslash
        "/model//" + str(sibling_secret),          # absolute path
    ]
    for path in payloads:
        res = await client.get(path)
        assert res.status_code == 404, path
        assert b"leak" not in (res.body or b"")


async def test_asset_route_refuses_every_traversal_shape(client, served_dir):
    # /asset/<rel> does a real filesystem resolve-and-contain check
    # (paths.safe_join); these payloads exercise that check directly rather
    # than relying on an allowlist miss.
    images = served_dir / "images"
    images.mkdir()
    (images / "legit.png").write_bytes(b"legit image bytes")
    (served_dir / "secret.txt").write_text("do not leak")

    payloads = [
        "/asset/../secret.txt",                 # ".." traversal
        "/asset/%2e%2e/secret.txt",             # URL-encoded traversal
        "/asset//../secret.txt",                # doubled slash
        "/asset/..\\secret.txt",                # backslash
        "/asset//" + str(served_dir / "secret.txt"),  # absolute path
    ]
    for path in payloads:
        res = await client.get(path)
        assert res.status_code == 404, path
        assert b"do not leak" not in (res.body or b"")


async def test_stl_alias_and_manifest_exclude_symlink_outside_directory(client, served_dir, tmp_path_factory):
    outside_dir = tmp_path_factory.mktemp("outside")
    outside_secret = outside_dir / "outside-secret.stl"
    outside_secret.write_bytes(b"solid outside\nendsolid outside\n")
    (served_dir / "escape.stl").symlink_to(outside_secret)

    res = await client.get("/manifest")
    rels = {m["rel"] for m in res.json["models"]}
    assert "escape.stl" not in rels

    res = await client.get("/escape.stl")
    assert res.status_code == 404
    res = await client.get("/model/escape.stl")
    assert res.status_code == 404


async def test_asset_route_refuses_symlink_outside_directory(client, served_dir, tmp_path_factory):
    outside_dir = tmp_path_factory.mktemp("outside")
    outside_secret = outside_dir / "outside-secret.png"
    outside_secret.write_bytes(b"outside png bytes")

    images = served_dir / "images"
    images.mkdir()
    (images / "escape.png").symlink_to(outside_secret)

    res = await client.get("/asset/escape.png")
    assert res.status_code == 404
    assert b"outside png bytes" not in (res.body or b"")


# --- HEAD parity with GET ------------------------------------------------------

async def test_head_matches_get_for_manifest(client):
    get_res = await client.get("/manifest")
    head_res = await client.request("HEAD", "/manifest")

    assert head_res.status_code == get_res.status_code
    assert head_res.headers.get("Content-Type") == get_res.headers.get("Content-Type")
    assert head_res.body is None


async def test_head_matches_get_for_model_file(client, served_dir):
    get_res = await client.get("/widget.stl")
    head_res = await client.request("HEAD", "/widget.stl")

    assert head_res.status_code == get_res.status_code
    assert head_res.headers.get("Content-Type") == get_res.headers.get("Content-Type")
    assert head_res.headers.get("Content-Length") == get_res.headers.get("Content-Length")
    assert head_res.body is None


# --- case-insensitive .stl alias -----------------------------------------------

async def test_stl_alias_matches_uppercase_extension(client, served_dir):
    (served_dir / "WIDGET.STL").write_bytes(b"solid upper\nendsolid upper\n")

    res = await client.get("/manifest")
    rels = {m["rel"] for m in res.json["models"]}
    assert "WIDGET.STL" in rels

    res = await client.get("/WIDGET.STL")
    assert res.status_code == 200
    assert res.body == (served_dir / "WIDGET.STL").read_bytes()


async def test_stl_alias_matches_mixed_case_extension(client, served_dir):
    (served_dir / "part.Stl").write_bytes(b"solid mixed\nendsolid mixed\n")

    res = await client.get("/part.Stl")
    assert res.status_code == 200
    assert res.body == (served_dir / "part.Stl").read_bytes()


# --- every manifest entry is fetchable ----------------------------------------

async def test_every_manifest_entry_is_fetchable_by_its_rel(client, served_dir):
    # The manifest is the viewer's only source of what to fetch, so every
    # entry it lists must answer at /model/<rel>. A flat scan cannot produce
    # two entries sharing a rel, since a directory cannot hold two entries
    # with one name; files in subdirectories are not scanned at all and so
    # never appear in the manifest.
    (served_dir / "a").mkdir()
    (served_dir / "b").mkdir()
    (served_dir / "a" / "dupe.stl").write_bytes(b"solid a\nendsolid a\n")
    (served_dir / "b" / "dupe.stl").write_bytes(b"solid b\nendsolid b\n")
    (served_dir / "plain.stl").write_bytes(b"solid p\nendsolid p\n")

    res = await client.get("/manifest")
    models = res.json["models"]
    assert models
    for model in models:
        res_rel = await client.get("/model/" + model["rel"])
        assert res_rel.status_code == 200, model


# --- symlink exclusion beyond the containment check -----------------------------

async def test_symlink_into_dotdir_target_excluded_despite_stl_looking_name(client, served_dir):
    git_dir = served_dir / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text("TOPSECRET-FLAG-GIT")
    (served_dir / "gitcfg.stl").symlink_to(Path(".git") / "config")

    res = await client.get("/manifest")
    rels = {m["rel"] for m in res.json["models"]}
    assert "gitcfg.stl" not in rels

    for path in ("/gitcfg.stl", "/model/gitcfg.stl"):
        res = await client.get(path)
        assert res.status_code == 404, path
        assert b"TOPSECRET" not in (res.body or b"")


async def test_symlink_to_non_model_target_excluded_despite_stl_looking_name(client, served_dir):
    (served_dir / "SECRET.txt").write_text("do not leak")
    (served_dir / "secretlink.stl").symlink_to("SECRET.txt")

    res = await client.get("/manifest")
    rels = {m["rel"] for m in res.json["models"]}
    assert "secretlink.stl" not in rels

    for path in ("/secretlink.stl", "/model/secretlink.stl"):
        res = await client.get(path)
        assert res.status_code == 404, path
        assert b"do not leak" not in (res.body or b"")


# --- percent-decoded model and asset paths --------------------------------------

async def test_model_route_decodes_space_and_non_ascii_filenames(client, served_dir):
    (served_dir / "my part.stl").write_bytes(b"solid sp\nendsolid sp\n")
    (served_dir / "caf\u00e9-pi\u00e8ce.stl").write_bytes(b"solid nb\nendsolid nb\n")

    res = await client.get("/model/my%20part.stl")
    assert res.status_code == 200
    assert res.body == b"solid sp\nendsolid sp\n"

    res = await client.get("/my%20part.stl")
    assert res.status_code == 200
    assert res.body == b"solid sp\nendsolid sp\n"

    # Percent-encoded UTF-8, which is how a browser sends a non-ASCII name.
    res = await client.get("/model/caf%C3%A9-pi%C3%A8ce.stl")
    assert res.status_code == 200
    assert res.body == b"solid nb\nendsolid nb\n"

    res = await client.get("/caf%C3%A9-pi%C3%A8ce.stl")
    assert res.status_code == 200
    assert res.body == b"solid nb\nendsolid nb\n"


async def test_asset_route_decodes_space_in_filename(client, served_dir):
    images = served_dir / "images"
    images.mkdir()
    (images / "my photo.png").write_bytes(b"space photo bytes")

    res = await client.get("/asset/my%20photo.png")
    assert res.status_code == 200
    assert res.body == b"space photo bytes"


# --- images/ as a symlink -------------------------------------------------------

async def test_asset_route_refuses_a_symlinked_images_directory(client, served_dir):
    # A symlink at images/ cannot be made safe by checking where it points,
    # because containment is satisfied by the served directory itself: an
    # "images -> ." link passes that test and then becomes the base every
    # /asset request is joined against, which restores the serve-anything
    # fallback this route replaces. Pointing it at a subdirectory is no
    # better, since nothing there was indexed.
    real_images = served_dir / "real_images"
    real_images.mkdir()
    (real_images / "photo.png").write_bytes(b"\x89PNG real bytes")
    (served_dir / "images").symlink_to(real_images)

    res = await client.get("/asset/photo.png")
    assert res.status_code == 404
    assert b"real bytes" not in (res.body or b"")


async def test_asset_route_refuses_images_symlinked_to_the_served_dir(client, served_dir):
    # The specific shape that defeats a containment-only check.
    (served_dir / "secret.txt").write_text("TOPSECRET-FLAG-ASSET")
    (served_dir / "images").symlink_to(served_dir)

    res = await client.get("/asset/secret.txt")
    assert res.status_code == 404
    assert b"TOPSECRET-FLAG-ASSET" not in (res.body or b"")


async def test_asset_route_refuses_images_symlink_outside_served_dir(
        client, served_dir, tmp_path_factory):
    # An images/ symlink whose target is outside the served directory (a
    # shape that arrives inside a zip, a tarball or a git clone, not only
    # by an operator's own hand) must not turn every /asset request into a
    # read of anything under that other location.
    outside_dir = tmp_path_factory.mktemp("outside")
    (outside_dir / "secret.png").write_bytes(b"outside png bytes")
    (served_dir / "images").symlink_to(outside_dir)

    res = await client.get("/asset/secret.png")
    assert res.status_code == 404
    assert b"outside png bytes" not in (res.body or b"")


# --- /asset content-type restriction and dotdir exclusion ----------------------

async def test_asset_route_serves_non_image_extensions_as_octet_stream(client, served_dir):
    # images/ can contain whatever a reviewed bundle happened to ship. A
    # file saved with an ".html" or ".svg" extension must never be labelled
    # as active content on this server's own origin, regardless of what it
    # actually contains, since that label is what would let a browser run
    # it as script.
    images = served_dir / "images"
    images.mkdir()
    (images / "evil.html").write_text("<script>alert(1)</script>")
    (images / "evil.svg").write_text("<svg onload='alert(1)'></svg>")

    for name in ("evil.html", "evil.svg"):
        res = await client.get("/asset/" + name)
        assert res.status_code == 200
        assert res.headers.get("Content-Type") == "application/octet-stream"


async def test_asset_route_excludes_dot_prefixed_path_components(client, served_dir):
    hidden = served_dir / "images" / "sub" / ".secretdir"
    hidden.mkdir(parents=True)
    (hidden / "x.txt").write_text("do not leak")

    res = await client.get("/asset/sub/.secretdir/x.txt")
    assert res.status_code == 404
    assert b"do not leak" not in (res.body or b"")


async def test_responses_carry_nosniff_header(client):
    res = await client.get("/manifest")
    assert res.headers.get("X-Content-Type-Options") == "nosniff"


# --- file_response 404s do not disclose the resolved path or existence ---------

async def test_asset_404_does_not_disclose_resolved_path_or_existence(client, served_dir):
    # An absent name and a present-but-unreadable one must produce the same
    # template ("not found: <the name the client asked for>"), so neither
    # echoes the server's absolute filesystem path, and the only thing that
    # varies between the two responses is the name the client itself
    # supplied, not a signal of which cause produced the 404.
    images = served_dir / "images"
    images.mkdir()
    unreadable = images / "noperm.png"
    unreadable.write_bytes(b"\x89PNG bytes")
    unreadable.chmod(0o000)
    try:
        res_unreadable = await client.get("/asset/noperm.png")
    finally:
        unreadable.chmod(0o644)
    res_absent = await client.get("/asset/does-not-exist.png")

    assert res_unreadable.status_code == 404
    assert res_absent.status_code == 404
    assert res_unreadable.body == b"not found: noperm.png"
    assert res_absent.body == b"not found: does-not-exist.png"
    assert str(unreadable) not in (res_unreadable.text or "")


# --- model-index caching keeps the scan off the event loop and shared ----------

async def test_manifest_and_model_fetches_share_one_scan_within_the_cache_window(
        client, served_dir, monkeypatch):
    calls = []
    real_scan = paths.scan_models

    def counting_scan(serve_dir):
        calls.append(serve_dir)
        return real_scan(serve_dir)

    monkeypatch.setattr(paths, "scan_models", counting_scan)

    await client.get("/manifest")
    await client.get("/widget.stl")
    await client.get("/model/widget.stl")

    assert len(calls) == 1


async def test_concurrent_requests_during_a_cold_window_share_one_scan(
        client, served_dir, monkeypatch):
    # A burst of requests arriving while the cache is cold (startup, or just
    # after the TTL expires) must not each launch their own recursive walk;
    # they should all await the one scan already in flight. A short sleep
    # inside the (executor-run) scan widens the race window deterministically,
    # since without it a fast real scan might finish before a second
    # concurrent coroutine even reaches its own cache check.
    calls = []
    real_scan = paths.scan_models

    def slow_counting_scan(serve_dir):
        calls.append(serve_dir)
        time.sleep(0.05)
        return real_scan(serve_dir)

    monkeypatch.setattr(paths, "scan_models", slow_counting_scan)

    results = await asyncio.gather(*[client.get("/manifest") for _ in range(20)])

    assert len(calls) == 1
    assert all(res.status_code == 200 for res in results)


async def test_manifest_rescans_after_the_cache_window_expires(client, served_dir, monkeypatch):
    monkeypatch.setattr(routes_viewer, "INDEX_CACHE_TTL", 0.0)

    res1 = await client.get("/manifest")
    assert {m["rel"] for m in res1.json["models"]} == {"widget.stl"}

    (served_dir / "second.stl").write_bytes(b"solid s\nendsolid s\n")
    res2 = await client.get("/manifest")
    assert {m["rel"] for m in res2.json["models"]} == {"widget.stl", "second.stl"}


# --- POST /submit body size ------------------------------------------------------

def _pin(i):
    return {
        "id": i, "part": "widget", "label": "+X",
        "point": [1.0, 2.0, 3.0], "normal": [1.0, 0.0, 0.0],
        "faceIndex": i, "comment": "pin number %d, long enough to add up" % i,
    }


async def test_submit_accepts_a_review_over_microdots_default_body_limit(
        client, served_dir):
    pins = [_pin(i) for i in range(200)]
    assert len(json.dumps(pins).encode("utf-8")) > 16384

    res = await client.post("/submit", body=pins)
    assert res.status_code == 200
    assert res.json["count"] == 200

    record = json.loads((served_dir / "mesh-comments.json").read_text())
    assert record["count"] == 200
    assert record["annotations"] == pins


async def test_submit_over_max_request_body_returns_413_with_json_error(client):
    assert Request.max_content_length == MAX_REQUEST_BODY
    res = await client.post("/submit", body=b"x" * (MAX_REQUEST_BODY + 1))
    assert res.status_code == 413
    assert res.json["ok"] is False
    assert "error" in res.json


async def test_submit_rejects_non_object_array_elements_without_destroying_prior_data(
        client, served_dir):
    good_pins = [_pin(1), _pin(2), _pin(3)]
    res = await client.post("/submit", body=good_pins)
    assert res.status_code == 200

    res = await client.post("/submit", body=[1, 2, 3])
    assert res.status_code == 400
    assert res.json["ok"] is False

    record = json.loads((served_dir / "mesh-comments.json").read_text())
    assert record["count"] == 3
    assert record["annotations"] == good_pins


# --- fixed-name exchange files refuse a symlink, a hardlink or a FIFO ----------

async def test_callouts_refuses_a_symlink_to_outside_the_served_directory(
        client, served_dir, tmp_path_factory):
    """A symlink at mesh-callouts.json pointing outside the served directory is refused, not followed."""
    outside_dir = tmp_path_factory.mktemp("outside")
    victim = outside_dir / "victim.json"
    victim.write_text("TOPSECRET-CALLOUTS-SYMLINK")
    (served_dir / paths.CALLOUTS_JSON_NAME).symlink_to(victim)

    for path in ("/callouts", "/callouts.json"):
        res = await client.get(path)
        assert res.status_code == 200, path
        assert res.json == {"annotations": []}
        assert b"TOPSECRET-CALLOUTS-SYMLINK" not in (res.body or b"")


async def test_callouts_refuses_a_hardlink_to_outside_the_served_directory(
        client, served_dir, tmp_path_factory):
    """A hardlink at mesh-callouts.json to a file outside the served directory is refused."""
    outside_dir = tmp_path_factory.mktemp("outside")
    victim = outside_dir / "victim.json"
    victim.write_text("TOPSECRET-CALLOUTS-HARDLINK")
    target = served_dir / paths.CALLOUTS_JSON_NAME
    try:
        os.link(str(victim), str(target))
    except OSError as exc:
        pytest.skip("hardlink across filesystems not supported here: %s" % exc)

    res = await client.get("/callouts")
    assert res.status_code == 200
    assert res.json == {"annotations": []}
    assert b"TOPSECRET-CALLOUTS-HARDLINK" not in (res.body or b"")


@pytest.mark.parametrize("name", [paths.COMMENTS_JSON_NAME, paths.COMMENTS_LOG_NAME])
async def test_submit_refuses_a_symlink_at_either_comments_file(
        client, served_dir, tmp_path_factory, name):
    """POST /submit refuses to follow a symlink planted at either comments file name."""
    outside_dir = tmp_path_factory.mktemp("outside")
    victim = outside_dir / "victim.txt"
    victim.write_text("TOPSECRET-COMMENTS-SYMLINK")
    (served_dir / name).symlink_to(victim)

    res = await client.post("/submit", body=[_pin(1)])
    assert res.status_code == 500
    assert res.json["ok"] is False
    assert victim.read_text() == "TOPSECRET-COMMENTS-SYMLINK"


async def test_submit_refuses_a_hardlink_at_the_comments_log(
        client, served_dir, tmp_path_factory):
    """POST /submit refuses to write through a hardlink planted at mesh-comments.log."""
    outside_dir = tmp_path_factory.mktemp("outside")
    victim = outside_dir / "victim.log"
    victim.write_text("TOPSECRET-LOG-HARDLINK\n")
    target = served_dir / paths.COMMENTS_LOG_NAME
    try:
        os.link(str(victim), str(target))
    except OSError as exc:
        pytest.skip("hardlink across filesystems not supported here: %s" % exc)

    res = await client.post("/submit", body=[_pin(1)])
    assert res.status_code == 500
    assert res.json["ok"] is False
    assert victim.read_text() == "TOPSECRET-LOG-HARDLINK\n"


async def test_submit_refuses_a_fifo_at_the_comments_log_without_hanging(client, served_dir):
    """A FIFO at mesh-comments.log is refused up front rather than blocking an executor thread."""
    if not hasattr(os, "mkfifo"):
        pytest.skip("os.mkfifo not available on this platform")
    os.mkfifo(str(served_dir / paths.COMMENTS_LOG_NAME))

    res = await asyncio.wait_for(client.post("/submit", body=[_pin(1)]), timeout=5.0)
    assert res.status_code == 500
    assert res.json["ok"] is False


# --- concurrent submissions ------------------------------------------------------

async def test_concurrent_submissions_leave_well_formed_files_and_no_temp_droppings(
        client, served_dir):
    """Concurrent POST /submit calls each leave one intact log line and no leftover temp file."""
    n = 12
    results = await asyncio.gather(*[
        client.post("/submit", body=[_pin(i)]) for i in range(n)
    ])
    assert all(res.status_code == 200 for res in results)
    assert {res.json["count"] for res in results} == {1}

    record = json.loads((served_dir / "mesh-comments.json").read_text())
    assert record["count"] == len(record["annotations"])

    log_lines = (served_dir / "mesh-comments.log").read_text().splitlines()
    assert len(log_lines) == n
    for line in log_lines:
        entry = json.loads(line)
        assert entry["count"] == len(entry["annotations"])

    assert list(served_dir.glob(".mesh-comments-*.tmp")) == []


async def test_submit_writes_comments_json_not_mode_0600(client, served_dir):
    """mkstemp's 0600 default is widened before os.replace so the record stays readable."""
    res = await client.post("/submit", body=[_pin(1)])
    assert res.status_code == 200

    mode = (served_dir / "mesh-comments.json").stat().st_mode & 0o777
    assert mode != 0o600
    assert mode == routes_viewer._RECORD_FILE_MODE
