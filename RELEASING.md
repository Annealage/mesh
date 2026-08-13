# Releasing Annealage Mesh to PyPI

Annealage Mesh publishes to PyPI via GitHub Actions trusted publishing
(OIDC), so there's no API token to store. First-time setup, then it's one
release per version.

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
