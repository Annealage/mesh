# Live model selection: protocol frame, per-driver switch, webui picker

Phase: 5
Depends on: Phase 3 (`CodexSession`), Phase 4 (`OmpSession`) both must exist;
Phase 2 (`model` setting as the starting default)
Written: 2026-09-19 at HEAD 58f78db34e

## Context

The user's explicit requirement: backend is CLI-configured (a settings key,
Phase 2); model selection is a live webui control, with its *default*
configured the same way as the backend (i.e. also a settings key, not only a
per-invocation flag). Codex and omp both support changing the active model
mid-session natively; Claude does not have an equivalent primitive in
`claude_agent_sdk` as far as this research established, so this ticket must
resolve Q3 before Claude's path can be more than a documented limitation.

## Scope

In scope: new inbound WS frame `set_model`, new `AgentSession.set_model(...)`
Protocol member, new `AgentModelChanged` event, per-driver implementation
(native for Codex/omp, reconnect-or-refuse for Claude pending Q3), `hello`
frame carrying the starting model, webui picker control and model-list
sourcing.

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
- `session/codex.py` (Phase 3) - `set_model` stores `self._model = model`;
  `submit_turn` passes `model=self._model` to `thread.turn(...)` on every
  call, not only the next one (a turn started before the switch and a new
  turn after it must both reflect current state correctly - verify there is
  no unwanted mid-turn model change via `turn.steer()`, which should not
  carry a model override).
- `session/omp.py` (Phase 4) - `set_model` calls
  `client.set_model(provider=self._provider, model_id=model)` directly (the
  RPC command is literally named this - no adapter needed beyond argument
  naming).
- `session/sdk.py` - `set_model` needs Q3 resolved first (see Open
  questions).
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
- Claude's path (Q3) must fail loudly and specifically ("model can only be
  changed by restarting mesh" or similar, surfaced as a normal chat-pane
  message or a refused-frame response) if live switching turns out to be
  unsupported - never a silent no-op where the picker shows a new value but
  the next turn quietly uses the old model.

## Approach sketch

For Claude, if Q3 resolves positively:

```python
async def set_model(self, model: str) -> None:
    if model == self._model:
        return
    await self.close()
    self._model = model
    self._resume = self.sdk_session_id  # reconnect into the same conversation
    await self.start()
```

matching the existing `-c`/`-r` resume mechanism's shape
(`session/sdk.py:192,208`, `cli.py:764` `resume=_resumable_sdk_id(...)`) -
this is not new machinery, it is the existing reconnect path triggered from a
different caller.

If Q3 resolves negatively, `set_model` on `SdkSession` should raise/return a
refusal mesh's WS layer turns into a `protocol.build_refused(...)` frame
(`protocol.py:186-195`) rather than attempting a reconnect that would not
actually change the model.

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

- Q3 (roadmap): does `claude_agent_sdk` honor a changed `model=` on
  `resume=<id>`? Blocks whether Claude gets live switching or a documented
  refusal - resolve before this ticket's Claude work item is implemented,
  not discovered during review.
