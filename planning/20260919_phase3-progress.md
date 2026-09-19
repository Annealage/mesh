# Phase 3 progress report - CodexSession core driver + MCP tool bridge

Date: 2026-09-19
Status: **COMPLETE**. Both tickets
(`planning/tickets/phase3_codex-session.md`,
`planning/tickets/phase3_codex-tool-mcp-bridge.md`) executed via the Dynamic
Workflow tiering, one fix-loop iteration across both combined, now clean.

## Research corrections before implementation

Two of the roadmap's open questions turned out to need real source
investigation beyond what the original research pass covered, both resolved
by installing `openai-codex` 0.154.0 in a scratch venv and reading its
source directly (never against a live Codex account - see the design notes
for why `~/.codex` on this workstation was deliberately not used as a test
rig, it belongs to the harness executing this session, not this project):

- **Q1/Q5** (`20260919_codex-approval-handler-finding.md`): the ticket's
  original approach sketch assumed `AsyncCodex(config=CodexConfig(approval_handler=...))`
  would work. It does not - `approval_handler` exists only on the low-level,
  fully synchronous `CodexClient`; the public `Codex`/`AsyncCodex`/
  `AsyncCodexClient` wrappers never accept or forward it, and the unset
  default silently auto-accepts every command-execution approval request.
  `CodexSession` had to be redesigned around `CodexClient` directly with
  executor-thread bridging for every blocking call, not only the approval
  callback.
- **Q2** (`20260919_codex-mcp-bridge-finding.md`): the ticket's "MCP-over-HTTP"
  premise had no corresponding config surface - Codex only registers
  stdio-launched MCP subprocesses (`CodexConfig.config_overrides`, a
  `--config key=value` CLI passthrough), never a direct URL. The bridge
  design gained one more hop: a stdio-to-HTTP proxy subprocess Codex
  launches, forwarding to the HTTP endpoint mounted on mesh's own server.

Both tickets were rewritten in place with the corrected design before
implementation started, per the phase-entry procedure.

## What landed

- `session/codex.py` (new, ~940 lines after both passes): `CodexSession`
  implementing the full `AgentSession` Protocol against `CodexClient`
  directly. Two single-worker `ThreadPoolExecutor`s (control-plane calls vs.
  the long-lived per-turn notification drain - a deadlock the ticket's own
  sketch would have had, found and fixed during implementation: putting
  `interrupt()` on the same executor as the drain would queue every
  interrupt behind the very turn it means to cut short). `ThreadStartParams`
  constructed directly with `approvals_reviewer=ApprovalsReviewer.user`
  (never the curated `ApprovalMode` enum). OAuth via `account_login_start()`
  called directly (the convenience `login_chatgpt()`/`login_api_key()`
  methods only exist on the forbidden wrapper classes).
- `http/routes_mcp.py` (new) + `session/codex_mcp_stdio_bridge.py` (new):
  the two-piece tool-exposure bridge. `/mcp` on mesh's own microdot app,
  same run-token/origin auth as `/ws`, built from a new `tool_table()`
  accessor on `MeshTools` (reuses the same `_wrap`-gated handlers
  `.mcp_servers` already exposes to Claude, no duplicated classification
  logic). A stdio-to-HTTP proxy subprocess built on the official `mcp` SDK's
  own stdio server machinery (no third-party proxy dependency existed for
  Python; writing one against the official SDK needed no new dependency,
  since `mcp`/`httpx` were already transitive via `claude-agent-sdk`).
- `tools/registry.py`, `cli.py`, `app.py`, `diagnostics.py`,
  `http/routes_settings.py`, `pyproject.toml` (new `codex` optional
  dependency extra, `mcp`/`httpx` promoted from transitive to declared).

## The zero-approval finding

The MCP bridge implementation surfaced a finding the ticket's own risk
framing got backwards: it worried about a write-class call reaching
`PermissionBroker` *twice* (transport-level double-counting). The real risk
was *zero* times - Codex's app-server has no `approval_handler` hook for a
generic external MCP tool call, only for its own native `commandExecution`/
`fileChange` requests. Without a gate inside `routes_mcp.py` itself, every
MCP-bridged write-class tool call would have silently bypassed the human
entirely. The gate now lives in the route: it reads each tool's
`WRITE_CLASS` membership (computed once in `tool_table()`) and calls
`broker.ask(...)` exactly once before invoking the handler, failing closed
if no broker was supplied.

## Review loop

One review pass, four findings, two priority-1:

1. **Startup-order race** (priority 1): `app.run()` awaited the agent
   session's `start()` before the HTTP listener was accepting connections,
   so the Codex-launched proxy's initial `tools/list` hit a refused port and
   Codex would mark the MCP server failed - the production `backend=codex`
   path could reach READY with every mesh tool silently unavailable. Fixed
   by starting the listener first.
2. **Interrupt deadlock** (priority 1): a pending native approval blocks
   `CodexClient`'s sole reader thread inside `_approval_handler`;
   `interrupt()` waited on a `turn/interrupt` response that could only be
   routed by that same blocked thread. Fixed: `interrupt()` now resolves
   (denies) any in-flight broker request first, unblocking the reader
   thread, before awaiting the interrupt response. A new regression test
   models the single-reader-thread constraint and would hang on the pre-fix
   code.
3. **Client leak on partial-startup failure** (priority 2): a `client.start()`
   success followed by `initialize()`/`account_read()` raising dropped the
   only client reference without closing it, leaking the subprocess. Fixed.
4. **Wrong exception path on stdio-proxy `KeyboardInterrupt`** (priority 2):
   AnyIO's cancellation class was resolved after `anyio.run()` had already
   unwound (no active backend), raising `NoEventLoopError` instead of
   matching the real interruption. Fixed by resolving it inside the active
   backend.

Re-review: CLEAN, confidence 0.98, explicitly traced why the interrupt
regression test genuinely reproduces the pre-fix deadlock rather than
trivially passing either way.

## Test results

Full suite excluding the pre-existing-flaky, unrelated Playwright e2e file:
**1008 passed, 0 failed, 0 errors** (independently re-run after the fix, not
only trusted from the implementer's report).

## Commit-hygiene note

A `git add -A` while closing Q2's research (mid-Phase-3, with the core
driver's code sitting uncommitted in the working tree) swept the entire
core-driver implementation into a commit titled "Close Q2..." A subsequent
attempt to un-mix it via `git reset --soft` did not fully separate the two
(soft reset leaves the index, not just the working tree, at the
pre-reset state, which was not accounted for), so the core driver's code
ended up staying folded into the "Close Q2 and Q4 research questions"
commit rather than getting its own. The MCP bridge and review-fix delta,
landed afterward, did get a properly scoped commit. Net effect: two of
Phase 3's four commits describe their contents completely accurately: two
have `.md`-only titles that also carry the core driver's code. No
information was lost and nothing is incorrect, but it is a real deviation
from this project's "snapshot-coherent commit messages" convention, noted
here rather than silently left for a future reader to puzzle over. Future
phases: scope every `git add` explicitly per commit and verify with
`git status`/`git diff --cached --stat` before committing, rather than
`git add -A`, whenever unrelated work might be sitting in the tree.

## What the next phase needs to know

Phase 4's ticket (`planning/tickets/phase4_omp-session.md`) depends on
`tools/registry.py`'s `tool_table()` accessor, now landed with a concrete
shape (`{name: ToolSpec}`, `ToolSpec(schema, description, handler, write)`)
- revalidate against that exact shape, not the ticket's original
speculative sketch, at Phase 4's entry.
