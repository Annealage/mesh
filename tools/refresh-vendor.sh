#!/bin/sh
# Re-download the vendored browser dependencies and rewrite VERSIONS.json.
#
# Usage: tools/refresh-vendor.sh [THREE_VERSION]
#
# Files are copied byte-for-byte from the upstream release. Nothing is
# rewritten: the addons import the bare specifier "three", which the
# importmap in viewer.html resolves to the local three.module.js, so the
# hashes recorded here stay directly comparable against upstream.
#
# After running this, `pytest tests/test_vendor.py` confirms the tree and
# VERSIONS.json agree, and the viewer must be loaded once by hand, because a
# three.js minor release can change addon behaviour in ways no Python test
# sees.
set -eu

THREE_VERSION="${1:-0.160.0}"
BASE="https://cdn.jsdelivr.net/npm/three@${THREE_VERSION}"

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
vendor="$root/src/annealage_mesh/static/js/vendor"
mkdir -p "$vendor"

# local name : upstream path, one pair per line
files="three.module.js:build/three.module.js
OrbitControls.js:examples/jsm/controls/OrbitControls.js
STLLoader.js:examples/jsm/loaders/STLLoader.js
LICENSE:LICENSE"

entries=""
printf '%s\n' "$files" | while IFS=: read -r local upstream; do
    printf 'fetching %s\n' "$upstream" >&2
    curl -sSfL --max-time 120 -o "$vendor/$local" "$BASE/$upstream"
done

# Second pass so the JSON is only written once every download has succeeded;
# a partial refresh must not leave a VERSIONS.json describing a mixture.
printf '%s\n' "$files" | while IFS=: read -r local upstream; do
    sha=$(sha256sum "$vendor/$local" | cut -d' ' -f1)
    bytes=$(wc -c < "$vendor/$local" | tr -d ' ')
    printf '        {\n'
    printf '          "local": "%s",\n' "$local"
    printf '          "upstream": "%s",\n' "$upstream"
    printf '          "bytes": %s,\n' "$bytes"
    printf '          "sha256": "%s"\n' "$sha"
    printf '        },\n'
done > "$vendor/.entries.tmp"

# Drop the trailing comma from the last entry.
sed '$ s/},$/}/' "$vendor/.entries.tmp" > "$vendor/.entries"
rm -f "$vendor/.entries.tmp"

{
    cat <<'HEAD'
{
  "_comment": [
    "Provenance for the vendored browser dependencies in this directory.",
    "Every file here is a byte-for-byte copy of an upstream release, never",
    "edited: the addons import the bare specifier 'three', which the",
    "importmap in viewer.html resolves to three.module.js in this directory,",
    "so no rewriting is needed and these hashes stay comparable against",
    "upstream. Refresh with tools/refresh-vendor.sh, which rewrites this",
    "file; tests/test_vendor.py fails if what is on disk stops matching it."
  ],
  "packages": [
    {
      "name": "three",
HEAD
    printf '      "version": "%s",\n' "$THREE_VERSION"
    cat <<'MID'
      "license": "MIT",
      "license_file": "LICENSE",
      "homepage": "https://threejs.org/",
MID
    printf '      "base_url": "%s",\n' "$BASE"
    printf '      "files": [\n'
    cat "$vendor/.entries"
    printf '      ]\n'
    printf '    }\n'
    printf '  ]\n'
    printf '}\n'
} > "$vendor/VERSIONS.json"
rm -f "$vendor/.entries"

printf 'wrote %s\n' "$vendor/VERSIONS.json" >&2
