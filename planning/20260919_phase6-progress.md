# Phase 6 progress report - backend-aware diagnostics

Date: 2026-09-20
Status: **COMPLETE**. Final phase of the roadmap. Ticket
`planning/tickets/phase6_diagnostics.md` executed via the Dynamic Workflow
tiering, one fix-loop iteration, now clean.

## Scope correction at phase entry

Revalidating the ticket at phase entry found its actual scope had already
shrunk to almost nothing: `diagnostics.py`'s `collect()` already gates
`codex_cli`/`omp_cli` facts on `backend`, `cli.py`'s `diagnostics_report()`
already formats both new fact shapes, and `http/routes_settings.py` already
threads `backend`/`local_base_url` through to the same `collect()` call -
all landed as a side effect of Phase 3's and Phase 4's own implementers
proactively wiring their own collectors' call sites rather than leaving that
for this ticket. Confirmed via grep that `tests/test_diagnostics.py` had
zero coverage of any of it. This phase's real scope was entirely test
coverage, not implementation - the ticket's original "Approach sketch"
(different key names, a `base_url=` kwarg) was already stale against the
real landed shapes (`codex_cli`/`omp_cli` keys, `_omp_info`'s positional
`local_base_url` param) and documented as historical in the revalidation
stamp rather than corrected line-by-line.

## What landed

14 new tests in `tests/test_diagnostics.py` (no production code changed),
covering the ticket's three acceptance-criteria bullets:

- One case per backend proving `codex_cli` is present only for
  `backend="codex"` and `omp_cli` only for `backend="local"` (via `in`/`not
  in` membership checks, not falsy checks), plus the `backend=None`/
  `"claude"` cases keeping `claude_cli`'s pre-existing unconditional
  behavior.
- Missing-dependency/unreachable-endpoint cases for both new backends,
  proving `collect()` returns cleanly with a `"source": "missing"`-shaped
  fact rather than raising.
- Doctor-vs-`GET /settings` parity for `backend="codex"`/`"local"`.

## Review loop

One review pass, two findings, both priority-2, both about tests not
actually exercising the real code paths they claimed to cover:

1. **Parity tests bypassed both real surfaces**: the first version called
   `diagnostics.collect()` directly and hand-built a `settings_body` dict
   for comparison, so a regression dropping `backend`/`local_base_url` from
   `doctor_command`'s or `GET /settings`'s own wiring would have left the
   tests passing. Fixed: now invokes the real `cli.doctor_command` (stdout
   captured via `capsys`, with a real stubbed executable on `PATH` so
   version lookups genuinely run) and a real HTTP `GET /settings` request
   through microdot's `TestClient`, matching `tests/test_settings_routes.py`'s
   already-established pattern.
2. **Missing-Codex-package test only stubbed an already-imported
   function**: didn't exercise the real failure mode where
   `from codex_cli_bin import bundled_codex_path` itself raises (e.g. the
   `codex` extra never installed). Fixed: reused this same file's existing
   `builtins.__import__`-guard pattern (already present for a
   `claude_agent_sdk` import-avoidance test) rather than inventing a new
   mechanism, to force the real import statement to fail.

Re-review: CLEAN, confidence 0.99, confirmed both fixes exercise the real
code paths and the import-guard fix restores real state with no leakage
into other tests.

## Test results

Full suite excluding the pre-existing, unrelated WebGL/headless-Chromium
sandbox limitation in `test_viewer_e2e.py`: **1062 passed, 0 failed, 0
errors** (independently re-run after the fix). `tests/test_diagnostics.py`
alone: 37 tests (14 new).

## Roadmap status

All six phases of the multi-backend agent rollout are now complete:
permission-broker decoupling, settings/CLI backend switch, `CodexSession`,
`OmpSession`, live model selection, and backend-aware diagnostics. See
`planning/roadmap.md`'s risk register for the one standalone follow-up not
folded into any phase (the CI workflow gap: `.github/workflows/test.yml`
needs `--extra codex` and an `omp-rpc` install step to collect every test
file this rollout added).
