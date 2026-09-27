# Releasing Annealage Mesh to PyPI

Annealage Mesh publishes to PyPI via GitHub Actions trusted publishing
(OIDC), so there's no API token to store. First-time setup, then it's one
release per version.

**The agent package ships inside the wheel, and always will.** Mesh's agent
side is the separate `annealage-agent` package, which the Annealage products
take as a git submodule and never from an index, and a published wheel can't
depend on a path. So a Mesh wheel carries it: `hatch_build.py` copies
`annealage_agent` from the `lib/agent` submodule, at the commit its gitlink
pins, into the wheel beside `annealage_mesh`, and `pyproject.toml` declares
the agent's runtime dependencies as Mesh's own. The published metadata names
no `annealage-agent`. The build refuses to run with `lib/agent` empty, or
while `pyproject.toml` lacks one of the agent's dependencies or its codex
extra's (`tools/bump-agent.sh` lists them after a bump). A release therefore
ships whatever agent commit `lib/agent` pins: bump it first
(`tools/bump-agent.sh`, CONTRIBUTING.md) if the release should carry newer
agent work, and push that agent commit to GitHub before tagging, since the
publish workflow checks the submodule out from there.

The installed wheel puts a top-level `annealage_agent` package into the
environment with no distribution of its own. Installed beside another copy (a
second product bundling the agent the same way, or `annealage-agent` itself),
whichever installs last overwrites the other's files, and uninstalling either
removes them for both. `uvx` and `uv tool install` give each tool its own
environment, which avoids it.

The version is not written down anywhere: `hatch-vcs` takes it from the git
tag at build time, and `annealage_mesh.__version__` reads it back out of the
installed package's metadata. So the tag is the version, and there is nothing
to keep in step with it.

## One-time setup

1. On PyPI, add a "pending publisher" for a new project (Account → Publishing):
   - PyPI project name: `annealage-mesh`
   - Owner: `Annealage`
   - Repository: `mesh`
   - Workflow: `publish.yml`
   - Environment: leave blank (the workflow doesn't use one)

That's it - PyPI now trusts this repo's `publish.yml` to upload `annealage-mesh`.

## Cutting a release

1. Tag it and push the tag: `git tag v2.0.1 && git push origin v2.0.1`.
2. Create a GitHub Release for that tag. The `publish` workflow runs the whole
   test suite against that commit, builds, checks that what it built carries the
   tag's version, and uploads to PyPI.

A version on PyPI cannot be replaced once uploaded, which is why the suite runs
inside the publish workflow rather than being trusted from an earlier run.

## Checking a build before tagging

`uv build` writes an sdist and a wheel into `dist/`. Two things are worth
checking by hand when the packaging itself has changed:

    uv build
    uvx twine check dist/*
    # the static assets actually shipped, since a gitignore pattern has
    # silently dropped them before (see the artifacts note in pyproject.toml)
    unzip -l dist/*.whl | grep -c static/
    # the agent package is in the wheel, and its metadata doesn't require it
    unzip -l dist/*.whl | grep -c annealage_agent/
    unzip -p dist/*.whl '*.dist-info/METADATA' | grep '^Requires-Dist: annealage'   # prints nothing

The version in those filenames will be a dev version (`1.0.1.dev34`) unless you
are exactly on a tag. That is expected: `local_scheme = "no-local-version"`
keeps it uploadable, but only a tagged build produces a release version.

## Notes for the 2.0.0 release

The default bind changed from every interface to loopback. Non-loopback binding
is still fully supported and needs no more than `--host`, including the
`--host tailscale` alias, but reaching the tool from another machine is now a
decision rather than a side effect of starting it. Anyone relying on the old
default has to pass `--host` explicitly.

`--no-agent` still works and still means viewer-only, but `annealage-mesh view`
is the spelling to prefer, and the published skill now uses it.
