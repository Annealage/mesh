# session/permissions.py: decouple PermissionBroker from claude_agent_sdk

Phase: 1
Depends on: nothing (foundation ticket)
Written: 2026-09-19 at HEAD 58f78db34e
Revalidated: 2026-09-19 at HEAD 58f78db34e - no drift, HEAD unchanged since Written

## Context

`session/permissions.py`'s `PermissionBroker` is the human-approval gate
every future backend must reuse unchanged: the future/timeout/no-viewer/
shutdown machinery and `.mesh/permissions.toml` grant persistence are already
generic. One spot is not: `ask()` builds and returns
`claude_agent_sdk.PermissionResultAllow`/`PermissionResultDeny` directly, and
`_session_rule()` returns a `claude_agent_sdk.PermissionUpdate`. Without this
ticket, `CodexSession` (Phase 3) and `OmpSession` (Phase 4) would each have
to import `claude_agent_sdk` just to interpret the broker's return type -
exactly the coupling the multi-backend project exists to remove.

See `planning/20260919_multi-backend-research.md`'s "Settled architecture"
table and `planning/roadmap.md`'s Phase 1 section.

## Scope

In scope: change `PermissionBroker.ask()`'s return type to a provider-neutral
`Decision`, and everything inside `session/permissions.py` that currently
constructs a Claude SDK type. Add the one Claude-specific adapter to
`session/sdk.py` so `SdkSession`'s externally observable behavior is
unchanged.

Out of scope: `CodexSession`/`OmpSession`'s own adapters (Phase 3/4 tickets);
any change to the broker's timeout/grant/persistence logic itself.

## Files and anchors

- `session/permissions.py:61-62` - `from claude_agent_sdk import
  PermissionResultAllow, PermissionResultDeny, PermissionUpdate` and
  `from claude_agent_sdk.types import PermissionRuleValue,
  ToolPermissionContext`. Remove both once nothing below references them.
- `session/permissions.py:71` - `PermissionResult = Union[PermissionResultAllow,
  PermissionResultDeny]` type alias. Replace with the new `Decision` type
  (defined in this file or `session/base.py` - prefer `session/permissions.py`
  since it is the only current consumer; move to `base.py` only if a second
  consumer needs it at import time before `permissions.py` does).
- `session/permissions.py:207-212` - `ask()`'s shutdown/remembered-grant/
  no-viewer fast paths, currently `return PermissionResultDeny(...)` /
  `return PermissionResultAllow(updated_permissions=[_session_rule(tool_name)])`.
- `session/permissions.py:234-248` - the awaited-future and timeout paths,
  same pattern.
- `session/permissions.py:269` - `decide()`, the inbound-frame handler; check
  whether it also touches Claude types (it dispatches to the same
  `_build_result`/future-resolution code, so likely yes via
  `future.set_result(...)` at line 459).
- `session/permissions.py:459` - `future.set_result(PermissionResultDeny(message=message))`.
- `session/permissions.py:468-491` - `_build_result(tool_name, decision,
  message) -> Tuple[PermissionResult, Optional[str]]`, the pure/synchronous
  decision-to-result mapping for `allow`/`deny`/`allow_always`. This is the
  natural place to construct `Decision` instead.
- `session/permissions.py:494-511` - `_session_rule(tool_name) ->
  PermissionUpdate`. This one genuinely is Claude-specific (it is the SDK's
  own rule-matcher mechanism for "grant lasts the rest of this process's
  session"); it moves to the new `session/sdk.py` adapter rather than being
  generalized, since Codex/omp have their own, different remembered-grant
  mechanisms (`acceptForSession` / broker-side interception respectively -
  see Phase 3/4 tickets).
- `session/sdk.py` - new adapter, e.g. `_to_claude_result(decision:
  Decision) -> PermissionResult`, called from wherever `SdkSession`'s
  `can_use_tool` callback currently receives `ask()`'s return value directly
  (not yet read in this ticket's research pass - locate it in `session/sdk.py`
  lines 264-748, the unread middle of the file, before starting).
- `tests/test_sdk_session.py` (32.9 KB, not read in this research pass) -
  almost certainly asserts on `PermissionResultAllow`/`PermissionResultDeny`
  shapes somewhere; audit and update for the new adapter boundary.

## Design constraints

- The module's own stated invariant (docstring, `session/permissions.py:6-11`):
  "every path through `ask` returns a `PermissionResultAllow` or
  `PermissionResultDeny` and nothing here ever raises out of `ask` itself...
  a returned dict, or an uncaught exception, both surface to the model as an
  infrastructure error rather than a decision, which is worse than a denial
  with a clear reason." Rewrite this invariant to say `Decision` instead of
  the two Claude types, but the invariant itself - no path raises, every path
  returns a decision - must survive unchanged. This is exactly what the
  adversarial review in Phase 1's workflow shape checks.
- `NEVER_REMEMBERED = frozenset({"Bash"})` (`session/permissions.py:100`) and
  its enforcement must be preserved verbatim - it is a security property
  ("a broad standing grant for the one tool with unrestricted shell access
  would turn one careless click into a permanent bypass"), not incidental
  behavior.
- Do not change `PermissionBroker`'s public method names/signatures
  (`ask`, `decide`) beyond the return-type change - `SdkSession` and its
  tests call them positionally/by-keyword today and only the interior of
  what they return should move.

## Approach sketch

```python
@dataclasses.dataclass(frozen=True)
class Decision:
    allow: bool
    remember_tool: Optional[str] = None  # set only on an allow_always grant
    message: str = ""
```

`_build_result` becomes the single place that builds `Decision` for
`allow`/`deny`/`allow_always`; `ask()`'s fast paths (shutdown, remembered
grant, no-viewer, timeout) build `Decision` directly, matching their current
inline `PermissionResultDeny(...)`/`PermissionResultAllow(...)` calls
one-for-one. `session/sdk.py` adds:

```python
def _to_claude_result(decision: Decision) -> PermissionResult:
    if decision.allow:
        updates = [_session_rule(decision.remember_tool)] if decision.remember_tool else []
        return PermissionResultAllow(updated_permissions=updates)
    return PermissionResultDeny(message=decision.message)
```

with `_session_rule` moved into `session/sdk.py` alongside it (it is a
`PermissionUpdate` builder, Claude-specific by construction).

## Acceptance criteria and tests

- `grep -r claude_agent_sdk session/permissions.py` returns nothing.
- Full existing test suite (`tests/test_sdk_session.py` plus any dedicated
  permission-broker tests) passes with only import/type-construction changes
  visible in the diff - no test's *asserted behavior* changes (timeout
  duration, no-viewer grace window, remembered-grant persistence to
  `.mesh/permissions.toml`, `NEVER_REMEMBERED` refusal).
- New/updated test: `ask()` under every decision path (`allow`, `deny`,
  `allow_always`, timeout, no-viewer, shutdown) returns a `Decision` with the
  expected fields, independent of any Claude SDK import.
- Adversarial check: force an internal exception inside whatever code path
  used to be guarded by the module's "never raises" invariant, confirm it
  still cannot escape `ask()`.

## Workflow shape

Implementation on sonnet, test run on haiku, standard + adversarial review on
opus (adversarial pass specifically re-verifies the "never raises, always
returns a decision" invariant survived the refactor, and that
`NEVER_REMEMBERED` enforcement is unchanged). Loop until clean.

## Open questions

None specific to this ticket. It is a prerequisite for Q1/Q2/Q4 in
`roadmap.md`, not a source of new ones.
