"""Packaging contract tests: what actually ships in the published release.

Exercises plain ``uv build`` against the real project tree, the same
invocation ``.github/workflows/publish.yml`` uses to release: it builds an
sdist and then builds the wheel from that sdist, not from the working tree.
A ``--wheel``-only build exercises a different hatchling code path and would
not have caught the sdist target missing the same artifact override the
wheel target has, so both built files are inspected here rather than parsing
``pyproject.toml``, since the property under test is hatchling's own
VCS-ignore filtering of package contents, not the config file's syntax.
"""

import shutil
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = REPO_ROOT / "src" / "annealage_mesh" / "static"

pytestmark = pytest.mark.skipif(shutil.which("uv") is None, reason="uv is not on PATH")


def _build(out_dir):
    """Build the sdist and, from it, the wheel; return (sdist_names, wheel_names).

    ``sdist_names`` are ``tarfile`` member names, each prefixed with the
    ``<project>-<version>/`` directory hatchling wraps the sdist contents
    in. ``wheel_names`` are ``zipfile`` member names, unprefixed.
    """
    subprocess.run(
        ["uv", "build", "-o", str(out_dir), str(REPO_ROOT)],
        check=True,
        capture_output=True,
        text=True,
    )
    sdists = list(Path(out_dir).glob("*.tar.gz"))
    wheels = list(Path(out_dir).glob("*.whl"))
    assert len(sdists) == 1, sdists
    assert len(wheels) == 1, wheels
    with tarfile.open(sdists[0]) as tf:
        sdist_names = tf.getnames()
    with zipfile.ZipFile(wheels[0]) as zf:
        wheel_names = zf.namelist()
    return sdist_names, wheel_names


def test_wheel_ships_the_packaged_viewer(tmp_path):
    _, wheel_names = _build(tmp_path / "dist")
    assert "annealage_mesh/static/viewer.html" in wheel_names


def test_sdist_ships_the_packaged_viewer(tmp_path):
    sdist_names, _ = _build(tmp_path / "dist")
    assert any(n.endswith("src/annealage_mesh/static/viewer.html") for n in sdist_names)


def test_wheel_and_sdist_ship_the_split_front_end_and_vendored_three_js(tmp_path):
    # The packaged front end is a shell plus a tree of ES modules plus a
    # vendored three.js, and the shell is inert without them: a hatchling
    # include pattern matching only viewer.html would ship an importmap and a
    # module script pointing at files that are not there, with no test
    # noticing until someone opened it in a browser.
    sdist_names, wheel_names = _build(tmp_path / "dist")
    for rel in (
        "static/css/app.css",
        "static/js/main.js",
        "static/js/vendor/three.module.js",
        "static/js/vendor/VERSIONS.json",
    ):
        assert any(n.endswith("src/annealage_mesh/" + rel) for n in sdist_names), rel
        assert "annealage_mesh/" + rel in wheel_names, rel


def test_sdist_and_wheel_ship_a_static_file_under_a_gitignored_directory_name(tmp_path):
    # "lib/" is one of this repo's .gitignore entries (the standard
    # Python-template line for a venv's lib/ directory). A vendored asset
    # tree living under static/js/lib/, the layout a later milestone adds,
    # must still ship despite that name collision, in both the sdist and
    # the wheel built from it. Writes a throwaway probe file into the real
    # static/ tree for the duration of one build, since the property under
    # test is hatchling's filtering against the repo's actual .gitignore,
    # not a copy of it.
    probe_dir = STATIC_DIR / "lib"
    probe_dir.mkdir()
    probe_file = probe_dir / "probe.js"
    probe_file.write_text("// packaging test probe, safe to delete\n")
    try:
        sdist_names, wheel_names = _build(tmp_path / "dist")
    finally:
        probe_file.unlink()
        probe_dir.rmdir()
    assert any(n.endswith("src/annealage_mesh/static/lib/probe.js") for n in sdist_names)
    assert "annealage_mesh/static/lib/probe.js" in wheel_names


def test_the_version_is_whatever_the_installed_metadata_says():
    """`__version__` is read from the installed package's own metadata rather
    than written down, and hatch-vcs derives that from the git tag at build
    time. This pins the one thing that could silently drift: a fallback path
    that reported something other than what was installed, which would make the
    banner, `--version`, the Server header and the diagnostics block all lie in
    the same way at once.
    """
    from importlib.metadata import version

    import annealage_mesh

    assert annealage_mesh.__version__ == version("annealage-mesh")


def test_the_version_is_not_the_unknown_fallback_in_a_normal_install():
    """The fallback exists for running straight from a checkout with nothing
    installed. Reaching it in the test environment would mean the package under
    test is not the package being imported."""
    import annealage_mesh

    assert annealage_mesh.__version__ != "0.0.0+unknown"
