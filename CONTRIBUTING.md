# Contributing to Annealage Mesh

Thanks for your interest. Read the licensing section before submitting a patch; it sets out the grant you make to the project by submitting.

## Commit messages

```
<Capitalised subject in the imperative, under 72 characters>

Optional body explaining what the change makes true and why, wrapped at
75 characters per line.

Signed-off-by: Your Name <you@example.com>
```

## Pull requests

- Rebase onto current `main` before submitting; do not merge `main` into your branch.
- One logical change per pull request. Small focused PRs are easier to review and revert.
- Include a test or a reproducer in the same PR where it is reasonable to do so.
- Run `uv run --extra dev pytest -q` locally before pushing. uv installs `annealage-ember` from the git submodule `lib/ember` (`[tool.uv.sources]` in `pyproject.toml`), so clone with `--recurse-submodules`, or run `git submodule update --init` in an existing clone. The omp backend's tests also need `omp-rpc`, which isn't on PyPI and which `uv sync` removes: install it the way CI does (`.github/workflows/test.yml`).
- The agent package is developed in its own repository, [Annealage/ember](https://github.com/Annealage/ember); `lib/ember` is a pinned checkout of it, not a place to make changes. To take a new agent commit into Mesh, commit it in your clone of that repository, run `tools/bump-ember.sh [REV]` (REV defaults to `main`, fetched from `$EMBER_DEV`, default `~/studio/ember`, pushed or not; it checks the commit out in `lib/ember` and stages the gitlink), and commit the bump. CI can only check the commit out once it's pushed to GitHub.
- The brand styling under `src/annealage_mesh/style/` is copied from the private `Annealage/style` repository and never edited by hand (it is not a submodule). To update it, run `tools/vendor-style.sh <mesh>/src/annealage_mesh/style --ref <sha>` from a clean style checkout with `<sha>` on its `origin/main` (it needs access to that repository, and reads `src/annealage_mesh/vendor-style.exclude` for what Mesh leaves out) and commit the result with its `VENDORED.json`. `tests/test_vendored_style.py` checks the files against `VENDORED.json`, and against the style checkout in `$STYLE_DEV` (default `~/studio/style`) when there is one; the hash check needs no checkout. The look lives in those vendored files; `static/css/app.css` only lays out what Mesh alone draws (the canvas, the sketch overlay, part swatches, the measure readout, the toast).
- The page's visual gate runs from a style checkout, since its tools are not vendored. Install them once with `npm --prefix ~/studio/style/tools ci`. Serve a folder of three STL parts (`annealage-mesh <folder> --no-open --no-git --backend <name> --port 5733`, and a second server with `--no-agent` for the `mesh-viewer-only` entry), then run `~/studio/style/tools/shot --table tests/shots.mjs --base http://127.0.0.1:5733 --webgl --preset smoke --conform` per change and `--preset full` before a merge, with `MESH_SHOT_TOKEN` set to the `#t=` value the server printed. `tests/shots.mjs` lists which entries need which folder. Accepted shots are in `shots/accepted/`. Check the static rules with `~/studio/style/tools/check src/annealage_mesh/static --style ~/studio/style`.
- Install the pre-commit hooks once per checkout (`uv run --extra dev pre-commit install`), so `ruff check --fix` and `ruff format` run automatically on `git commit` and a commit message without a `Signed-off-by:` line is rejected. CI's `lint` job runs the identical ruff hooks (`.pre-commit-config.yaml`) and fails a PR where they were skipped.

## Contribution licensing

Annealage Mesh is offered under the PolyForm Noncommercial License 1.0.0, except for the Claude Code skill under `skill/annealage-mesh/`, which is offered under the MIT License, and the vendored three.js files under `src/annealage_mesh/static/js/vendor/`, which are the three.js authors' work under the MIT License. Andrew Leech also offers commercial licences to organisations whose use is not permitted under the PolyForm Noncommercial License. For that dual model to work, contributions need a clear licensing grant; otherwise contributed code could not be offered under a commercial licence without re-asking every contributor.

By submitting a contribution (a pull request, patch, or any change) to this project, you agree to the following.

### Developer Certificate of Origin

You certify the contribution under the Developer Certificate of Origin 1.1 (<https://developercertificate.org/>). Sign off each commit with:

```
Signed-off-by: Your Name <your.email@example.com>
```

(`git commit -s` adds this line.) The sign-off certifies that you wrote the contribution or otherwise have the right to submit it under the terms below. It is mandatory: the `verifygitlog` CI workflow checks every commit in a pull request, and every commit pushed to `main`, and fails if any lacks a `Signed-off-by:` line.

### Licence grant

You grant Andrew Leech a perpetual, worldwide, irrevocable, royalty-free, sublicensable, and transferable licence to use, reproduce, modify, distribute, and relicense your contribution, in whole or in part, under any terms, including the PolyForm Noncommercial License and any commercial licence Andrew Leech offers, now or in future.

You confirm you have the right to grant this licence (the contribution is your own work, or you are authorised to submit it under these terms). The grant is royalty-free: no payment is due to you for it.

### Outbound licence

Your contribution is also made available to the public under the outbound licence of the path it touches: MIT for `skill/annealage-mesh/`, PolyForm Noncommercial 1.0.0 everywhere else. The vendored three.js files under `src/annealage_mesh/static/js/vendor/` stay under the three.js authors' MIT License, and the vendored brand styling under `src/annealage_mesh/style/` (fonts under the SIL OFL 1.1) stays as it is upstream. Change them upstream, not here. Your own rights to your contribution are not otherwise affected; you retain copyright in your work.

### Other terms

If you cannot grant the licence above, for example, your employer owns the work and has not authorised this grant, or you require a separate contributor agreement, contact andrew@alelec.net before submitting, so alternative arrangements can be made.
