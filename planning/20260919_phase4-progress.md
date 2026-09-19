# Phase 4 progress report - OmpSession (local/arbitrary-endpoint driver)

Date: 2026-09-20
Status: **COMPLETE**. Ticket `planning/tickets/phase4_omp-session.md` executed
via the Dynamic Workflow tiering, one fix-loop iteration, now clean.

## Research corrections before/during implementation

- **`omp_rpc` is not on PyPI**, confirmed twice (Phase 4's revalidation stamp,
  then re-confirmed by the implementer). Its real MIT-licensed source lives
  in the upstream `oh-my-pi` monorepo (`github.com/can1357/oh-my-pi/tree/main/python/omp-rpc`),
  matching `omp://rpc.md`'s own claim that "the bundled `omp-rpc`
  distribution" is the client. Installed via a commit-pinned git URL for
  implementation/testing. **Not** declared as a `pyproject.toml` extra:
  hatchling refuses to build metadata for a direct/VCS reference without
  `tool.hatch.metadata.allow-direct-references`, and even with that opt-in
  PyPI's own upload validation rejects direct-URL dependencies - declaring it
  would have broken this project's real `publish.yml`. Documented as a manual
  prerequisite instead (pinned pip command in `session/omp.py`'s module
  docstring, surfaced by `doctor`'s "omp_rpc package: NOT FOUND" message).
- **Concurrency model resolved**: `RpcClient` is not asyncio-native - a
  `subprocess.Popen` plus a dedicated synchronous reader thread, structurally
  identical to `CodexClient`. `OmpSession` mirrors `CodexSession`'s
  thread-bridging pattern, with one simplification: `RpcClient`'s event
  stream is one persistent subscription (not scoped per-turn), so no second
  "drain" executor was needed.
- **Custom-provider injection mechanism resolved**: `RpcClient` has no
  `base_url`/`api_key` constructor knobs; `PI_CODING_AGENT_DIR` relocates
  omp's entire agent-config directory. `OmpSession` gives every session its
  own throwaway temp dir via that env var, writes a generated `models.yml`
  there (Q4's confirmed schema), and removes it in `close()`/on any startup
  failure.
- **The load-bearing correction**: the ticket's approach sketch assumed
  `extension_ui_request{method:"confirm"}` was the live per-tool approval
  gate. Reading the actual installed omp CLI's bundled TypeScript source
  showed the real gate uses an undocumented `select{options:["Approve","Deny"]}`
  frame with no structured correlation back to a specific tool call at all -
  relying on it would have made the whole permission design only as correct
  as one CLI build's private implementation. Corrected design: omp is
  launched with `--auto-approve --no-tools --no-extensions` (see Review loop
  below for why the third flag was added), neutralizing every native
  execution/approval path entirely; the real write-class gate lives inside
  `OmpSession`'s own `host_tool_call` adapter (mirroring `CodexSession`'s
  `_approval_handler`/`SdkSession`'s `_can_use_tool`), with the `confirm`
  handler kept only as a defensive second layer.

## What landed

- `session/omp.py` (new, ~800 lines): `OmpSession` implementing the full
  `AgentSession` Protocol against `omp_rpc.RpcClient`. Executor-bridged
  blocking calls; broker-gated `host_tool_call` adapter; per-session
  isolated `models.yml`/`PI_CODING_AGENT_DIR`; `interrupt()` resolving any
  in-flight broker request first, mirroring Phase 3's deadlock fix for the
  same reader-thread-blocking reason.
- `cli.py`: `backend == "local"` branch constructs `OmpSession` for real;
  dead `except NotImplementedError` handler removed; `doctor`/diagnostics
  wiring.
- `diagnostics.py`: `_omp_info`/`_local_endpoint_info`/`_omp_rpc_importable`,
  gated on `backend == "local"`.
- `http/routes_settings.py`: threads `local_base_url` through so `GET
  /settings` reports the same facts `doctor` does.

## Review loop

One review pass, four findings, two priority-1:

1. **Ambient OMP extension execution** (priority 1): the launch disabled
   built-in tools and native approvals but not project-local `.omp`/`.pi`
   extension discovery, which omp loads as trusted code with no trust
   prompt. Combined with `--auto-approve`, an extension could execute at
   session start and register native tools entirely outside `OmpSession`'s
   own broker gate - the "host-tool broker is the only execution path" claim
   was false. Fixed by adding `--no-extensions` (confirmed as a real,
   correctly-named omp 18.1.22 flag via the installed CLI's own `--help`
   output, not guessed).
2. **Confirm-frame mis-correlation** (priority 1): the defensive `confirm`
   handler guessed which in-flight tool call an uncorrelated confirm frame
   belonged to ("most recently inserted"). With two genuinely concurrent
   host-tool calls (confirmed as a real scenario, not hypothetical - omp
   runs sibling tool calls from one turn concurrently), this could approve
   the wrong request. Fixed: fails closed (`confirmed=False`) whenever more
   than one tool call is in flight, with a regression test proving two
   overlapping tool-call lifetimes trigger the fail-closed path rather than
   passing trivially.
3. **Literal API key in `models.yml`** (priority 2): omp's own `apiKey`
   resolution treats the value as an environment-variable name first (and a
   leading `!` as a shell command), so writing `local_api_key` directly risked
   silent substitution or command execution. Fixed: the literal secret is
   set as a generated, session-scoped, collision-resistant environment
   variable's value on the subprocess environment; only that variable's
   *name* is written into `models.yml`.
4. **Temp directory leak on startup failure** (priority 2): `mkdtemp()`
   could succeed and then a failed config write would leave `self._agent_dir`
   unset, so the cleanup path couldn't find the directory to remove. Fixed
   with a wrapping try/except that cleans up before any caller-side
   assignment matters.

Re-review: CLEAN, confidence 0.99, independently re-verified the
`--no-extensions` flag against the real installed CLI's `--help` output and
traced the confirm-handler's fail-closed condition and the temp-dir cleanup
ordering directly.

## Test results

Full suite excluding the pre-existing-flaky, unrelated Playwright e2e file:
**1031 passed, 0 failed, 0 errors** (independently re-run after the fix, not
only trusted from the implementer's report). `tests/test_omp_session.py`
alone: 23 tests.

## Flagged but not fixed (out of this ticket's scope)

- `settings.js` was not touched: `local_base_url`/`local_api_key` were
  deliberately left out of the settings window's rendering in Phase 2 (only
  the `backend` dropdown was in scope there); still configurable via
  `.mesh/config.toml`/user settings/the generic `PUT /settings` API, just not
  via a dedicated form field yet.
- **`.github/workflows/test.yml` gap**: `uv run --extra dev pytest -q`
  installs neither `--extra codex` nor `omp-rpc`, so `tests/test_codex_session.py`
  (Phase 3) and `tests/test_mcp_bridge.py`/`tests/test_omp_session.py`
  (Phase 3/4) all need their optional dependencies installed by hand to even
  collect in CI as currently configured. This is a real gap, not fixed here
  since it falls outside every phase's stated anchors - flagged for
  immediate follow-up (see roadmap's risk register).
- Resume/session-continuity was deliberately not wired for omp
  (`provider_session_id` maps to an undocumented, unconfirmed CLI flag);
  `sdk_session_id` is populated best-effort from `get_state()` instead,
  satisfying the Protocol's "never fabricated" contract.

## What the next phase needs to know

Phase 5 (live model selection) needs no changes to anything Phase 4 landed -
`OmpSession`'s constructor already accepts `model` and the RPC protocol's
`set_model` command (documented in `omp://rpc.md`) is a natural fit for that
phase's live-switching work. Phase 6 (diagnostics) should build on the
`_omp_info` collector shape already landed here, not the ticket's original
speculative sketch.
