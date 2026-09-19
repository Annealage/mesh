# Phase 2 progress report - settings + CLI backend switch

Date: 2026-09-19
Status: **COMPLETE**. Ticket `planning/tickets/phase2_settings-backend-switch.md`
executed via the Dynamic Workflow tiering, one fix-loop iteration, now clean.

## What landed

- `settings.py`: three new `Key`s - `backend` (choices `claude`/`codex`/
  `local`, default `claude`, layers `USER`+`PROJECT`, `effect=restart`),
  `local_base_url`, `local_api_key` (both nullable str, same layers).
  `SETTING_KEYS` grew from 8 to 11 entries. `model`'s description reworded
  to backend-generic wording. `permission_mode` left untouched (still
  `PROJECT`-only).
- `cli.py`: `--backend` flag wired identically to `--model`/`--effort`/
  `--permission-mode` (argparse, `flags_from()`, `agent_only` refusal list).
  The bwrap/socat sandbox preflight now gated on `resolved_settings["backend"]
  == "claude"`. `build_session` branches on `backend` before any
  Claude-specific import: `codex`/`local` raise `NotImplementedError`
  surfaced as a clean stderr message + exit code 2 (new `except
  NotImplementedError` clause around `asyncio.run(app_module.run(...))`),
  never reaching `describe_agent_posture`/`on_ready`. The `claude` branch's
  `SdkSession` construction is byte-for-byte unchanged.
- `static/js/settings.js`: `"backend"` added to the Agent section,
  `CHOICES.backend = ["claude", "codex", "local"]`. `local_base_url`/
  `local_api_key` deliberately not yet rendered (Phase 4's concern).

Net diff: 4 source files + 1 test file, +98/-18 (excluding the ticket's own
Revalidated stamp).

## Fix loop

The implementation pass surfaced two pre-existing tests broken by adding 3
new settings keys - both correctly anticipated by the implementer, left for
the test stage per instructions:

- `test_setting_keys_and_keys_by_name_agree` hardcoded `len(SETTING_KEYS) ==
  8`; updated to `11`.
- `test_emitter_round_trips_every_value_type_in_the_table` asserted every key
  resolves to `USER`/`PROJECT` provenance after an `apply()` call whose input
  dict didn't include the 3 new keys (so they defaulted to `"default"`
  provenance); fixed by adding `backend`/`local_base_url`/`local_api_key`
  values to the `apply()` call and matching assertions against
  `project_mapping`.

Both are test-only fixes with no production-code changes. Full suite after
the fix: 971 passed, 0 failed (excluding the pre-existing-flaky, unrelated
Playwright e2e file).

## Review

CLEAN, confidence 0.98, zero findings. Confirmed: `backend` key shape matches
the ticket exactly; `permission_mode` untouched; `--backend` fully threaded
through `flags_from()` (not just parsed and dropped); sandbox preflight
correctly gated; `build_session`'s stub branches raise cleanly without
reaching posture-reporting code; `settings.js` follows the existing generic-
rendering convention with no special-cased new code; `local_base_url`/
`local_api_key` correctly deferred out of the UI.

## Infrastructure note

`reviewer` (opus-tier) succeeded on first try this phase - no repeat of
Phase 1's provider usage-limit issue. `sonic` (haiku-tier) was not dispatched
this phase; the authoritative test run was executed directly to avoid
re-triggering that provider if it was still degraded (it had failed twice for
`reviewer` and once for `sonic` during Phase 1).

## What the next phase needs to know

Phase 3/4's tickets (`CodexSession`/`OmpSession`) should, at their own
phase-entry revalidation, confirm `build_session`'s current branch shape
(`cli.py`, now restructured - anchors moved from the Phase-1-era line numbers
their tickets cite) before replacing the `NotImplementedError` stubs with
real construction calls. The stub raise happens *before* any
Claude-SDK-specific import, which Phase 3/4 should preserve: importing
`openai_codex`/`omp_rpc` only inside their own branch, not at module top
level, keeps `claude`-backend runs free of an unnecessary dependency import.
