# Phase 5 progress report - live model selection

Date: 2026-09-20
Status: **COMPLETE**. Ticket `planning/tickets/phase5_live-model-selection.md`
executed via the Dynamic Workflow tiering, four fix-loop iterations, now
clean. The longest review loop of the six phases - see below for why.

## Research correction before implementation

Q3 originally guessed Claude would need a reconnect-with-`resume=` workaround
to switch models live. Reading `claude_agent_sdk`'s installed source directly
found something better: `ClaudeSDKClient.set_model(model)` is a genuine
first-class live control-plane call (`_internal/query.py:789`, `{"subtype":
"set_model", "model": model}` sent over the already-open transport) - no
reconnect needed at all. Codex's `TurnStartParams.model` was independently
confirmed to override "for this turn and subsequent turns" within the same
thread. All three backends ended up with a native live-switch mechanism;
the ticket's original "refuse and require a restart" fallback path for
Claude was dropped entirely as unnecessary.

## What landed

- `protocol.py`: `set_model` inbound frame, `build_hello` carries the
  starting model.
- `session/base.py`: `AgentSession.set_model` Protocol member,
  `AgentModelChanged` event (deliberately named/documented to avoid any
  confusion with the existing `ModelsChanged` - served STL files - event).
- `http/ws.py`, `app.py`: frame dispatch; `session_info['model']` kept
  live in place as switches happen, not only set once at startup.
- Per-driver: `session/sdk.py` calls the live SDK method directly;
  `session/codex.py` stores the new model and (after a fix - see below)
  actually sends it on every subsequent turn; `session/omp.py` calls the RPC
  `set_model` command through the same executor bridge every other blocking
  call already uses.
- `static/js/`: a model-picker input wired into `chat.js`/`store.js`/
  `ws.js`/`main.js`/`viewer.html`/`app.css`, with local pending-request
  tracking, focus-aware revert-on-refusal, and reconnect reconciliation via
  `handleHello`.

## Bugs found during implementation (before any review)

- **Codex never sent a model per turn at all.** `submit_turn`'s
  `TurnStartParams` construction only ever included `effort`; the
  thread-level `model=` set once at `thread_start`/`thread_resume` was the
  only place a model ever reached Codex. A live `set_model()` call updated
  `self._model` locally but the change never reached the running
  conversation. Fixed as part of the initial implementation, not left for
  review to catch.

## Review loop (four passes - the longest of any phase)

**Pass 1** (opus, standard - not adversarial per the ticket's stated
workflow shape, since this was scoped as additive UI/plumbing): 4 findings,
1 priority-1. The local/omp backend's live switch was **actually broken as
shipped** - omp's `RpcClient.set_model` rejects any model id not already
registered in the session's `models.yml`, which only listed the one startup
model. Fixed by adding `discovery: {type: proxy}` to the generated
custom-provider config. Also fixed: `session/fake.py`'s `FakeSession`
(used by app/WS tests) was never migrated to the new Protocol member and
would raise `AttributeError`; the picker left a rejected value stuck
displayed on refusal; `session_info['model']` was never updated after a
live switch, so a fresh tab could see a stale startup value.

**Pass 2**: re-review surfaced a narrower residual - the refusal handler
reverted the picker on *any* refused frame, not only ones caused by its own
`set_model` request (the WS `refused` frame carries no correlation id at
all), and a focus race could still leave a rejected value stuck. Fixed with
local `pendingSetModel`/`queuedModelRevert` client-side state.

**Pass 3**: re-review found the pending-state tracking itself could leak -
`pendingSetModel` was set before `send()`, but `send()` silently no-ops on a
closed socket, and a genuinely transmitted request's response could be lost
across a disconnect/reconnect with nothing to reconcile it. Fixed: `send()`
now reports whether it actually transmitted (reverting immediately if not),
and `handleHello` reconciles/clears pending state on every reconnect,
verified live in a real browser across both scenarios (disconnected submit,
in-flight-across-reconnect) using real keyboard-driven interaction against a
throwaway scripted server.

**Pass 4**: this same live-browser verification pass surfaced an unrelated
but real regression, not caught by any of the first three review passes'
static analysis: `tests/test_viewer_e2e.py`'s `chat_server` fixture (used by
~35 tests) crashed with `AttributeError` in `routes_mcp.py`, independent of
the pre-existing WebGL/headless-Chromium sandbox limitation. Root cause:
Phase 3's `register_mcp_routes` wiring assumed `mesh_tools` is never `None`
whenever a real `session` exists, which held for the real `cli.py` path but
not for a test fixture supplying a real `build_session` factory without a
real `mesh_session_id`. **First fix attempt was itself wrong**: gating
`mesh_tools` construction on `build_session is not None` fixed the crash but
introduced a worse regression - `cli.py`'s real `build_session` closure is
*always* a real callable (its mode check happens internally when called),
so every `annealage-mesh view`/`--no-agent` run would have started
unconditionally importing `claude_agent_sdk` and building an unused MCP tool
server. Caught by the next review pass before landing. Correct fix: reverted
`app.py` to the original `mesh_session_id is not None` gate, and instead
fixed the actual defect - the test fixture itself - to call
`sessions.create_session(d)` and pass a real `mesh_session_id`, exactly
matching the adjacent `settings_server` fixture's already-established
pattern in the same file. Final review confirmed clean.

## Test results

Full suite excluding the pre-existing, unrelated WebGL/headless-Chromium
sandbox limitation in `test_viewer_e2e.py`: **1048 passed, 0 failed, 0
errors** (independently re-run after every fix, not only trusted from
implementer reports). The specific `AttributeError` crash that used to
block `chat_server`-based e2e tests entirely is confirmed fixed (the file's
tests now only hit the pre-existing WebGL limitation, verified by directly
re-running the previously-crashing test).

## Known residual limitation (accepted, not fixed)

The refusal-handling client state still has one narrow, self-correcting
edge case: since the WS `refused` frame carries no correlation id at all
(`protocol.build_refused` is just `{v, type, reason}`), an unrelated
refusal arriving while a `set_model` is genuinely pending can still be
misattributed to it - reviewed and traced to confirm this never leaves a
*wrong* value stuck (a switch that actually succeeds self-corrects via the
unconditional `agent_model_changed` handler moments later; a switch that
actually failed just gets a redundant second revert), only a possible
spurious toast/premature revert in a race that is rare in practice. A
complete fix requires adding a correlation id to the wire protocol's
`refused` frame - a change affecting every frame type, not only
`set_model`, and explicitly out of this ticket's scope. Documented here
rather than silently dropped; a future ticket adding request correlation to
the WS protocol generally should close this as a side effect.

## What the next phase needs to know

Phase 6 (diagnostics) has no dependency on anything Phase 5 touched.
