# Design note: openai-codex has no async approval_handler path

Date: 2026-09-19
HEAD: 2a9fce9
Verified against: `openai-codex` 0.154.0 (bundling `openai-codex-cli-bin`
0.154.0), installed and read directly in a scratch venv
(`/tmp/codex_probe`), not against a live Codex account. This corrects and
supersedes part of Q1 in `roadmap.md` and the approach sketch in
`planning/tickets/phase3_codex-session.md`.

## What Q1 originally asked

Does the curated `ApprovalMode.auto_review` route every write-class Codex
tool call to `approval_handler`, or only ones the app-server's own
auto-reviewer escalates?

## What source-reading actually found (two findings, the second bigger than the first)

### Finding A: `ApprovalMode.auto_review` does route to an AI auto-reviewer, not the human

`openai_codex/_approval_mode.py`:

```python
class ApprovalMode(str, Enum):
    """High-level approval behavior for escalated permission requests."""
    deny_all = "deny_all"
    auto_review = "auto_review"

def _approval_mode_settings(approval_mode):
    match approval_mode:
        case ApprovalMode.auto_review:
            return (AskForApproval(root=AskForApprovalValue.on_request),
                     ApprovalsReviewer.auto_review)
        ...
```

`ApprovalsReviewer` (`openai_codex/generated/v2_all.py:254-257`) is a
three-way enum: `user`, `auto_review`, `guardian_subagent`. Confirms the
original suspicion: the curated `ApprovalMode.auto_review` sets
`approvals_reviewer = ApprovalsReviewer.auto_review` - an automated reviewer,
not `user`. Fix (as the ticket already anticipated): construct
`ThreadStartParams`/`V2ThreadStartParams` directly with
`approvals_reviewer=ApprovalsReviewer.user` to force every approval request
to the human path. `ThreadStartParams` (`generated/v2_all.py:9898-9930`) has
this as a plain optional field, every field on it optional, so this is a
straightforward direct construction, not a raw untyped `config={...}` dict
hack as originally guessed.

### Finding B (the load-bearing one): the public `Codex`/`AsyncCodex` API has no way to set `approval_handler` at all

`approval_handler` exists as a constructor parameter on exactly one class:
the low-level, fully synchronous `CodexClient`
(`openai_codex/client.py:214-223`):

```python
class CodexClient:
    """Synchronous typed JSON-RPC client for `codex app-server` over stdio."""
    def __init__(self, config: CodexConfig | None = None,
                 approval_handler: ApprovalHandler | None = None) -> None:
        self.config = config or CodexConfig()
        self._approval_handler = approval_handler or self._default_approval_handler
```

Neither the public synchronous wrapper `Codex.__init__` nor the async
wrapper `AsyncCodex.__init__` nor `AsyncCodexClient.__init__` accept or
forward it:

```python
# api.py:85-92
class Codex:
    def __init__(self, config: CodexConfig | None = None) -> None:
        self._client = CodexClient(config=config)   # <- no approval_handler
        ...

# api.py:312-316
class AsyncCodex:
    def __init__(self, config: CodexConfig | None = None) -> None:
        self._client = AsyncCodexClient(config=config)   # <- same
        ...

# async_client.py:57-62
class AsyncCodexClient:
    """Async wrapper around CodexClient using thread offloading."""
    def __init__(self, config: CodexConfig | None = None) -> None:
        self._sync = CodexClient(config=config)   # <- same
```

And `CodexConfig` (`client.py:196-211`) itself has no `approval_handler`
field - it is launch/identity config only (`codex_bin`,
`launch_args_override`, `config_overrides`, `cwd`, `env`, `client_name`,
etc.), confirming this cannot be threaded through indirectly either.

If no handler is supplied, `CodexClient._default_approval_handler`
(`client.py:833-836`) is used, and it **auto-accepts every command
execution request**:

```python
def _default_approval_handler(self, method, params) -> JsonObject:
    """Accept approval requests when the caller did not provide a handler."""
    if method == "item/commandExecution/requestApproval":
        return {"decision": "accept"}
```

Consequence: using the public `Codex`/`AsyncCodex`/`AsyncCodexClient` API as
documented in the package's own module docstring example (`with Codex() as
codex: thread = codex.thread_start(...); thread.run(...)`) means every
write-class tool call is **silently auto-approved with no way to intercept
it** - not a partial escalation-only gap (Finding A's concern), a total
bypass. This is a correctness-and-safety-critical finding for a project
whose whole approval-broker design exists to put a human between the model
and destructive actions.

## Resolution

`CodexSession` must not use `Codex`/`AsyncCodex`/`AsyncCodexClient`. It must
construct the low-level `CodexClient` directly, passing `approval_handler`
explicitly:

```python
from openai_codex.client import CodexClient, CodexConfig
from openai_codex.generated.v2_all import (
    ApprovalsReviewer, AskForApproval, AskForApprovalValue, ThreadStartParams,
)

client = CodexClient(
    config=CodexConfig(cwd=cwd),
    approval_handler=self._approval_handler,  # the thread-bridged callback
)
client.start()
client.initialize()
thread = client.thread_start(ThreadStartParams(
    cwd=cwd,
    approval_policy=AskForApproval(root=AskForApprovalValue.on_request),
    approvals_reviewer=ApprovalsReviewer.user,   # Finding A's fix
    sandbox=...,
    model=self._model,
))
```

Everything downstream (`turn_start`, `TurnHandle.stream()`/notification
iteration, `thread_resume`, etc.) is `CodexClient`'s own synchronous,
blocking API - there is no async variant with a working approval path.
`CodexSession`, which lives in mesh's asyncio world, must run every
`CodexClient` call off the event loop, not only the approval callback as
`phase3_codex-session.md`'s original approach sketch assumed. The natural
shape: a dedicated single-thread `concurrent.futures.ThreadPoolExecutor` (or
a plain background `threading.Thread` owning `CodexClient` outright) that
owns the `CodexClient` instance; `CodexSession`'s async methods
(`submit_turn`, `interrupt`, etc.) post work to it via
`loop.run_in_executor(executor, ...)` and results/notifications are marshaled
back via `loop.call_soon_threadsafe`. The approval callback itself (already
documented as running on `CodexClient`'s own internal reader thread,
`client.py:855-861`) bridges to `PermissionBroker.ask()` via
`asyncio.run_coroutine_threadsafe(...).result()`, exactly as originally
planned - that part of the design was already correct, it just isn't the
*only* place a thread bridge is needed now.

This is a materially bigger implementation surface than the ticket
originally scoped (the whole client, not just the approval hook, needs
thread-bridging), but it does not change the phase's goal, tests, or exit
criteria - only the internal shape of `CodexSession`.

## Status

Not yet verified against a live `codex app-server` process (no dedicated
Codex account available in this environment; the workstation's `~/.codex`
belongs to the harness executing this session and was deliberately not used
for live testing - see `planning/00_index.md`'s note, or the phase 3 ticket's
Open Questions). The source-level finding above is unambiguous regardless:
`approval_handler` genuinely does not exist as a parameter anywhere in the
call chain from `AsyncCodex`, so no live test is needed to confirm *that*
part; only Finding A (does `approvals_reviewer=ApprovalsReviewer.user`
actually route every request when set explicitly, as opposed to
`ApprovalMode.auto_review`'s indirect path) still benefits from a live
confirmation before the acceptance criteria's manual integration pass.
