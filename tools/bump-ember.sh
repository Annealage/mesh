#!/usr/bin/env bash
# Move lib/ember, the annealage-ember submodule, to another commit of the agent
# and stage it.
#
#   tools/bump-ember.sh [REV]
#
# REV (default main) is a commit in the agent's development clone, EMBER_DEV
# (~/studio/ember), the clone that pushes to GitHub; it reaches Mesh from
# there, pushed or not. The commit is fetched into lib/ember and checked out,
# and the new gitlink staged for a commit with uv.lock (brought up to date): it is the agent Mesh runs, and the
# one a release bundles. CI, and any other clone, can only check the commit
# out once it's pushed.
#
# A release declares the agent's dependencies as Mesh's own, and a wheel
# refuses to build while one is missing (hatch_build.py), so this ends with the
# same check: what the agent now requires that pyproject.toml doesn't declare.
set -euo pipefail

repo=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
ember_dev=${EMBER_DEV:-$HOME/studio/ember}
sub=$repo/lib/ember

die() { echo "bump-ember: $*" >&2; exit 1; }

(($# <= 1)) || die "usage: tools/bump-ember.sh [REV]"
rev=${1:-main}
[[ -e $sub/.git ]] || die "lib/ember is not checked out; run git submodule update --init lib/ember"
sha=$(git -C "$ember_dev" rev-parse --verify --quiet "$rev^{commit}") ||
    die "no commit $rev in $ember_dev"
[[ -z $(git -C "$sub" status --porcelain) ]] ||
    die "lib/ember has changes of its own; make them in $ember_dev and bump to that commit"

git -C "$sub" fetch -q "$ember_dev" "$sha"
git -C "$sub" checkout -q --detach "$sha"
(cd "$repo" && uv lock -q)
git -C "$repo" add lib/ember uv.lock
echo "bump-ember: lib/ember is at $(git -C "$sub" log -1 --format='%h %s'), staged"

status=0
missing=$(cd "$repo" && uv run -q --no-project --with hatchling python hatch_build.py) || status=$?
case $status in
    0) echo "bump-ember: pyproject.toml declares every requirement of the agent's" ;;
    3)
        echo "bump-ember: the agent requires what pyproject.toml doesn't declare; copy these in, or a wheel won't build:"
        sed 's/^/  /' <<<"$missing"
        ;;
    *) echo "bump-ember: couldn't compare the agent's dependencies with Mesh's (exit $status); a wheel build checks them too" >&2 ;;
esac
