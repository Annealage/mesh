# Live model selection: protocol frame, per-driver switch, webui picker

Phase: 5
Depends on: Phase 3 (`CodexSession`), Phase 4 (`OmpSession`) both must exist;
Phase 2 (`model` setting as the starting default)
Written: 2026-09-19 at HEAD 58f78db34e
Revalidated: 2026-09-20 at HEAD e50c36e - Q3 DECIDED, and resolved better than guessed: claude_agent_sdk's ClaudeSDKClient has a genuine live set_model(model) control-plane call, no reconnect needed (session/sdk.py's approach sketch below rewritten accordingly). Confirmed by reading source: Codex's TurnStartParams.model genuinely overrides "for this turn and subsequent turns" within the same thread (openai_codex 0.154.0's generated/v2_all.py) - the Codex anchor below was correct as originally sketched. session/codex.py and session/omp.py both landed in Phases 3/4 with self._model set once at construction/thread_start time and never re-read per-turn yet - this ticket adds that. protocol.py/session/base.py/http/ws.py/static/js/chat.js anchors not yet re-verified against current line numbers - re-locate by name, Phase 1/2/3/4 all touched files in this area.

## Context

The user's explicit requirement: backend is CLI-configured (a settings key,
Phase 2); model selection is a live webui control, with its *default*
configured the same way as the backend (i.e. also a settings key, not only a
per-invocation flag). Codex and omp both support changing the active model
`claude_agent_sdk` does not have an equivalent primitive - or so this
roadmap assumed until Q3's revalidation found `ClaudeSDKClient.set_model()`,
a genuine first-class live control-plane call. All three backends turn out
to support live switching natively; there is no "refuse and require a
restart" path needed for any of them.

## Scope

In scope: new inbound WS frame `set_model`, new `AgentSession.set_model(...)`
Protocol member, new `AgentModelChanged` event, per-driver implementation
(native live switch on all three backends - Claude via `ClaudeSDKClient.set_model()`,
Codex via `TurnStartParams.model`, omp via the RPC `set_model` command),
`hello` frame carrying the starting model, webui picker control and
model-list sourcing.


Out of scope: anything about *which* model is available on a given
backend/endpoint beyond straightforward enumeration (`codex.models()`,
`get_available_models`) - no new capability for the backend/endpoint itself.

## Files and anchors

- `protocol.py:296-307` - `_INBOUND_SPECS`. Add:
  `"set_model": _Spec({"model"}, {"model"})`, following the exact pattern of
  `"pause": _Spec({"paused"}, {"paused"}, _check_pause)` at line 306 (no
  `check` function needed unless model-name validation beyond "is it a
  string" is wanted - probably not, since valid values are backend/endpoint-
  dependent and not enumerable at the protocol layer).
- `protocol.py:127-161` - `build_hello`. Add the effective starting model to
  the `"session"` dict, same pattern as `"agent": agent_status` at line 157 -
  needs a new parameter threaded through from `http/ws.py`'s call site.
- `session/base.py:313-395` - `AgentSession` Protocol. Add:
  `async def set_model(self, model: str) -> None: ...` alongside the other
  four coroutine members (`submit_turn`, `decide_permission`, `interrupt`,
  plus the lifecycle pair).
- `session/base.py` - new event class. **Naming hazard, read this file's
  existing `ModelsChanged` class (`session/base.py:217-236`) before naming
  anything**: that class is "the served directory's set of models, or one
  model's bytes, changed" - i.e. the project's *3D printable STL files*, a
  completely unrelated concept that happens to share the word "model". The
  new LLM-model-selection event must not be named anything that reads as a
  variant of that class (`ModelChanged`, `ModelsChanged2`, etc. are all
  traps). Use `AgentModelChanged(model: str)`, matching the existing
  `AgentStatus`/`AgentError` naming convention (the `Agent`-prefixed events
  are about the conversation driver itself, not the served project).
- `http/ws.py:360-465` - `_dispatch`. Add a branch for `frame["type"] ==
  "set_model"` calling `session.set_model(frame["model"])`, following
  whichever existing branch (`"permission"` or `"interrupt"`) is structurally
  closest - read the full function before adding, it is currently only
  partially read in this research pass (lines 360-465 were read but the
  per-type branches' exact bodies were elided in the structural summary).
- `session/codex.py` (Phase 3, landed) - `_model` is currently set once in
  `__init__` and read at `thread_start`/`TurnStartParams` construction
  (search for `self._model` - re-locate exactly, do not trust a line
  number). `set_model` should store the new value and every subsequent
  `TurnStartParams` construction (in `submit_turn`) must read the current
  `self._model`, not a value captured at thread-start time - confirmed via
  source that `TurnStartParams.model` genuinely overrides "for this turn and
  subsequent turns" within the same thread, so no new thread/reconnect is
  needed.
- `session/omp.py` (Phase 4, landed) - `_model` is currently set once in
  `__init__` (search for `self._model`). `omp://rpc.md`'s documented
  `{ type: "set_model", provider: string, modelId: string }` command is the
  wire shape; confirm `RpcClient`'s actual Python method name/signature for
  it against the installed `omp_rpc` source before assuming a call shape -
  not verified in this research pass.
- `session/sdk.py` - `set_model` now has a confirmed, simple implementation:
  `await self._client.set_model(model)` directly on the live
  `ClaudeSDKClient` (`claude_agent_sdk/client.py:350-372`), which sends a
  `{"subtype": "set_model", "model": model}` control request
  (`claude_agent_sdk/_internal/query.py:788-796`) over the already-connected
  session. No `close()`/`start()`/`resume=` cycle needed - this replaces the
  ticket's original reconnect-based approach sketch entirely.
- `static/js/chat.js:229-236` (`AGENT_LABEL`/`AGENT_TITLE` constants) and
  `initChat` (237-805, only partially read) - add a model-picker control;
  read the composer's existing DOM-construction pattern in the unread middle
  of `initChat` before adding a sibling control, to match conventions (event
  wiring via `send`, `store` subscription pattern already used by
  `chat.js`/`settings.js`).
- `static/js/settings.js` - not touched by this ticket (the live picker is
  deliberately *not* routed through the settings save/reload flow
  `settings.js` implements - see Design constraints).

## Design constraints

- The live picker is session-scoped, not a settings write. Do not route
  `set_model` through `PUT /settings` (`http/routes_settings.py`) - that
  route's whole contract (`routes_settings.py:17-30`, "effective and saved
  are different questions") is about persisted, restart-or-load-effect
  configuration; a live in-session model change is neither and would confuse
  that route's `pending`-vs-effective reporting.
- `AgentModelChanged` must not collide, in name or in the browser's event
  dispatch, with the existing `ModelsChanged` (STL files) - if `chat.js`
  and/or `models.js` (`static/js/models.js`, not read in this research pass)
  share an event-handling switch statement, confirm the two are
  distinguishable there too, not only in `base.py`.
- All three backends now support live switching natively (Q3's revalidated
  resolution) - there is no "refuse and require a restart" path to design
  for any of them. If a future backend genuinely cannot switch live, follow
  the same shape this bullet originally described (a clear refusal via
  `protocol.build_refused(...)`, never a silent no-op), but that is not
  needed for Claude/Codex/omp as they stand today.

## Approach sketch

All three drivers now have a confirmed, simple live-switch mechanism - no
driver needs the reconnect workaround the ticket originally sketched for
Claude:

```python
# session/sdk.py
async def set_model(self, model: str) -> None:
    if model == self._model:
        return
    await self._client.set_model(model)  # live control-plane call,
    self._model = model                   # no reconnect

# session/codex.py
async def set_model(self, model: str) -> None:
    self._model = model  # read by the next TurnStartParams construction

# session/omp.py
async def set_model(self, model: str) -> None:
    await self._run_blocking(self._client.set_model, provider=..., model_id=model)
    self._model = model
    # confirm RpcClient's actual method name/signature against omp_rpc
    # source before implementing this literally - not verified here.
```

## Acceptance criteria and tests

- `protocol.py`'s inbound-frame validation test suite covers `set_model`
  (missing `model` key rejected, non-string `model` rejected - matching
  `_check_pause`'s pattern of type-checking, `protocol.py:266-274`).
- Per-driver unit test (fake transport): `set_model` followed by
  `submit_turn` results in the new model actually being passed to the
  underlying client call (`thread.turn(model=...)` for Codex,
  `client.set_model(...)` call recorded for omp, reconnect-with-new-model
  recorded for Claude if Q3 is positive).
- `hello` frame's session object carries the starting model; a fresh webui
  tab shows the correct picker value with no flash of a wrong default.
- Manual/browser check: switching model mid-conversation on each backend
  visibly changes behavior on the next turn (or, for Claude if Q3 is
  negative, produces the documented refusal).

## Workflow shape

Implementation on sonnet, automated tests on haiku, standard review on opus
(not adversarial - additive UI/plumbing feature, low security surface; the
one thing worth a sharper look is the Claude-refusal-vs-silent-no-op
distinction called out above, which a standard review can catch).

## Open questions

- `omp_rpc.RpcClient`'s actual Python method name/signature for the
  `set_model` RPC command - `omp://rpc.md` documents the wire shape
  (`{type: "set_model", provider, modelId}`) but this ticket's revalidation
  pass did not confirm the client library's exposed method against its
  installed source (Phase 4's implementer already resolved similar
  questions for other commands - check `session/omp.py`'s existing
  `_run_blocking` call sites for the established pattern first).
- Whether `static/js/models.js` (not read in this research pass) shares an
  event-handling switch statement with `chat.js` that also needs to
  distinguish `AgentModelChanged` from `ModelsChanged` - confirm before
  wiring the webui picker.
