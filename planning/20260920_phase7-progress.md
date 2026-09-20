# Phase 7 progress: real on-demand integration tests

Date: 2026-09-20
HEAD at completion: (see final commit of this phase)

## What shipped

- `pyproject.toml`: new `[tool.pytest.ini_options]` section registers the
  `integration` marker and excludes it from every default run
  (`addopts = "-m 'not integration'"`). Verified: the existing 1062-test
  baseline is unaffected by this change alone.
- `tests/test_sdk_session_live.py`, `tests/test_codex_session_live.py`,
  `tests/test_omp_session_live.py`: one real, on-demand test per backend.
  Each constructs the *real* driver class (`SdkSession`/`CodexSession`/
  `OmpSession`) with no fake `client_factory`/`transport` override, submits
  one trivial text-only prompt ("Reply with exactly the single word PONG
  and nothing else, no punctuation."), and asserts "pong" appears in the
  concatenated reply. Each is individually gated by
  `pytest.mark.skipif` on its own required `MESH_LIVE_*` credential(s), so
  the tier stays usable per-backend even when only some credentials are
  configured.
- `.github/workflows/integration.yml`: `workflow_dispatch`-only CI job
  (no `push`/`pull_request`/`workflow_call` trigger - this tier must never
  run unattended), mapping repo/environment secrets to the `MESH_LIVE_*`
  variables and running `uv run --extra dev --extra codex pytest -m
  integration -q`. Documents, rather than fabricates, a real `omp` binary
  install step - this project has no verified, repeatable install channel
  for that binary (unlike `openai-codex`/`omp-rpc`, which are Python
  packages this repo's own dependency chain can pin), so the workflow says
  explicitly what a human must add before the omp leg can actually run.

## Why this phase existed

Phase 3's and Phase 4's own tickets each named a "manual integration pass
against a real account/endpoint" as an acceptance criterion. Neither phase
actually performed one before being marked complete - every test in this
rollout up to this point drives a hand-written fake
(`FakeCodexClient`/`FakeRpcClient`/`test_sdk_session.py`'s `FakeTransport`)
one level below the real SDK object. That is deliberate and correct for
what it proves (protocol-shape and approval-gating correctness against a
scriptable double), but proves nothing about whether the real
`openai_codex`/`omp_rpc`/`claude_agent_sdk` packages, given mesh's actual
generated config, actually start, authenticate, and complete a turn. The
user asked directly whether any existing test did this; the honest answer
was no, and this phase closes that gap as a repeatable, opt-in automated
tier rather than a one-off manual pass that leaves nothing behind.

## Key design decisions

- **`MESH_LIVE_*`-prefixed env vars, not bare `ANTHROPIC_API_KEY`/
  `OPENAI_API_KEY`.** A developer's shell commonly has those set for
  unrelated purposes (this very harness, other tools); reusing them would
  let an ambient credential silently turn on live spending the moment
  someone typed `-m integration` for an unrelated reason. The prefix makes
  enabling each backend's live tier a deliberate, single-purpose decision.
- **`omp` model naming corrected from the user's literal phrasing.**
  `OmpSession.start()` (`session/omp.py`) always constructs
  `model="%s/%s" % (_PROVIDER_ID, model_id)` where `_PROVIDER_ID` is mesh's
  own hardcoded `"mesh-local"` string - confirmed by reading the real
  source and cross-checked against `tests/test_omp_session.py`'s existing
  fake-transport assertion. The `model=` constructor argument is therefore
  always the bare model id the endpoint itself understands
  (`MESH_LIVE_OMP_MODEL` defaults to `"qwen3.8-27b"`); "titan" names which
  real host serves it (`MESH_LIVE_OMP_BASE_URL`'s concern), never part of
  the model string - passing `"titan/qwen3.8-27b"` as `model=` would
  double-prefix into `"mesh-local/titan/qwen3.8-27b"`, which no real
  endpoint answers to. Documented prominently in both the ticket and each
  live test file's own docstring so this does not get silently
  reintroduced later.
- **Credential/config isolation, never the shared harness's own state:**
  - Claude: `tests/conftest.py`'s existing autouse `isolated_user_config`
    fixture already isolates `XDG_CONFIG_HOME`; `ANTHROPIC_API_KEY` is set
    via `monkeypatch` for the test process only, confirmed (by reading
    `claude_agent_sdk`'s installed `subprocess_cli.py`) to be inherited by
    the real subprocess through `os.environ` unless `ClaudeAgentOptions.env`
    overrides it, which `SdkSession` never does.
  - Codex: `CODEX_HOME` monkeypatched to a fresh `tmp_path` subdirectory
    before a throwaway raw `CodexClient` performs a real
    `account_login_start` API-key login - confirmed (by reading
    `openai_codex/client.py`) that `CodexConfig.env` merges onto
    `os.environ.copy()`, so both the throwaway login client and the real
    `CodexSession` afterward inherit the isolated directory with no
    `CodexSession` constructor change needed. Never touches `~/.codex`.
  - omp: `OmpSession.start()` hardcodes `executable="omp"` with no
    constructor override, resolved via the launched subprocess's `PATH` -
    confirmed this is genuinely the only seam (same one
    `tests/test_diagnostics.py`'s `_omp_info` tests already use). The live
    test prepends a scratch directory holding a symlink literally named
    `omp`, pointing at `MESH_LIVE_OMP_EXECUTABLE`'s target, onto `PATH` -
    no bare-`"omp"` fallback, so it can never silently resolve to this
    workstation's own shared `~/.local/bin/omp` install. `PI_CODING_AGENT_DIR`
    isolation is already handled by `OmpSession` itself (Phase 4's design),
    nothing extra needed there.
- **Every live test constructs a real `PermissionBroker`, never
  `broker=None`** - even though a trivial text-only prompt never triggers
  an approval, this codebase's own Q1/Q5 finding
  (`20260919_codex-approval-handler-finding.md`) is that a nulled
  `approval_handler` is a silent full bypass specifically on the Codex
  side; no test in this codebase should ever be the one place that runs a
  real backend with no broker wired, even as an unexercised code path.

## Review findings and fixes

One adversarial review loop (opus), ten targeted checks against the
isolation/safety guarantees this tier's whole premise rests on. One
priority-1 finding, confirmed fixed in a follow-up re-review:

- **GitHub Actions expands an unset secret/variable to an empty string,
  not an absent env var.** `.github/workflows/integration.yml`'s env:
  block unconditionally maps every `MESH_LIVE_*` name from
  `secrets`/`vars`, so a dispatch with a backend's secrets unconfigured
  would set that variable to `""` - present in `os.environ`, just empty.
  All three test files' original `skipif` conditions
  (`"X" not in os.environ`) and optional-override defaults
  (`os.environ.get("X", default)`) checked presence, not truthiness, so
  that dispatch would have attempted a real run with an empty
  key/URL/executable path instead of skipping, and every optional model
  override would have silently become `""` instead of its documented
  default. Fixed: every `skipif` now uses `not os.environ.get("X")`
  (falsy, not presence); every optional default now uses
  `os.environ.get("X") or default`; `test_omp_session_live.py`'s optional
  `api_key` now uses `os.environ.get("MESH_LIVE_OMP_API_KEY") or None`
  (an empty-string secret must mean keyless, not "send an empty
  Authorization header", per `session/omp.py`'s own documented
  `api_key=None` contract). Verified directly: re-running
  `pytest -m integration -q -rs` with all four required `MESH_LIVE_*`
  variables explicitly exported as empty strings - the exact failure mode
  found - now reports all three skipped, same as the unset case.

Nine other checks passed clean on the first pass: marker exclusion is
effective from every default entry point including `test.yml`'s own
invocations; no test ever constructs a real driver with `broker=None`;
the omp test never falls back to a bare `"omp"` PATH lookup; the Codex
login flow never puts the API key in a diagnostic dump, assertion
message, or log line; the skip path has no side-effecting module-level
code and never touches `~/.codex`/a bare `omp` before the skip evaluates;
`CodexConfig.env`'s merge semantics were independently re-verified against
the installed source; `OmpSession` genuinely has no `executable=`
parameter to use instead of the PATH seam; the omp model-naming claim was
independently re-derived from `session/omp.py`'s actual source, not just
taken on the ticket's word; `integration.yml` is `workflow_dispatch`-only
with no reusable-workflow reference from `test.yml`/`publish.yml`.

## What is verified vs. not

Verified for real, in this environment:
- `pytest -q` (bare, matching every default local/CI invocation) collects
  and passes the same 1062 tests as before this phase.
- `pytest -m integration -q -rs` with no `MESH_LIVE_*` variables set
  reports all three new tests skipped, each with its documented reason -
  never errored, never silently absent.
- The same command re-run with all four required `MESH_LIVE_*` variables
  explicitly set to empty strings (the exact GitHub Actions
  unset-secret-expansion failure mode the review caught) also reports all
  three skipped - proving the fix, not just the original design.
- `ruff check`/`ruff format --check` clean on all three new test files.
- `.github/workflows/integration.yml` parses as valid YAML with
  `workflow_dispatch` as its sole trigger.

**Not verified, and cannot be verified in this environment:** an actual
passing live run against a real Claude/Codex/omp backend. No Anthropic API
key, OpenAI API key, or reachable omp/titan endpoint is available here.
This is the roadmap's own stated risk-register entry for this phase, not
an oversight - a human exporting the documented `MESH_LIVE_*` variables (or
configuring the equivalent GitHub Actions secrets and running
`integration.yml` from the Actions tab, once they also add an `omp`
binary-install step it deliberately does not fabricate) is required to get
a real pass/fail. Nothing in this phase claims otherwise.

## Test baseline

`pytest -q` (bare): 1062 passed, 3 deselected (unchanged from before this
phase). `pytest -m integration -q`: 3 skipped, 1113 deselected, 0
failed/errored, in this credential-less environment.
