# Releasing Annealage Mesh to PyPI

Annealage Mesh publishes to PyPI via GitHub Actions trusted publishing
(OIDC), so there's no API token to store. First-time setup, then it's one
release per version.

## One-time setup

1. On PyPI, add a "pending publisher" for a new project (Account → Publishing):
   - PyPI project name: `annealage-mesh`
   - Owner: `Annealage`
   - Repository: `mesh`
   - Workflow: `publish.yml`
   - Environment: leave blank (the workflow doesn't use one)

That's it - PyPI now trusts this repo's `publish.yml` to upload `annealage-mesh`.

## Cutting a release

1. Bump `version` in `pyproject.toml` and `__version__` in `src/annealage_mesh/__init__.py`.
2. Commit, tag it (`git tag v0.1.0 && git push --tags`).
3. Create a GitHub Release for that tag. The `publish` workflow builds and
   uploads to PyPI automatically.

After the first successful publish, install shortens to `uvx annealage-mesh` /
`pipx install annealage-mesh` / `uv tool install annealage-mesh`.
