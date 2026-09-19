# diagnostics.py: backend-aware doctor / settings diagnostics

Phase: 6
Depends on: Phase 3 (`_codex_cli_info`), Phase 4 (`_omp_info`) - both
collectors are written as part of those phases' tickets; this ticket is only
the aggregation/reporting wiring
Written: 2026-09-19 at HEAD 58f78db34e
Revalidated: 2026-09-20 at HEAD 5d8094e - MAJOR DRIFT, ticket shape largely obsolete. `diagnostics.py`'s `collect()` already gates `codex_cli`/`omp_cli` on `backend` (landed as a side effect of Phase 3's/Phase 4's own implementation work, each adding its own collector's call site directly rather than leaving it for this ticket); `claude_cli` stays ungated per an explicit, already-written docstring decision ("predates the multi-backend project and this ticket does not change that"). `cli.py`'s `diagnostics_report()` already formats both new fact shapes (codex_cli/omp_cli blocks, missing/not-configured reporting). `http/routes_settings.py` already threads `backend`/`local_base_url` through to the same `collect()` call. The ONLY work item genuinely still open is this ticket's own stated "Acceptance criteria and tests" - `tests/test_diagnostics.py` currently has zero coverage of `codex_cli`/`omp_cli`/`backend=` (confirmed via grep, 0 matches) despite the production code being fully wired and shipped. Scope narrowed accordingly below; anchors/approach-sketch sections are now historical (what already happened) rather than a work plan.

## Context

`diagnostics.py`'s `collect()` is the single source both `annealage-mesh
doctor` (`cli.py`'s `doctor_command`/`diagnostics_report`) and `GET
/settings`'s diagnostics block (`http/routes_settings.py`) read from - that
file's own docstring states this is deliberate ("Both read one collector, so
they cannot drift"). This ticket keeps that property true across three
backends instead of one.

## Scope

In scope: `collect()`'s backend-conditional dispatch to
`_claude_cli_info`/`_codex_cli_info`/`_omp_info`; `cli.py`'s
`diagnostics_report()` formatting for the two new fact shapes;
`tests/test_diagnostics.py` coverage.

Out of scope: the collectors themselves (`_codex_cli_info`, `_omp_info`) -
those are written in Phase 3/4's tickets as part of building each driver,
since writing them requires already having built the SDK-wrapping code they
introspect.

## Files and anchors

- `diagnostics.py:78-124` - `collect()`. Currently calls `_claude_cli_info`
  and `_detect_git`/`_sandbox_info`/`_lock_info`/`_settings_files`
  unconditionally (exact call site inside `collect()`'s body was elided in
  this research pass's structural read - re-read lines 78-124 in full before
  editing). Change the Claude-specific call to be conditional on
  `settings["backend"] == "claude"`, and add the equivalent conditional
  calls for `codex`/`local`.
- `diagnostics.py:166-183` - `_claude_cli_info`, the `{"path", "version",
  "source"}` shape to match for the two new collectors (written in Phase
  3/4, this ticket only wires their call sites).
- `diagnostics.py:241-256` - `_sandbox_info`. Stays Claude-specific
  (`bwrap`/`socat`) per Phase 2's sandbox-preflight gating - do not extend
  this to report on Codex's `Sandbox` enum or omp's posture; if those need
  reporting, that is scope for their own collectors, not this one repurposed.
- `cli.py:339-417` - `diagnostics_report()`. Currently formats Claude-CLI
  facts unconditionally in fixed positions; needs conditional sections
  matching whichever backend's facts `collect()` actually returned.
- `http/routes_settings.py` (49-167, largely unread in this research pass) -
  confirm its diagnostics-block formatting reads generically off whatever
  `collect()` returns rather than hard-coding Claude-specific keys; if it
  does hard-code them, this ticket must fix that too, since the module's own
  docstring claims formatting parity with `doctor` as an invariant.

## Design constraints

- Preserve `_settings_files`/`_lock_info`/`_detect_git`'s backend-independent
  reporting exactly as is - only the agent-CLI-specific block branches by
  backend.
- `doctor` and `GET /settings` must report identically for a given resolved
  backend - this is the pre-existing invariant this ticket must not weaken
  while adding branches.

## Approach sketch

```python
def collect(*, project_dir=None, ..., backend="claude"):
    ...
    agent_info = {
        "claude": lambda: {"claude_cli": _claude_cli_info(run=run, which=which)},
        "codex": lambda: {"codex_sdk": _codex_cli_info(run=run, which=which)},
        "local": lambda: {"omp": _omp_info(run=run, which=which, base_url=...)},
    }[backend]()
    return {**agent_info, "git": ..., "sandbox": _sandbox_info(), ...}
```

(Exact key names should match whatever shape Phase 3/4's tickets actually
land, since this ticket depends on them - revalidate against their landed
code at Phase 6 entry per `planning/00_index.md`'s phase-entry procedure,
not against this sketch.)

## Acceptance criteria and tests

- `tests/test_diagnostics.py` extended with one case per backend: the
  right collector's facts appear, the other two backends' facts do not.
- A backend whose CLI/runtime is missing/not-logged-in reports that clearly
  (matching the existing "missing" reporting voice for Claude's `bwrap`/
  `socat` case) rather than raising.
- `doctor`'s stdout and `GET /settings`'s diagnostics JSON, for the same
  resolved backend, contain the same facts (manual or scripted comparison).

## Workflow shape

Sonnet implementation, haiku test run, one opus review (not adversarial -
purely additive reporting, no new security surface). No loop expected.

## Open questions

None new - this ticket only aggregates facts Phase 3/4 already define;
revalidate its file:line anchors against whatever those phases actually
land before starting, per the standard phase-entry procedure.
