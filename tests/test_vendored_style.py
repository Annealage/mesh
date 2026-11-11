"""Provenance tests for the vendored brand styling.

``src/annealage_mesh/style/`` holds files copied from Annealage/style by that
repository's ``tools/vendor-style.sh``, never edited here; ``VENDORED.json``
records the commit and the sha256 of every file. The first test needs nothing
but this tree. The second runs the script's own ``--check`` against a style
checkout when one exists (``$STYLE_DEV``, default ``~/studio/style``), and
skips otherwise: public CI has no such checkout and no network.
"""

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
STYLE_DIR = REPO_ROOT / "src" / "annealage_mesh" / "style"
MANIFEST = STYLE_DIR / "VENDORED.json"


def test_vendored_files_match_the_recorded_hashes():
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert manifest["commit"]
    recorded = {f["path"]: f["sha256"] for f in manifest["files"]}
    assert recorded
    on_disk = {
        p.relative_to(STYLE_DIR).as_posix()
        for p in STYLE_DIR.rglob("*")
        if p.is_file() and p != MANIFEST
    }
    assert on_disk == set(recorded), sorted(on_disk ^ set(recorded))
    for rel, digest in recorded.items():
        assert hashlib.sha256((STYLE_DIR / rel).read_bytes()).hexdigest() == digest, rel


def test_vendored_files_match_the_style_checkout():
    checkout = Path(os.environ.get("STYLE_DEV", "~/studio/style")).expanduser()
    script = checkout / "tools" / "vendor-style.sh"
    if not script.is_file():
        pytest.skip("no style checkout at %s (set STYLE_DEV)" % checkout)
    result = subprocess.run(
        [str(script), str(STYLE_DIR), "--check"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
