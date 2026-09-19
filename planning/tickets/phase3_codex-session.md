# session/codex.py: CodexSession (core driver)

Phase: 3
Depends on: Phase 1 (permission-broker `Decision` type), Phase 2 (`backend`
setting, `build_session` branch point)
Written: 2026-09-19 at HEAD 58f78db34e
Revalidated: 2026-09-19 at HEAD 2a9fce9 - Phase 1 changed session/permissions.py's ask() to return Decision (as this ticket already assumed); Phase 2 restructured cli.py's build_session (now branches on backend before any import, stub raises NotImplementedError - anchor below updated). Also: Q1 investigated further and DECIDED, revealing a bigger finding (Q5) that changes the Approach sketch substantially - see 20260919_codex-approval-handler-finding.md. Design constraints and Approach sketch below rewritten accordingly; Open questions trimmed to what remains genuinely open.

## Context

`openai-codex` (Apache-2.0, `pip install openai-codex`) is OpenAI's own
Python SDK wrapping the local `codex app-server`. It ships its own pinned
CLI runtime, supports genuine ChatGPT-subscription OAuth
(`login_chatgpt()`), and - the load-bearing finding of this research pass,
confirmed by reading SDK source directly rather than the curated docs - has
a real per-call human-approval callback (`approval_handler`) whose shape
matches mesh's `PermissionBroker`/`can_use_tool` pattern closely enough to
reuse directly. Full research trail:
`planning/20260919_multi-backend-research.md`'s "Codex: openai-codex Python
SDK" section.

This ticket is the core session driver (construction, auth, sandbox,
approval, model params, lifecycle). Tool exposure (the MCP bridge) is
deliberately a separate ticket - `phase3_codex-tool-mcp-bridge.md` - because
it depends on an unverified config schema (Q2) and should not block the rest
of this driver if that schema takes longer to confirm.

## Scope

In scope: `session/codex.py`'s `CodexSession` class implementing the full
`AgentSession` Protocol against `openai_codex.client.CodexClient` directly
(**not** `Codex`/`AsyncCodex`/`AsyncCodexClient` - see
`20260919_codex-approval-handler-finding.md`: the public wrappers have no
way to set `approval_handler` at all, and silently auto-accept every
command-execution approval without one); OAuth surfacing; sandbox posture;
the approval-handler adapter; the executor-thread bridge every blocking
`CodexClient` call needs.

Out of scope: tool exposure (`phase3_codex-tool-mcp-bridge.md`); live model
switching (`phase5_live-model-selection.md` - this ticket only needs
`model=`/`effort=` accepted as construction-time defaults, not live
switching); `diagnostics.py` wiring (folded into this ticket's work item 7
per `roadmap.md`, listed below).

## Files and anchors

- `session/base.py:313-395` - the `AgentSession` Protocol this class must
  satisfy: `session_id`, `sdk_session_id`, `cwd` attributes; `agent_status()`,
  `submit_turn(blocks, viewer=None)`, `decide_permission(request_id,
  decision, message="")`, `interrupt()`, `start()`, `close()`,
  `on_viewer_presence(count)`, `sandbox_status()`.
- `session/sdk.py:168-263` (and the unread remainder, lines 264-748) - the
  reference implementation. Read the full file before writing
  `CodexSession`: it is the pattern to structurally mirror (constructor
  argument shape, `_set_status`/`AGENT_CONNECTING`/`AGENT_READY`/
  `AGENT_UNAVAILABLE` transitions, `_emit` helper, viewer-presence counting
  feeding the broker) even though the underlying client library differs
  completely.
- `session/permissions.py` (post-Phase-1) - `PermissionBroker.ask(...)`
  returns `Decision`; `decide_permission` on this class dispatches inbound
  `permission` frames to `broker.decide(...)`, unchanged from `SdkSession`'s
  pattern.
- `settings.py:183-224` - `model`/`effort` keys, now backend-generic; this
  session's constructor takes both as `Optional[str]` the same way
  `SdkSession` does (`session/sdk.py:189-190`).
- `cli.py`'s `build_session` (post-Phase-2; restructured, no longer at the
  original ~724-773 anchor - re-locate it by name before editing) - the
  `backend == "codex"` branch currently does
  `raise NotImplementedError("backend=codex is not yet implemented")`
  *before* any Claude-SDK import. Replace that raise with the real
  construction call, importing `openai_codex`/`session.codex` only inside
  this branch (matching the Phase-2 progress report's stated intent: keep
  the `claude` backend free of an unnecessary dependency import). Mirror
  `SdkSession(...)`'s keyword shape (`cwd`, `session_id`, `broker`, `model`,
  `effort`, `on_sdk_session_id`, `trusted_config_digest` as applicable -
  `permission_mode` is Claude-only per the settled design decision in
  `roadmap.md` and has no Codex equivalent to pass).
- `diagnostics.py:78-183` - `collect()`/`_claude_cli_info`. Add
  `_codex_cli_info(*, run, which)` following the same `{"path", "version",
  "source"}` shape, but sourced from `codex.account()`/the pinned
  `openai-codex-cli-bin` version rather than a `which()` lookup (there may be
  no `codex` binary on `PATH` at all - it is bundled inside the Python
  package). Gated on `backend == "codex"` in `collect()`'s call site.

## Design constraints

- OAuth is the whole point of this backend: `login_api_key()` must exist as
  an explicit fallback (someone may prefer pay-per-token billing) but must
  not be the default or the only path documented to the human - mirror
  however this project already tells a human "you need to run X to set this
  up" for Claude (check `describe_agent_posture`, `cli.py:420-467`, and
  `diagnostics.py`'s missing-dependency reporting pattern for the voice to
  match).
- Sandbox default is `Sandbox.workspace_write` - the same posture Claude's
  `SANDBOX_SETTINGS` (`session/sdk.py:124-129`) establishes, not
  `Sandbox.full_access`. Do not let a Codex session run with unrestricted
  filesystem access by default even though the SDK makes that one enum value
  away.
- Every write-class tool call (per `tools/registry.py`'s `WRITE_CLASS`
  classification) must reach the human via the approval-handler adapter.
  Q1 and Q5 are now DECIDED (`roadmap.md`, `20260919_codex-approval-handler-finding.md`):
  (a) construct `ThreadStartParams(approvals_reviewer=ApprovalsReviewer.user,
  approval_policy=AskForApproval(root=AskForApprovalValue.on_request), ...)`
  directly rather than passing the curated `ApprovalMode` enum through
  `Codex.thread_start()` - `ApprovalMode.auto_review` maps to
  `ApprovalsReviewer.auto_review` (an AI reviewer), not `user`; (b) the
  handler must be wired via `CodexClient(config=..., approval_handler=...)`
  directly - the public `Codex`/`AsyncCodex`/`AsyncCodexClient` wrappers
  never accept or forward `approval_handler`, and constructing one of those
  instead would silently auto-accept every command-execution request via
  `CodexClient._default_approval_handler`. This is now a source-verified
  design decision, not an open question - still confirm once, in the manual
  integration pass, that `approvals_reviewer=ApprovalsReviewer.user`
  actually routes every request to the handler in practice (live behavior
  can still differ from the typed protocol's stated intent).
- `CodexClient` is fully synchronous/blocking (its own docstring: "Synchronous
  typed JSON-RPC client for `codex app-server` over stdio") with **no**
  working async variant that also supports `approval_handler`
  (`AsyncCodexClient` wraps `CodexClient` but does not forward
  `approval_handler` either). `CodexSession` must therefore own a dedicated
  background thread (or a single-worker `ThreadPoolExecutor`) that holds the
  one `CodexClient` instance and runs every blocking call
  (`start`/`initialize`/`thread_start`/`turn_start`/notification-stream
  iteration) on it; `CodexSession`'s async Protocol methods post work to that
  thread via `loop.run_in_executor(executor, ...)` and marshal
  results/events back via `loop.call_soon_threadsafe`. This is a materially
  larger thread-bridging surface than "just the approval callback" - every
  interaction with Codex crosses the bridge, not only approvals.
- The approval callback (`ApprovalHandler = Callable[[str, JsonObject | None],
  JsonObject]`, from `client.py`) runs synchronously on the SDK's own reader
  thread, not on mesh's asyncio loop. Bridge with
  `asyncio.run_coroutine_threadsafe(broker.ask(...), loop).result()` -
  blocking that one thread is correct and intentional (it mirrors
  `can_use_tool`'s "the turn is paused until answered" semantics), not a bug
  to route around.
- Decision-vocabulary mapping, confirmed from the app-server protocol docs:
  `Decision(allow=True, remember_tool=None)` -> `{"decision": "accept"}`;
  `Decision(allow=True, remember_tool=<tool>)` -> `{"decision":
  "acceptForSession"}`; `Decision(allow=False, ...)` -> `{"decision":
  "decline"}`. `acceptForSession` is scoped to the app-server's own process
  session, not persisted - mesh's `.mesh/permissions.toml` (via the broker,
  unchanged from Phase 1) remains the durable source of truth across mesh
  restarts; `acceptForSession` is a convenience layered on top, not a
  replacement for it.

## Approach sketch

```python
from openai_codex.client import CodexClient, CodexConfig
from openai_codex.generated.v2_all import (
    ApprovalsReviewer, AskForApproval, AskForApprovalValue, ThreadStartParams,
)

class CodexSession:
    def __init__(self, on_event, *, cwd, session_id, broker=None,
                 model=None, effort=None, resume=None,
                 on_sdk_session_id=None, trusted_config_digest=None):
        ...
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self._client = None  # CodexClient, constructed in start()

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self._client = CodexClient(
            config=CodexConfig(cwd=self.cwd),
            approval_handler=lambda method, params: self._approval_handler(
                method, params, loop
            ),
        )
        await loop.run_in_executor(self._executor, self._client.start)
        await loop.run_in_executor(self._executor, self._client.initialize)
        started = await loop.run_in_executor(
            self._executor,
            self._client.thread_start,
            ThreadStartParams(
                cwd=self.cwd,
                model=self._model,
                approval_policy=AskForApproval(root=AskForApprovalValue.on_request),
                approvals_reviewer=ApprovalsReviewer.user,  # Q1/Q5 fix - never
                # the curated ApprovalMode enum, which maps auto_review to an
                # AI reviewer, not the human handler.
                sandbox=...,  # Sandbox.workspace_write equivalent
            ),
        )
        ...

    def _approval_handler(self, method: str, params, loop) -> dict:
        # Runs on CodexClient's own internal reader thread
        # (client.py:855-861), not the executor thread and not the asyncio
        # loop. Do not confuse this with the executor bridge above - this is
        # a second, separate thread-crossing point, already documented as
        # correct/intentional in the design constraints.
        future = asyncio.run_coroutine_threadsafe(
            self._broker.ask(tool_name=..., ...), loop)
        decision = future.result()
        return _to_codex_decision(decision)
```

Every subsequent blocking `CodexClient` call (`turn_start`, notification
iteration via `TurnHandle`-equivalent hand-rolled subscription consumption,
`turn_interrupt`, `close`) follows the same `loop.run_in_executor(self._executor,
...)` pattern shown in `start()` above - the workflow implementing this
ticket should factor that into a small helper rather than repeating the
`run_in_executor` call at every call site.

## Acceptance criteria and tests

- Fake stdio-transport harness (mirrors `tests/test_sdk_session.py`'s
  `transport=` injection) drives `CodexSession` through canned app-server
  JSON-RPC frames: `initialize`/`initialized` handshake, a `turn/start` ->
  `item/commandExecution/requestApproval` -> approval-handler ->
  `turn/completed` sequence, and a decline path.
- `on_viewer_presence(0)` followed by an approval request denies via the
  broker's no-viewer grace path (unchanged broker behavior from Phase 1,
  exercised through this new driver).
- Manual integration pass against a real Codex account (subscription first,
  API key as a secondary check) completing an actual write-class turn end to
  end, since Q1/Q5's source-level resolution still benefits from one live
  confirmation that `approvals_reviewer=ApprovalsReviewer.user` behaves as
  documented in practice.

## Workflow shape

Implementation on sonnet, automated (fake-transport) tests on haiku, standard
+ adversarial review on opus - the adversarial pass's specific brief: attempt
to construct a turn where a write-class Codex tool call does *not* reach
`approval_handler` given the resolved design (direct `CodexClient` + explicit
`approvals_reviewer=ApprovalsReviewer.user`), and
confirm it cannot happen. Loop until clean and until the manual integration
pass succeeds.

## Open questions

- Q1 and Q5 (roadmap) are DECIDED at the source level - see
  `20260919_codex-approval-handler-finding.md`. What remains open is purely
  empirical: does `approvals_reviewer=ApprovalsReviewer.user` actually route
  every approval request to the handler against a real running
  `codex app-server`, confirmed only by the manual integration pass above
  (no dedicated Codex account was available during planning to verify this
  live).
- Exact shape of hand-rolling turn-notification consumption off
  `CodexClient`'s subscription/router internals (`_subscribe_turn_notifications`,
  `client.py:410-411`) without the convenience of `TurnHandle.stream()`
  (which is only reachable through the public `Codex`/`AsyncCodex` wrapper
  this ticket now avoids) - read `client.py`'s `_router`/`_TurnSubscription`
  internals in full before implementing the notification-consumption loop.
