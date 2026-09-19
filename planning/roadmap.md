# Multi-backend agent roadmap

Date: 2026-09-19
HEAD: 58f78db34e

Goal: mesh supports three independently selectable agent backends (Claude,
Codex, arbitrary local/OpenAI-compatible), chosen via a `backend` setting the
same way every other CLI/settings key already works, with model selection
promoted to a live webui control (default sourced the same way as the
backend). Mesh's own chat pane, approval-card UX and viewer stay unchanged
regardless of backend.

See `planning/00_index.md` for how to read this folder and the phase-entry
procedure. See `planning/20260919_multi-backend-research.md` for the SDK
research this roadmap rests on.

## Current state

- `session/base.py` defines the provider-agnostic `AgentSession` Protocol
  (`session_id`, `sdk_session_id`, `cwd`, `agent_status()`, `submit_turn`,
  `decide_permission`, `interrupt`, `start`, `close`, `on_viewer_presence`,
  `sandbox_status`). `session/sdk.py`'s `SdkSession` is the only real
  implementation today, wrapping `claude_agent_sdk.ClaudeSDKClient`.
  `session/fake.py` is the scripted test double. `http/ws.py` and `app.py`
  consume only the Protocol; the sole construction site is
  `cli.py`'s `build_session` (~line 724).
- `session/permissions.py`'s `PermissionBroker` is provider-neutral except
  for one spot: `ask()` constructs and returns
  `claude_agent_sdk.PermissionResultAllow`/`PermissionResultDeny` directly
  (imported at the top of the file), and `_session_rule()` returns a
  `claude_agent_sdk.PermissionUpdate`. Every other line (the
  future/timeout/no-viewer/shutdown machinery, `.mesh/permissions.toml`
  grant persistence) is already generic.
- `tools/registry.py`'s `MeshTools` classifies every tool READ/VIEW/WRITE,
  wraps each with the pause gate and failure mapping (`_wrap`), and exposes
  the result as `.mcp_servers` - a Claude-SDK-specific in-process MCP server
  object. `model_tools.py`/`review_tools.py`/`viewer_tools.py` hold the
  actual handlers, already decoupled from the SDK.
- `settings.py`'s `SETTING_KEYS` is a flat tuple of `Key` objects
  (name/type/default/layers/effect/py_type/choices/nullable), resolved
  across four layers (flag/project/user/default) by `resolve()`. Existing
  agent-relevant keys: `model` (Claude-specific description today,
  `USER`/`PROJECT`, `effect=restart`), `effort`, `permission_mode`.
- `protocol.py`'s `_INBOUND_SPECS` is the full inbound WS frame catalogue:
  `hello`, `turn`, `permission`, `result`, `error`, `interrupt`, `state`,
  `pause`. No model-selection frame exists yet.
- `static/js/settings.js` renders every `SETTING_KEYS` entry generically off
  a `SECTIONS`/`CHOICES` table - adding a choice-driven key is a two-line
  change, no new rendering code needed.

## Open questions

| # | Question | Candidate resolution | Owning phase | Status |
|---|---|---|---|---|
| Q1 | Does the curated `ApprovalMode.auto_review` route every write-class Codex tool call to `approval_handler`, or only ones the app-server's own auto-reviewer escalates? | Force `ApprovalsReviewer.user` via a direct `ThreadStartParams(approvals_reviewer=ApprovalsReviewer.user, ...)` construction instead of the curated `ApprovalMode` enum. | Phase 3 | **DECIDED 2026-09-19**: confirmed by reading `openai-codex` 0.154.0 source directly - `ApprovalMode.auto_review` maps to `ApprovalsReviewer.auto_review` (an AI reviewer), not `user`. See `20260919_codex-approval-handler-finding.md`. Still needs a live confirmation that the direct-construction fix actually works before Phase 3's manual integration pass. |
| Q5 | Does the public `Codex`/`AsyncCodex`/`AsyncCodexClient` API expose any way to set a custom `approval_handler`? | N/A - answer is no, use `CodexClient` directly. | Phase 3 | **DECIDED 2026-09-19**: confirmed by reading source - `approval_handler` exists only on the low-level, fully synchronous `CodexClient.__init__`; the public `Codex`/`AsyncCodex`/`AsyncCodexClient` wrappers never accept or forward it, and the unset default silently auto-accepts every command-execution approval request. This is a bigger finding than Q1 was written to cover - see `20260919_codex-approval-handler-finding.md`. `CodexSession` must construct `CodexClient` directly and bridge every blocking call through an executor thread, not only the approval callback. |
| Q2 | What is the current config schema for pointing Codex at a remote MCP-over-HTTP server, given `codex mcp-server`/`codex-mcp-server` was removed? | Register via `CodexConfig.config_overrides` (`--config key=value` CLI passthrough), pointing at a stdio-to-HTTP proxy mesh ships, not a direct HTTP registration. | Phase 3 | **DECIDED 2026-09-19**: confirmed by reading source - there is no remote/HTTP MCP server registration schema anywhere in the generated protocol; Codex's MCP support is stdio-launched-subprocess only (confirmed against the real `~/.codex/config.toml`'s `[mcp_servers.NAME]` `command=`/`env=` shape). The ticket's HTTP-mount design for the tool-execution side is still correct; it now needs one more hop, a stdio-to-HTTP proxy subprocess Codex launches via `config_overrides`. See `20260919_codex-mcp-bridge-finding.md`. Still needs a live check of whether `config_overrides` accepts table-shaped values, not only scalar `key=value`. |
| Q3 | Does `claude_agent_sdk` honor a changed `model=` on a resumed conversation (`resume=<id>, model=<new>`) without a fresh SDK session id? | Very likely (mirrors `claude --resume <id> --model <name>`), but not directly verified against the SDK. If not honored, live model switching for the `claude` backend needs a different mechanism (e.g. refuse the live switch for Claude and require a restart, degrading gracefully). | Phase 5 | OPEN |
| Q4 | Exact wire shape omp expects for `local_api_key`-bearing custom providers (is `models.yml`'s `apiKey` a literal value, an env-var reference, or both)? | A custom provider entry under `~/.omp/agent/models.yml`'s `providers:` key: `baseUrl`, `api: openai-completions`, `apiKey` (env-var-name-or-literal, or `auth: none` for keyless), `models: [{id, name, contextWindow, maxTokens}]`. | Phase 4 | **DECIDED 2026-09-19**: read `omp://providers.md`'s "Custom providers in `models.yml`" section directly (no design note needed, a single documented schema). `apiKey`'s value is resolved as env-var-name-or-literal: if it names an existing environment variable that variable's value is used, otherwise the string itself is the key (a `!`-prefixed value runs as a shell command and uses trimmed stdout). `OmpSession` generates `local_api_key`'s value as a literal (it will not happen to match an env var name in the vanishingly unlikely case it does, which is an acceptable, already-documented edge case of the same mechanism every other custom-provider user relies on) when set, or emits `auth: none` when `local_api_key` is unset - matching mesh's own `local_base_url`/`local_api_key` settings from Phase 2 directly, no further design needed. |

Close each with a dated DECIDED entry and a pointer to the design note that
resolved it; never delete a row.

## Design decisions (settled)

- Claude stays on `claude-agent-sdk` unchanged - it is already the sanctioned
  way to consume Claude subscription billing programmatically
  (`CLAUDE_CODE_OAUTH_TOKEN`), and the user explicitly wants it to remain in
  scope for TOS adherence, not replaced.
- Codex is supported directly via OpenAI's own `openai-codex` Python SDK, for
  the same TOS-adherence reasoning - not routed through omp or any other
  third harness, even though omp's own `openai-codex` provider would have
  worked mechanically.
- Local/arbitrary-endpoint backend goes through omp (`omp_rpc.RpcClient`),
  the one candidate that genuinely supports an arbitrary OpenAI-compatible
  `base_url` with built-in tools and sandboxing, MIT-licensed and safe to
  embed in a commercial product.
- No LiteLLM anywhere in the stack (explicit user rejection: OAuth stability,
  one more tool to configure).
- `permission_mode` (Claude's `default`/`acceptEdits`/`plan`) stays
  Claude-only; Codex and local get a fixed always-ask posture rather than a
  user-selectable laxer mode, since neither has been through the same review
  as Claude's permission modes.
- `effort` is reused as-is for Codex (`thread.run(effort=...)` takes the
  identical vocabulary mesh already validates) rather than duplicated under a
  backend-specific name.
- The new `AgentModelChanged` event/frame naming is deliberately distinct
  from the existing `ModelsChanged` event (`session/base.py`), which reports
  the *served directory's 3D model files* changing - an unrelated concept
  that happens to share the word "model". Do not reuse or rename either.

## Phase 1 - Permission-broker decoupling [COMPLETE - see `20260919_phase1-progress.md`]

Goal: `session/permissions.py` no longer imports `claude_agent_sdk`; every
other session driver can interpret `PermissionBroker.ask()`'s return value
without importing Claude's SDK.

Why this order / why this target first: every later phase's approval-path
integration depends on `ask()` returning a provider-neutral decision. Doing
it first means `CodexSession`/`OmpSession` are never written against the
Claude-coupled shape and then have to be reworked.

Work items:
1. Add a small provider-neutral `Decision` type (`allow: bool`,
   `remember_tool: Optional[str]`, `message: str`) to `session/permissions.py`
   or `session/base.py`.
2. Change `PermissionBroker.ask()` and `_build_result()`/`_session_rule()`'s
   internals to build and return `Decision` instead of
   `PermissionResultAllow`/`PermissionResultDeny`/`PermissionUpdate`.
3. Add a `_to_claude_result(decision) -> PermissionResult` adapter in
   `session/sdk.py`; `SdkSession`'s `can_use_tool` callback calls
   `broker.ask()` then the adapter, preserving today's externally observable
   behavior exactly.
4. Update `tests/test_sdk_session.py` and any direct `PermissionBroker` tests
   for the new return type.

Targets: no hardware; software-only. Tested against mesh's existing fake-transport
harness (`tests/test_sdk_session.py`'s pattern).

Tests: every existing permission-broker/SdkSession test continues passing
unmodified in *behavior* (timeout, no-viewer grace, shutdown-deny, remembered
grants, `NEVER_REMEMBERED` for Bash) with only the return-type plumbing
changed. Adversarial: confirm a raised exception inside `ask()` still cannot
escape (the module's own stated invariant, "every path through ask returns a
decision and nothing here ever raises").

Exit criteria: `grep -r claude_agent_sdk session/permissions.py` is empty;
full existing test suite green.

Workflow shape: implementation on sonnet, test run on haiku, standard +
adversarial review on opus (the adversarial pass specifically checks the
"never raises out of ask" invariant survived the refactor), looped until
clean. Ticket: `planning/tickets/phase1_permission-broker-decoupling.md`.

## Phase 2 - Settings + CLI backend switch (skeleton) [COMPLETE - see `20260919_phase2-progress.md`]

Goal: `--backend`/`backend` setting exists, is validated and CLI-flaggable
exactly like `--model`, and `build_session` branches on it - but the `codex`
and `local` branches are not yet implemented (still only `SdkSession`
constructible), so this phase ships no new backend capability, only the
switch itself.

Why this order: unblocks Phase 3/4 landing independently behind the same
switch, and lets the `model` key's description/behavior change (backend-
generic wording) ship and be reviewed on its own before any new driver exists
to actually use it differently.

Work items:
1. New `Key(name="backend", ...)` in `settings.py`'s `SETTING_KEYS`, choices
   `("claude", "codex", "local")`, default `"claude"`, layers
   `(USER, PROJECT)`, `effect="restart"`.
2. New `local_base_url`/`local_api_key` keys (nullable str, same layers),
   pending Q4's resolution on exact omp wire shape.
3. `--backend` flag in `cli.py`'s `build_parser()`, added to `main()`'s
   `agent_only` list (~line 550) alongside `--model`/`--effort`/
   `--permission-mode`.
4. `build_session` (`cli.py` ~line 724) branches on
   `resolved_settings["backend"]`; `codex`/`local` branches raise/refuse
   clearly ("not yet implemented") until Phase 3/4 land.
5. Gate the `bwrap`/`socat` sandbox preflight (`cli.py` ~line 622) on
   `backend == "claude"` - meaningless for the other two.
6. `model` key's description in `settings.py` updated to backend-generic
   wording.
7. `static/js/settings.js`: add `"backend"` to the `"Agent"` section and
   `CHOICES.backend`.

Targets: no hardware; software-only.

Tests: `settings.py`'s existing validation test pattern extended for
`backend`'s choices; a CLI test confirming `--backend codex` on a `view`
invocation is refused the same way `--model` already is.

Exit criteria: `annealage-mesh --backend claude` behaves identically to
today's default; `--backend codex`/`--backend local` fail with a clear
"not yet implemented" rather than an import error or silent fallback.

Workflow shape: this phase is mechanical enough that a single sonnet pass
plus a haiku test run and one opus review (not adversarial - low-risk,
additive) suffices; no loop expected. Ticket:
`planning/tickets/phase2_settings-backend-switch.md`.

## Phase 3 - `CodexSession`

Goal: `backend = "codex"` drives a real Codex conversation through mesh's
existing chat pane and approval cards, using ChatGPT-subscription OAuth.

Why this order: highest-value new backend (subscription OAuth was the
original ask), and Phase 1's decoupled `PermissionBroker` is a hard
dependency for its approval-handler adapter.

Work items:
1. `session/codex.py`: `CodexSession` wrapping `openai_codex.AsyncCodex`,
   implementing the full `AgentSession` Protocol.
2. Resolve Q1 (approval escalation scope) against a real app-server before
   writing the approval-handler adapter; encode the answer as a design note
   and update this roadmap's Q1 row.
3. Approval-handler adapter: `Decision` (from Phase 1) <->
   `{"decision": "accept"|"acceptForSession"|"decline"}`, bridged off the
   SDK's synchronous reader thread via
   `asyncio.run_coroutine_threadsafe(broker.ask(...), loop).result()`.
4. OAuth: `login_chatgpt()` surfaced through mesh's existing one-time-setup
   affordance pattern (wherever `doctor`/settings already report "not
   configured" states); `login_api_key()` kept as an explicit fallback, not
   the default path.
5. Tool exposure - see `phase3_codex-tool-mcp-bridge.md` (separate ticket,
   Q2-gated; substantial enough and independently risky enough to track on
   its own).
6. Sandbox: `Sandbox.workspace_write` per thread, matching Claude's posture.
7. `diagnostics.py`: `_codex_cli_info` (pinned runtime version, login state
   via `account()`), gated on `backend == "codex"`.

Targets: no hardware; software-only, needs a real Codex account (subscription
or API key) for integration testing, not just the fake-transport unit tests.

Tests: fake stdio-transport harness under `CodexSession` (mirrors
`tests/test_sdk_session.py`'s `transport=` injection point) exercising the
approval-handler and tool-call paths without a real `codex` binary. Manual
integration pass against a real account before merge, since the fake
transport cannot validate Q1/Q2's real-world behavior.

Exit criteria: a `backend = "codex"` session can complete a turn that writes
a file, produces an approval card in the webui, and is answered by the
human, end to end.

Workflow shape: implementation on sonnet, automated (fake-transport) tests on
haiku, standard + adversarial review on opus - the adversarial pass
specifically probes whether every write-class Codex tool call actually
reaches the human given Q1's answer, not just the happy path. Looped until
clean. Tickets: `planning/tickets/phase3_codex-session.md`,
`planning/tickets/phase3_codex-tool-mcp-bridge.md`.

## Phase 4 - `OmpSession`

Goal: `backend = "local"` drives a conversation against an arbitrary
OpenAI-compatible endpoint through the same chat pane and approval cards.

Why this order: mechanically simpler than Phase 3 (in-process host-tool
callback, no MCP bridge needed) and shares the `tool_table()` accessor
Phase 3 introduces on `MeshTools`, so sequencing it after Phase 3 avoids
building that accessor twice.

Work items:
1. `session/omp.py`: `OmpSession` wrapping `omp_rpc.RpcClient`, scoped to a
   custom `models.yml`-style provider built from `local_base_url`/
   `local_api_key`.
2. Resolve Q4 (exact custom-provider wire shape) before generating that
   provider config.
3. Tool exposure via `set_host_tools`/`host_tool_call`/`host_tool_result`,
   reusing Phase 3's `tool_table()` accessor with a different adapter.
4. Permission translation: `extension_ui_request{method:"confirm"}` /
   `extension_ui_response{confirmed}`. Since RPC's `confirm` has no native
   "remember for session" verb, `OmpSession` must check the broker's
   granted-tools set itself before forwarding a `confirm` request to the
   human, rather than relying on omp to track it.
5. `diagnostics.py`: `_omp_info` (omp version, endpoint reachability check
   against `local_base_url`), gated on `backend == "local"`.

Targets: no hardware; software-only, needs some reachable OpenAI-compatible
endpoint for integration testing (a local Ollama/llama.cpp instance is
sufficient and matches the intended use case).

Tests: fake stdio-transport harness under `OmpSession`, same pattern as
Phase 3's. Manual integration pass against a real local endpoint.

Exit criteria: a `backend = "local"` session against a real local endpoint
can complete a turn that writes a file, produces an approval card, and is
answered by the human, end to end.

Workflow shape: implementation on sonnet, automated tests on haiku, standard
+ adversarial review on opus (adversarial pass specifically probes the
remembered-grant workaround in work item 4, since it is the one place this
driver's behavior diverges from the broker's own persistence model). Looped
until clean. Ticket: `planning/tickets/phase4_omp-session.md`.

## Phase 5 - Live model selection

Goal: the webui can change the active model for a running session, on any
backend, without restarting mesh; the CLI-configured `model` setting is only
the starting default.

Why this order: needs all three drivers in place to be one coherent feature
rather than three partial ones: Codex and omp already support it natively;
Claude needs a reconnect fallback that only makes sense to build once the
other two paths exist to compare against.

Work items:
1. `protocol.py`: new inbound frame `"set_model": _Spec({"model"}, {"model"})`
   in `_INBOUND_SPECS`.
2. `session/base.py`: new `AgentSession.set_model(model: str) -> None`
   Protocol member; new `AgentModelChanged(model: str)` event (name chosen
   to avoid collision with the existing `ModelsChanged` STL-files event -
   see the settled design decisions above).
3. `http/ws.py`: dispatch the `set_model` frame to `session.set_model(...)`.
4. Per-driver implementation:
   - `CodexSession`: store the model, apply on the next `thread.turn(model=...)`.
   - `OmpSession`: `client.set_model(provider=..., model_id=...)`.
   - `SdkSession`: resolve Q3 first; if confirmed, close and reconnect with
     `resume=<sdk_session_id>, model=<new>`; if not honored, refuse the live
     switch for Claude with a clear message rather than silently
     misbehaving, and require a restart instead.
5. `build_hello` (`protocol.py`) carries the effective starting model in the
   session object, same pattern as `agent_status`.
6. `static/js/chat.js`: model-picker control near the composer, sends
   `{type:"set_model", model}`, reflects `AgentModelChanged`. Initial value
   from the `hello` payload. Model-list source per backend: `codex.models()`
   for Codex, `get_available_models` for local (falls back to freeform if
   unsupported), a short static list or freeform for Claude.

Targets: no hardware; software-only.

Tests: WS-frame validation test for `set_model` (mirrors `pause`'s pattern in
`protocol.py`'s test suite); per-driver unit test that a submitted turn after
`set_model` actually uses the new model (fake-transport assertion, not a
real model call).

Exit criteria: switching model from the webui on any backend visibly changes
the model used by the next turn, without a mesh restart (or, for Claude if
Q3 resolves negatively, with a clearly communicated exception rather than a
silent no-op).

Workflow shape: implementation on sonnet, automated tests on haiku, standard
review on opus (not adversarial - this phase is additive UI/plumbing, low
security surface). Ticket: `planning/tickets/phase5_live-model-selection.md`.

## Phase 6 - Diagnostics and settings-UI polish

Goal: `doctor`/`GET /settings` report the right diagnostics for whichever
backend is configured, and the settings window's diagnostics block (already
deduped against `doctor` per `routes_settings.py`'s own docstring) reflects
all three backends.

Why this order: last, since it only aggregates state Phases 2-4 already
produce (`_codex_cli_info`, `_omp_info` collectors land in those phases'
tickets; this phase is the `collect()`/report wiring and formatting).

Work items:
1. `diagnostics.py`'s `collect()` calls the backend-appropriate collector(s)
   based on resolved settings, not unconditionally.
2. `cli.py`'s `diagnostics_report()` formats the new fields.
3. `routes_settings.py`'s diagnostics block picks up the same fields with no
   drift from the terminal `doctor` output (the module's own stated
   invariant: "both read one collector, so they cannot drift").

Targets: no hardware; software-only.

Tests: `tests/test_diagnostics.py` extended for each backend's collector,
including the missing-dependency/not-logged-in cases.

Exit criteria: `annealage-mesh doctor` and the settings window's diagnostics
panel report identical, backend-appropriate content for all three backends.

Workflow shape: sonnet implementation, haiku tests, one opus review (not
adversarial). Ticket: `planning/tickets/phase6_diagnostics.md`.

## Risk register

| Risk | Mitigation |
|---|---|
| Codex's `ApprovalMode.auto_review` silently under-prompts (Q1) | Phase 3 blocks on resolving Q1 against a real app-server before the approval adapter is written; adversarial review in Phase 3 specifically re-checks this. |
| Codex MCP-bridge config schema has moved since this research (Q2) | Phase 3's tool-bridge ticket is written as a separate, explicitly Q2-gated ticket rather than folded into the core session driver, so schema drift does not block the rest of the backend. |
| Claude live model-switch-on-resume unsupported (Q3) | Phase 5 degrades to "refuse with a clear message" rather than a silent no-op or a crash. |
| omp custom-provider wire shape assumed rather than verified (Q4) | Phase 4 blocks on reading `omp://providers.md`'s full custom-provider schema section before generating provider config. |
| Three drivers drift in approval/tool/sandbox behavior over time | `session/base.py`'s Protocol and `tools/registry.py`'s classification stay the single source of truth every driver adapts to, not three independent policies. |

## Progress tracking

Each phase writes its own `YYYYMMDD_<topic>.md` to this folder as it
completes (findings, deviations from the ticket, anything the next phase
needs to know). This roadmap is updated in place as phases complete and
questions close - never forked. At the entrance to each phase, the workflow
planner revalidates that phase's tickets against everything landed since
they were written (`planning/00_index.md`'s phase-entry procedure) before
authoring the phase's `Workflow` script.
