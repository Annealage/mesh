#!/usr/bin/env bash
# Move lib/agent, the annealage-agent submodule, to another commit of the agent
# and stage it.
#
#   tools/bump-agent.sh [REV]
#
# REV (default main) is a commit in the agent's development clone, AGENT_DEV
# (~/studio/agent), the clone that pushes to GitHub; it reaches Mesh from
# there, pushed or not. The commit is fetched into lib/agent and checked out,
# and the new gitlink staged for a commit: it is the agent Mesh runs, and the
# one a release bundles. CI, and any other clone, can only check the commit
# out once it's pushed.
#
# A release declares the agent's dependencies as Mesh's own, and a wheel
# refuses to build while one is missing (hatch_build.py), so this ends with the
# same check: what the agent now requires that pyproject.toml doesn't declare.
set -euo pipefail

repo=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
agent_dev=${AGENT_DEV:-$HOME/studio/agent}
sub=$repo/lib/agent

die() { echo "bump-agent: $*" >&2; exit 1; }

(($# <= 1)) || die "usage: tools/bump-agent.sh [REV]"
rev=${1:-main}
[[ -e $sub/.git ]] || die "lib/agent is not checked out; run git submodule update --init lib/agent"
sha=$(git -C "$agent_dev" rev-parse --verify --quiet "$rev^{commit}") ||
    die "no commit $rev in $agent_dev"
[[ -z $(git -C "$sub" status --porcelain) ]] ||
    die "lib/agent has changes of its own; make them in $agent_dev and bump to that commit"

git -C "$sub" fetch -q "$agent_dev" "$sha"
git -C "$sub" checkout -q --detach "$sha"
git -C "$repo" add lib/agent
echo "bump-agent: lib/agent is at $(git -C "$sub" log -1 --format='%h %s'), staged"

status=0
missing=$(cd "$repo" && uv run -q --no-project --with hatchling python hatch_build.py) || status=$?
case $status in
    0) echo "bump-agent: pyproject.toml declares every requirement of the agent's" ;;
    3)
        echo "bump-agent: the agent requires what pyproject.toml doesn't declare; copy these in, or a wheel won't build:"
        sed 's/^/  /' <<<"$missing"
        ;;
    *) echo "bump-agent: couldn't compare the agent's dependencies with Mesh's (exit $status); a wheel build checks them too" >&2 ;;
esac
