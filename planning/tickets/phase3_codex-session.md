# session/codex.py: CodexSession (core driver)

Phase: 3
Depends on: Phase 1 (permission-broker `Decision` type), Phase 2 (`backend`
setting, `build_session` branch point)
Written: 2026-09-19 at HEAD 58f78db34e

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
`AgentSession` Protocol against `openai_codex.AsyncCodex`; OAuth surfacing;
sandbox posture; the approval-handler adapter; resolving Q1.

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
- `cli.py:724-773` (post-Phase-2) - the `backend == "codex"` branch in
  `build_session` replaces its Phase-2 `NotImplementedError` stub with the
  real construction call, mirroring the `SdkSession(...)` call's keyword
  shape (`cwd`, `session_id`, `broker`, `model`, `effort`,
  `on_sdk_session_id`, `trusted_config_digest` as applicable - `permission_mode`
  is Claude-only per the settled design decision in `roadmap.md` and has no
  Codex equivalent to pass).
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
  This is the property Q1 threatens and this ticket must resolve before
  shipping: `ApprovalMode.auto_review`'s docstring says "High-level approval
  behavior for **escalated** permission requests", which reads as
  auto-approve-most / escalate-some rather than always-ask. Confirm against a
  real running `codex app-server` (not just source reading) whether setting
  `thread_start`/`thread_resume`'s raw `config={...}` override to force
  `ApprovalsReviewer.user` (bypassing the curated `ApprovalMode` enum
  entirely) makes every `item/commandExecution/requestApproval` and
  `item/fileChange/requestApproval` reach `approval_handler`. If it does not,
  this ticket is blocked pending a design note - do not ship an approval path
  believed to skip the human for some commands.
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
class CodexSession:
    def __init__(self, on_event, *, cwd, session_id, broker=None,
                 model=None, effort=None, resume=None,
                 on_sdk_session_id=None, trusted_config_digest=None):
        ...
        self._codex = None  # AsyncCodex, constructed in start()

    async def start(self) -> None:
        self._codex = AsyncCodex(config=CodexConfig(
            cwd=self.cwd,
            approval_handler=self._approval_handler,  # verify exact
            # constructor param name/whether it lives on CodexConfig or is
            # passed alongside it - client.py showed it as a separate
            # CodexClient(config, approval_handler) argument, not a
            # CodexConfig field; confirm which shape AsyncCodex's public
            # api.py actually exposes before assuming this call shape.
        ))
        await self._codex.start()
        thread = await self._codex.thread_start(
            model=self._model, sandbox=Sandbox.workspace_write,
            config={"approvalsReviewer": "user"},  # placeholder pending Q1
        )
        ...

    def _approval_handler(self, method: str, params) -> dict:
        # runs on the SDK's reader thread, not the asyncio loop
        future = asyncio.run_coroutine_threadsafe(
            self._broker.ask(tool_name=..., ...), self._loop)
        decision = future.result()
        return _to_codex_decision(decision)
```

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
  end, since Q1 cannot be fully settled from the fake transport alone.

## Workflow shape

Implementation on sonnet, automated (fake-transport) tests on haiku, standard
+ adversarial review on opus - the adversarial pass's specific brief: attempt
to construct a turn where a write-class Codex tool call does *not* reach
`approval_handler` given whatever Q1's resolution turned out to be, and
confirm it cannot happen. Loop until clean and until the manual integration
pass succeeds.

## Open questions

- Q1 (roadmap): does `ApprovalMode.auto_review` route every write-class call
  to `approval_handler`, or only escalated ones? Must resolve before the
  approval-adapter work item is considered done, not deferred to review.
- Whether `approval_handler` is a `CodexConfig` field or a separate
  constructor argument on the public `AsyncCodex`/`Codex` classes (`api.py`
  was not read in this research pass - `client.py`'s lower-level
  `CodexClient(config, approval_handler)` showed it as separate, but the
  public wrapper may thread it differently). Read `sdk/python/src/openai_codex/api.py`
  directly before writing the constructor call.
