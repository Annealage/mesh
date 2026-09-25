"""Unit tests for the model scan in ``paths.py``, independent of any HTTP
route.

These exercise ``scan_models``, ``_compute_labels`` and ``ModelIndex``
directly against a real filesystem tree (or, for the label algorithm, against
a synthetic list of ``rel`` values with no filesystem involved at all), so a
defect in the scan itself is pinned at the layer it lives in rather than only
visible through a route's response. ``tests/test_routes_viewer.py`` covers
the same contracts again through microdot's ``TestClient``, proving the
routes actually wire this module's results through to a client rather than
only that the module itself is correct. The agent layer's static scan and file
creation (``annealage_agent.files``) are tested in annealage-agent's suite.
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
    for label in labels.values():
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
