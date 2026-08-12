"""Unit tests for the model and static scans in ``paths.py``, independent of
any HTTP route.

These exercise ``scan_models``, ``_compute_labels``, ``ModelIndex``,
``scan_static`` and ``StaticIndex`` directly against a real filesystem tree
(or, for the label algorithm, against a synthetic list of ``rel`` values with
no filesystem involved at all), so a defect in the scan itself is pinned at
the layer it lives in rather than only visible through a route's response.
``tests/test_routes_viewer.py`` covers the same contracts again through
microdot's ``TestClient``, proving the routes actually wire this module's
results through to a client rather than only that the module itself is
correct.
"""

import os
import random

import pytest

from annealage_mesh import paths


# --- scan_models: recursion, exclusions, determinism ----------------------

def test_scan_models_finds_a_model_nested_several_directories_deep(tmp_path):
    deep = tmp_path / "models" / "a" / "sub"
    deep.mkdir(parents=True)
    (deep / "bracket.stl").write_bytes(b"solid bracket\nendsolid bracket\n")

    models, truncated = paths.scan_models(tmp_path)
    rels = {m["rel"] for m in models}
    assert "models/a/sub/bracket.stl" in rels
    assert truncated is False


@pytest.mark.parametrize("dirname", [".hidden", ".git", ".mesh"])
def test_scan_models_excludes_a_dotdir_nested_under_a_real_directory(tmp_path, dirname):
    nested_dotdir = tmp_path / "models" / dirname
    nested_dotdir.mkdir(parents=True)
    (nested_dotdir / "excluded.stl").write_bytes(b"solid x\nendsolid x\n")

    models, _ = paths.scan_models(tmp_path)
    rels = {m["rel"] for m in models}
    assert not any(dirname in rel.split("/") for rel in rels)


def test_scan_models_does_not_descend_a_symlinked_directory(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (real / "real.stl").write_bytes(b"solid r\nendsolid r\n")
    (tmp_path / "linked").symlink_to(real, target_is_directory=True)

    models, truncated = paths.scan_models(tmp_path)
    rels = {m["rel"] for m in models}
    assert rels == {"real/real.stl"}
    assert truncated is False


def test_scan_models_refuses_a_symlinked_model_at_any_depth(tmp_path):
    sub = tmp_path / "models"
    sub.mkdir()
    (sub / "widget.stl").write_bytes(b"solid widget\nendsolid widget\n")
    (sub / "alias.stl").symlink_to(sub / "widget.stl")

    models, _ = paths.scan_models(tmp_path)
    rels = {m["rel"] for m in models}
    assert "models/widget.stl" in rels
    assert "models/alias.stl" not in rels


def test_scan_models_refuses_a_hardlinked_model_at_any_depth(tmp_path, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    secret = outside / "secret.stl"
    secret.write_bytes(b"solid secret\nendsolid secret\n")
    sub = tmp_path / "models"
    sub.mkdir()
    try:
        os.link(secret, sub / "linked.stl")
    except OSError as exc:
        pytest.skip("cannot hardlink across these directories: %s" % exc)

    models, _ = paths.scan_models(tmp_path)
    rels = {m["rel"] for m in models}
    assert "models/linked.stl" not in rels


def _make_chain(base, depth):
    """Create ``depth`` nested directories under ``base``, named d1..dN, with
    a model file at the bottom. Returns the model's ``rel`` from ``base``."""
    cur = base
    parts = []
    for i in range(1, depth + 1):
        cur = cur / ("d%d" % i)
        parts.append("d%d" % i)
    cur.mkdir(parents=True)
    (cur / "leaf.stl").write_bytes(b"solid leaf\nendsolid leaf\n")
    return "/".join(parts + ["leaf.stl"])


def test_scan_models_max_scan_depth_still_opens_a_directory_exactly_at_the_cap(tmp_path):
    # The served directory itself is depth 0, so a chain exactly
    # MAX_SCAN_DEPTH directories long is a *child* at depth MAX_SCAN_DEPTH,
    # not past it, and must still be opened.
    rel = _make_chain(tmp_path, paths.MAX_SCAN_DEPTH)

    models, truncated = paths.scan_models(tmp_path)
    rels = {m["rel"] for m in models}
    assert rel in rels
    assert truncated is False


def test_scan_models_max_scan_depth_skips_a_directory_past_the_cap_and_truncates(tmp_path):
    rel = _make_chain(tmp_path, paths.MAX_SCAN_DEPTH + 1)

    models, truncated = paths.scan_models(tmp_path)
    rels = {m["rel"] for m in models}
    assert rel not in rels
    assert truncated is True


def test_scan_models_max_scan_dirs_stops_descending_deterministically(tmp_path, monkeypatch):
    # Three sibling directories, each with one model. With the served
    # directory itself counting as the first directory opened, a cap of 2
    # leaves room for exactly one of the three siblings, and sorted traversal
    # makes it the alphabetically first one, not an arbitrary one.
    monkeypatch.setattr(paths, "MAX_SCAN_DIRS", 2)
    for name in ("d0", "d1", "d2"):
        sub = tmp_path / name
        sub.mkdir()
        (sub / "x.stl").write_bytes(b"solid x\nendsolid x\n")

    models, truncated = paths.scan_models(tmp_path)
    rels = {m["rel"] for m in models}
    assert rels == {"d0/x.stl"}
    assert truncated is True


def test_scan_models_ordering_is_deterministic_across_repeated_scans(tmp_path):
    (tmp_path / "c.stl").write_bytes(b"solid c\nendsolid c\n")
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "z.stl").write_bytes(b"solid z\nendsolid z\n")
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "y.stl").write_bytes(b"solid y\nendsolid y\n")

    first = paths.scan_models(tmp_path)
    second = paths.scan_models(tmp_path)
    assert first == second

    models, _ = first
    assert [m["rel"] for m in models] == sorted(m["rel"] for m in models)


# --- _compute_labels: the three brief examples plus a property-style check ---

def test_compute_labels_two_directories_sharing_a_basename():
    models = [{"rel": "a/widget.stl"}, {"rel": "b/widget.stl"}]
    labels = paths._compute_labels(models)
    assert labels == {"a/widget.stl": "a/widget", "b/widget.stl": "b/widget"}


def test_compute_labels_a_short_path_forced_wider_by_a_longer_colliding_one():
    models = [{"rel": "x/foo.stl"}, {"rel": "y/x/foo.stl"}]
    labels = paths._compute_labels(models)
    assert labels == {"x/foo.stl": "x/foo", "y/x/foo.stl": "y/x/foo"}


def test_compute_labels_falls_back_to_full_rel_when_only_the_extension_differs():
    # "a/b.stl" and "a/b.STL" produce the identical string at every k, since
    # Path.stem strips the extension before the two are compared; the
    # per-entry search alone cannot see this, only the second, set-wide pass.
    models = [{"rel": "a/b.stl"}, {"rel": "a/b.STL"}]
    labels = paths._compute_labels(models)
    assert labels == {"a/b.stl": "a/b.stl", "a/b.STL": "a/b.STL"}


def test_compute_labels_empty_input():
    assert paths._compute_labels([]) == {}


def test_compute_labels_a_single_entry_uses_its_shortest_form():
    models = [{"rel": "widget.stl"}]
    assert paths._compute_labels(models) == {"widget.stl": "widget"}


def _adversarial_rels(seed, n=60):
    """A generated, deliberately collision-prone set of ``rel`` values.

    Draws directory segments and leaf stems from a small shared vocabulary so
    many entries agree on their last one or two segments, which is what
    forces the label search past its smallest candidate k, and mixes in both
    .stl and .STL so some entries can only be told apart by the extension the
    per-entry search strips off. Some stems (``"b.stl"``, ``"widget.STL"``)
    already contain a model extension, so a stem-plus-extension leaf name
    such as ``"b.stl.stl"`` exists in the generated tree: stripping only the
    outermost extension from that leaf can produce a short label identical
    to another entry's full ``rel`` (``"a/b.stl"`` is both a plausible rel
    and a plausible label for ``"q/a/b.stl.stl"``), which is the shape that
    makes the rel-verbatim fallback collide with an already-chosen label
    rather than only with another fallback.
    """
    rnd = random.Random(seed)
    vocab = ["a", "b", "c", "x", "y", "part"]
    stems = ["widget", "b", "foo", "part", "b.stl", "widget.STL"]
    rels = set()
    while len(rels) < n:
        depth = rnd.randint(1, 4)
        parts = [rnd.choice(vocab) for _ in range(depth)]
        stem = rnd.choice(stems)
        ext = rnd.choice([".stl", ".STL"])
        rels.add("/".join(parts + [stem + ext]))
    return sorted(rels)


@pytest.mark.parametrize("seed", range(8))
def test_compute_labels_set_uniqueness_holds_over_a_generated_adversarial_tree(seed):
    rels = _adversarial_rels(seed)
    models = [{"rel": r} for r in rels]
    labels = paths._compute_labels(models)

    assert set(labels) == set(rels)
    assert len(set(labels.values())) == len(labels)
    for rel, label in labels.items():
        assert label  # never empty


def test_compute_labels_fallback_collision_with_an_already_chosen_label():
    # "a/b.stl" and "a/b.STL" collide at every k and both fall back to their
    # own rel. "q/a/b.stl.stl" legitimately settles on the short label
    # "a/b.stl" (its leaf strips only the outer ".stl"), which is also the
    # literal rel "a/b.stl" falls back to: a single dedup pass that does not
    # re-check its own output would ship both under the label "a/b.stl".
    # "r/b.stl.stl" is a second, unrelated entry of the same shape so the
    # fixed point has to run more than once to reach a stable set.
    models = [
        {"rel": "a/b.stl"},
        {"rel": "a/b.STL"},
        {"rel": "q/a/b.stl.stl"},
        {"rel": "r/b.stl.stl"},
    ]
    labels = paths._compute_labels(models)
    assert len(set(labels.values())) == len(labels)


# --- ModelIndex: ambiguous basenames are excluded from the alias lookup ----

def test_model_index_ambiguous_basename_is_absent_from_by_file_and_identity(tmp_path, capsys):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "a" / "part.stl").write_bytes(b"solid a\nendsolid a\n")
    (tmp_path / "b" / "part.stl").write_bytes(b"solid b\nendsolid b\n")

    idx = paths.build_model_index(tmp_path)
    assert idx.by_file("part.stl") is None
    assert idx.identity_of_file("part.stl") is None
    # Still reachable by rel, individually, since rel does not collide.
    assert idx.by_rel("a/part.stl") == tmp_path / "a" / "part.stl"
    assert idx.by_rel("b/part.stl") == tmp_path / "b" / "part.stl"

    warning = capsys.readouterr().err
    assert "part.stl" in warning


def test_model_index_unambiguous_basename_still_resolves(tmp_path):
    (tmp_path / "widget.stl").write_bytes(b"solid widget\nendsolid widget\n")

    idx = paths.build_model_index(tmp_path)
    assert idx.by_file("widget.stl") == tmp_path / "widget.stl"
    assert idx.identity_of_file("widget.stl") is not None


def test_model_index_manifest_models_omits_private_scan_fields(tmp_path):
    (tmp_path / "widget.stl").write_bytes(b"solid widget\nendsolid widget\n")

    idx = paths.build_model_index(tmp_path)
    for m in idx.manifest_models:
        assert not any(k.startswith("_") for k in m)


# --- scan_static: extension allowlist, symlinks, the vendor/LICENSE carve-out --

def test_scan_static_indexes_only_allowed_extensions(tmp_path):
    (tmp_path / "app.css").write_text("body {}")
    (tmp_path / "main.js").write_text("console.log(1);")
    (tmp_path / "data.json").write_text("{}")
    (tmp_path / "index.html").write_text("<html></html>")
    (tmp_path / "notes.bak").write_text("not servable")
    (tmp_path / "source.map").write_text("not servable either")

    entries, truncated = paths.scan_static(tmp_path)
    rels = {e["rel"] for e in entries}
    assert rels == {"app.css", "main.js", "data.json", "index.html"}
    assert truncated is False


def test_scan_static_license_requires_a_vendor_ancestor_directory(tmp_path):
    (tmp_path / "LICENSE").write_text("bare license, no vendor dir")
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    (vendor / "LICENSE").write_text("vendored license")

    entries, _ = paths.scan_static(tmp_path)
    rels = {e["rel"] for e in entries}
    assert rels == {"vendor/LICENSE"}


def test_scan_static_excludes_a_dotdir(tmp_path):
    hidden = tmp_path / ".hidden"
    hidden.mkdir()
    (hidden / "file.js").write_text("console.log(1);")

    entries, _ = paths.scan_static(tmp_path)
    assert entries == []


def test_scan_static_refuses_a_symlink(tmp_path):
    real = tmp_path / "real.js"
    real.write_text("console.log('real');")
    (tmp_path / "evil.js").symlink_to(real)

    entries, _ = paths.scan_static(tmp_path)
    rels = {e["rel"] for e in entries}
    assert rels == {"real.js"}


def test_scan_static_does_not_refuse_a_hardlink(tmp_path):
    # Deliberately the opposite of scan_models's rule: uv and pip install
    # package files by hardlinking out of a wheel cache, so a legitimately
    # installed asset routinely has more than one link, and refusing it
    # would make a normal install unservable.
    one = tmp_path / "one.js"
    one.write_text("console.log(1);")
    try:
        os.link(one, tmp_path / "two.js")
    except OSError as exc:
        pytest.skip("cannot hardlink within this directory: %s" % exc)

    entries, truncated = paths.scan_static(tmp_path)
    rels = {e["rel"] for e in entries}
    assert rels == {"one.js", "two.js"}
    assert truncated is False


def test_scan_static_cap_truncates_and_warns(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(paths, "MAX_STATIC_FILES", 2)
    for i in range(5):
        (tmp_path / ("f%d.js" % i)).write_text("console.log(%d);" % i)

    entries, truncated = paths.scan_static(tmp_path)
    assert truncated is True
    assert len(entries) == 2
    assert "MAX_STATIC_FILES" in capsys.readouterr().err


# --- StaticIndex ------------------------------------------------------------

def test_static_index_content_type_of_known_and_extensionless_files():
    assert paths.StaticIndex.content_type_of("app.css") == paths.CONTENT_TYPES[".css"]
    assert paths.StaticIndex.content_type_of("main.js") == paths.CONTENT_TYPES[".js"]
    assert paths.StaticIndex.content_type_of("data.json") == paths.CONTENT_TYPES[".json"]
    assert paths.StaticIndex.content_type_of("vendor/LICENSE") == "text/plain; charset=utf-8"


def test_static_index_by_rel_and_identity_of_absent_key(tmp_path):
    idx = paths.build_static_index(tmp_path)
    assert idx.by_rel("does-not-exist.js") is None
    assert idx.identity_of("does-not-exist.js") is None


def test_build_static_index_resolves_present_files(tmp_path):
    (tmp_path / "main.js").write_text("console.log(1);")

    idx = paths.build_static_index(tmp_path)
    assert idx.by_rel("main.js") == tmp_path / "main.js"
    assert idx.identity_of("main.js") is not None


# --- create_image_file: the one path that creates a file from a model's input --

def test_create_image_file_writes_into_images_and_makes_the_directory(tmp_path):
    fd, target = paths.create_image_file(tmp_path, "front.png")
    try:
        os.write(fd, b"bytes")
    finally:
        os.close(fd)
    assert target == tmp_path / paths.IMAGES_DIRNAME / "front.png"
    assert target.read_bytes() == b"bytes"
    assert oct(target.stat().st_mode)[-3:] == "644"


@pytest.mark.parametrize("name", [
    "../escape.png",          # traversal
    "sub/front.png",          # a directory component
    ".hidden.png",            # a dotfile, which the scan excludes anyway
    "front.svg",              # an extension /asset would not serve
    "front",                  # no extension at all
    "",                       # nothing
    "front.png\x00.txt",      # a NUL, in case a lower layer truncates at it
])
def test_create_image_file_refuses_a_name_it_would_not_serve_back(tmp_path, name):
    """The whole containment check for a name that may come from the model, so
    it is a whitelist: a name accepted here is one /asset can hand back, and
    anything else is refused rather than sanitised into something adjacent."""
    assert paths.create_image_file(tmp_path, name) is None


def test_create_image_file_refuses_a_symlinked_images_directory(tmp_path):
    """A link named images/ makes this a writer into whatever it points at,
    which is a way to have this process create files anywhere its user can."""
    outside = tmp_path / "outside"
    outside.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    (project / paths.IMAGES_DIRNAME).symlink_to(outside, target_is_directory=True)

    assert paths.create_image_file(project, "front.png") is None
    assert list(outside.iterdir()) == []


def test_create_image_file_raises_rather_than_overwriting(tmp_path):
    """O_EXCL, so the caller has to choose another name. Every image here is
    evidence of what a part looked like at some moment, and a silent overwrite
    loses one."""
    fd, _target = paths.create_image_file(tmp_path, "front.png")
    os.close(fd)
    with pytest.raises(FileExistsError):
        paths.create_image_file(tmp_path, "front.png")


# --- atomic_replace ---------------------------------------------------------

def test_atomic_replace_keeps_an_existing_files_mode(tmp_path):
    """mkstemp creates 0600 and os.replace carries the mode across, so without
    the chmod a deliberate mode would silently narrow on every write."""
    target = tmp_path / "mesh-callouts.json"
    target.write_text("{}")
    os.chmod(target, 0o640)

    paths.atomic_replace(target, b"{\"annotations\": []}")
    assert oct(target.stat().st_mode)[-3:] == "640"
    assert target.read_text() == "{\"annotations\": []}"


def test_atomic_replace_gives_a_new_file_the_default_mode(tmp_path):
    target = tmp_path / "fresh.json"
    paths.atomic_replace(target, b"[]")
    assert oct(target.stat().st_mode)[-3:] == "644"


def test_atomic_replace_leaves_no_temporary_file_behind_on_failure(tmp_path):
    target = tmp_path / "record.json"
    with pytest.raises(TypeError):
        paths.atomic_replace(target, "a str, not bytes")
    assert list(tmp_path.iterdir()) == []
