"""Provenance tests for the vendored browser dependencies.

``static/js/vendor/`` holds byte-for-byte copies of upstream three.js
release files, never edited; ``VERSIONS.json`` in the same directory records
each file's expected size and sha256, and ``tools/refresh-vendor.sh`` is the
only thing meant to write either the files or that record. These tests catch
the two ways the two can drift apart: a file edited or replaced without
regenerating VERSIONS.json, and a file added to or removed from the
directory without updating the record to match, in either direction.
"""

import hashlib
import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
VENDOR_DIR = REPO_ROOT / "src" / "annealage_mesh" / "static" / "js" / "vendor"
VERSIONS_JSON = VENDOR_DIR / "VERSIONS.json"
REUSE_TOML = REPO_ROOT / "REUSE.toml"


def _versions():
    with open(VERSIONS_JSON, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _all_file_entries():
    entries = []
    for pkg in _versions()["packages"]:
        entries.extend(pkg["files"])
    return entries


def test_versions_json_lists_at_least_one_package_with_files():
    packages = _versions()["packages"]
    assert packages
    for pkg in packages:
        assert pkg["files"]


def test_every_recorded_file_matches_its_byte_count_and_sha256_on_disk():
    for entry in _all_file_entries():
        local = VENDOR_DIR / entry["local"]
        assert local.is_file(), "recorded in VERSIONS.json but missing on disk: %s" % entry["local"]
        on_disk_bytes = local.stat().st_size
        assert on_disk_bytes == entry["bytes"], (
            "%s is %d bytes on disk, VERSIONS.json records %d"
            % (entry["local"], on_disk_bytes, entry["bytes"])
        )
        on_disk_sha = _sha256(local)
        assert on_disk_sha == entry["sha256"], (
            "%s sha256 on disk (%s) does not match VERSIONS.json (%s)"
            % (entry["local"], on_disk_sha, entry["sha256"])
        )


def test_file_set_matches_versions_json_in_both_directions():
    recorded = {entry["local"] for entry in _all_file_entries()}
    on_disk = {p.name for p in VENDOR_DIR.iterdir() if p.is_file() and p.name != "VERSIONS.json"}
    # Every file VERSIONS.json describes is actually present (nothing
    # deleted without updating the record)...
    assert recorded <= on_disk, "recorded but absent from disk: %s" % (recorded - on_disk)
    # ...and every file present in the tree is described by the record
    # (nothing added, e.g. a new addon, without regenerating VERSIONS.json).
    assert on_disk <= recorded, "present on disk but not recorded: %s" % (on_disk - recorded)


def test_recorded_local_names_have_no_duplicates_and_no_path_separators():
    # "local" is joined onto VENDOR_DIR directly by the file-set check above
    # and, in tools/refresh-vendor.sh, by a plain curl -o; a path separator
    # snuck into one would let a future refresh write outside this directory.
    locals_ = [entry["local"] for entry in _all_file_entries()]
    assert len(locals_) == len(set(locals_))
    for name in locals_:
        assert "/" not in name and "\\" not in name


def test_reuse_toml_carries_an_mit_override_for_the_vendor_directory():
    text = REUSE_TOML.read_text(encoding="utf-8")
    # A minimal scan rather than a full TOML parse (tomllib is 3.11+ only,
    # and this repository's floor is 3.10): find the annotation block whose
    # path targets the vendor directory and check it declares an MIT
    # override rather than inheriting the repository's own PolyForm licence.
    blocks = re.split(r"\[\[annotations\]\]", text)
    vendor_blocks = [b for b in blocks if "static/js/vendor" in b]
    assert vendor_blocks, "no [[annotations]] block targets static/js/vendor in REUSE.toml"
    block = vendor_blocks[0]
    assert re.search(r'precedence\s*=\s*"override"', block)
    assert re.search(r'SPDX-License-Identifier\s*=\s*"MIT"', block)


def test_vendored_files_are_never_edited_by_this_repo_no_stray_local_marker():
    # tools/refresh-vendor.sh copies upstream bytes verbatim; a marker like
    # "annealage" or a repo-relative import path in a vendored file would
    # mean something here hand-edited a copy that is supposed to be
    # byte-for-byte, which breaks the hash comparability the module
    # docstring in VERSIONS.json relies on.
    for entry in _all_file_entries():
        local = VENDOR_DIR / entry["local"]
        if local.suffix != ".js":
            continue
        text = local.read_text(encoding="utf-8", errors="replace")
        assert "annealage" not in text.lower()
