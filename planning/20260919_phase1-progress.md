# Phase 1 progress report - permission-broker decoupling

Date: 2026-09-19
HEAD at completion: (working tree, uncommitted at report time - see commit
that follows this report for the landing SHA)

Status: **COMPLETE**. Ticket `planning/tickets/phase1_permission-broker-decoupling.md`
executed via the Dynamic Workflow tiering (implementation/sonnet, test/haiku,
review/opus, looped) end to end, one loop iteration, now clean.

## What landed

- `src/annealage_mesh/session/permissions.py`: new provider-neutral
  `Decision` frozen dataclass (`allow: bool`, `remember_tool:
  Optional[str] = None`, `message: str = ""`) replaces the
  `PermissionResult = Union[PermissionResultAllow, PermissionResultDeny]`
  alias. `ask()`, `decide()`, `_build_result()`, and every fast-path return
  site (shutdown, remembered-grant, no-viewer, emit-failure, timeout) now
  construct and return `Decision`. `claude_agent_sdk`/`claude_agent_sdk.types`
  imports removed entirely (`grep -rn claude_agent_sdk session/permissions.py`
  empty). `ask()`'s `context` parameter is now `Any` (backend-opaque).
  `NEVER_REMEMBERED = frozenset({"Bash"})` enforcement unchanged - traced and
  independently re-verified by the review.
- `src/annealage_mesh/session/sdk.py`: new `_to_claude_result(decision:
  Decision) -> PermissionResult` adapter and `_session_rule(tool_name) ->
  PermissionUpdate` (moved here verbatim from `permissions.py`). `SdkSession`
  no longer binds `options.can_use_tool = self._broker.ask` directly; it
  binds a new bound method `self._can_use_tool` that awaits
  `broker.ask(...)` then calls `_to_claude_result(decision)`, reproducing the
  exact `PermissionResultAllow`/`PermissionResultDeny` construction the old
  inline code built - confirmed byte-for-byte by manual trace in review.
  `from __future__ import annotations` added so `_can_use_tool`'s
  forward-referenced return type resolves lazily.
- `tests/test_permissions.py`: rewritten around `Decision` (`_assert_allow`/
  `_assert_deny`/`_assert_remembers` replace the old
  `isinstance(..., PermissionResultAllow/Deny)` + `_assert_session_rule`
  pattern); all 29 original behaviors preserved.
- `tests/test_sdk_session.py`: `can_use_tool` wiring assertion updated to
  `options.can_use_tool == session._can_use_tool`; four new tests added
  (post-review-loop, see below) directly exercising `_to_claude_result`/
  `_session_rule`, pinning the exact `PermissionUpdate`/`PermissionRuleValue`
  shape the pre-refactor suite used to pin.

Net diff: 4 files, +277/-141.

## Review loop

One finding surfaced by the (opus-tier) review, Medium severity: the new
`_to_claude_result`/`_session_rule` adapter functions in `sdk.py` were not
exercised by any test (the stub broker in `test_sdk_session.py` deliberately
raised if called), and `test_permissions.py`'s new module docstring falsely
claimed dedicated coverage existed for `_to_claude_result` in
`test_sdk_session.py`. The review's adversarial pass (forcing an exception at
the adapter call site, tracing `NEVER_REMEMBERED`) came back clean on
everything else, with two notable findings-that-weren't-findings recorded for
the record:

- The two-step `ask()` -> adapter architecture does introduce a call site
  (`_to_claude_result`, outside `ask()`'s own try/except) where a
  hypothetical adapter bug could propagate to the SDK. Confirmed this is
  **not a regression**: the equivalent pre-refactor inline construction was
  also outside any try/except, at the same logical point. Risk surface
  unchanged, merely relocated.
- `Decision` is a frozen dataclass constructed fresh at every call site, so
  the "missing `remember_tool=None` reset" failure mode the adversarial brief
  asked about is structurally impossible (no shared mutable instance to leave
  stale).

Fix: four new tests added to `test_sdk_session.py` exercising
`_to_claude_result` (bare allow, allow-with-remember pinning the exact
`PermissionUpdate(type="addRules", behavior="allow", destination="session",
rules=[PermissionRuleValue(tool_name=..., rule_content=None)])` shape, and
deny) and `_session_rule` directly; docstring corrected. Re-review: CLEAN,
confidence 0.99, zero findings.

## Test results

Full suite excluding the pre-existing-flaky, unrelated Playwright e2e file
(`tests/test_viewer_e2e.py` - confirmed broken in this sandbox by a headless-
Chromium WebGL rendering timeout, nothing to do with this change, sampled and
traced by the haiku-tier test agent): **971 passed, 0 failed, 0 errors**
(967 before the fix's 4 new tests).

## Deviations from the ticket

None of substance. The ticket's approach sketch matched the shipped shape
exactly (`Decision` in `permissions.py`, adapter in `sdk.py`,
`_session_rule` relocated rather than generalized).

## Infrastructure note for future phases

The `reviewer` and `sonic` (haiku-tier) subagent types are currently backed
by a provider (`openai-codex/gpt-5.6-terra`) that hit `usage_limit_reached`
partway through this phase - twice in a row for `reviewer`, once for
`sonic`. Both eventually recovered (a later `reviewer` call succeeded; the
`sonic` retest was run directly instead of re-queued). Future phases should
expect this and have a fallback ready: the general-purpose `task` agent type
(different, unaffected backend) can stand in for a review pass at the same
rigor bar if `reviewer` is down, and mechanical commands (test runs) can be
run directly via `bash` instead of a `sonic` subagent if `sonic` is down.
Neither fallback changes the acceptance bar, only who/what executes it.

## What the next phase needs to know

Nothing changes about Phase 2's ticket (`planning/tickets/phase2_settings-backend-switch.md`)
as a result of this phase - it has no dependency on `permissions.py`'s
internals beyond the `AgentSession` Protocol, which did not change. Phase
3/4's tickets should note, when revalidated at their own phase entry, that
`Decision` (not the old Claude types) is now the real return type of
`PermissionBroker.ask()` - their approach sketches already assumed this, so
no rewrite needed, just confirm at revalidation time that the shipped
`Decision` shape (`allow`, `remember_tool`, `message`) matches what those
tickets already reference.
