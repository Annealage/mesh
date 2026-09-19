# Multi-backend agent SDK research

Date: 2026-09-19
HEAD: 58f78db34e

Research behind the multi-backend roadmap (`planning/roadmap.md`): replacing
mesh's single-provider `claude-agent-sdk` dependency with three independently
selectable backends (Claude, Codex, arbitrary local/OpenAI-compatible), each
keeping mesh's own chat pane, approval cards and viewer UI.

## Requirement

- Claude: unchanged, stays on `claude-agent-sdk` (already TOS-compliant,
  subscription OAuth via `CLAUDE_CODE_OAUTH_TOKEN`/`claude setup-token`).
- Codex/GPT: same TOS-adherence bar as Claude, so it must be a genuine
  first-party OAuth path (ChatGPT-subscription billing), not an API-key-only
  integration and not an unofficial credential-file reuse. Supported directly
  by OpenAI's own SDK, not routed through a third harness.
- Local/arbitrary OpenAI-compatible endpoint: point at any `base_url`,
  independent of the above two.
- No LiteLLM: explicit user rejection (OAuth stability issues, one more tool
  to configure).

## Ruled out

- **LangSmith**: observability/eval platform, not an agent runtime. Cannot
  replace `claude-agent-sdk`.
- **Generic Codex-app-server routing for local models**: the app-server is
  hard-locked to `wire_api="responses"`; most local inference servers
  (vLLM, llama.cpp server, LM Studio) speak Chat Completions `tool_calls`
  instead. This constraint is unchanged by using OpenAI's own `openai-codex`
  Python SDK, since that SDK spawns the same app-server underneath - it does
  not add a local-model story of its own.
- **Vercel AI SDK**: only supports Anthropic/OpenAI via plain API keys (or
  Vercel Gateway/OIDC). No native Claude Pro/Max or ChatGPT-plan OAuth. The
  only path found was a third-party plugin reading
  `~/.claude/.credentials.json` directly - fragile, unofficial, and
  Anthropic has already once blocked then narrowly reinstated third-party
  OAuth-token reuse (scoped specifically to their own Agent SDK).
- **Reusing `~/.codex/auth.json` outside the CLI**: technically possible
  (one third-party tool, "OneCLI", reportedly does it) but thin evidence,
  unofficial, real ToS/revocation risk. Superseded once the official
  `openai-codex` Python SDK's own `login_chatgpt()` was found (see below).
- **OpenHands Software Agent SDK**: MIT, Python-native, real bash/file-edit
  tools, Docker sandboxing - but model-agnostic via LiteLLM, so API-key-only
  billing for GPT, no ChatGPT-subscription OAuth. Ruled out once LiteLLM was
  explicitly rejected and the official Codex SDK's OAuth was confirmed.
- **omp (Oh My Pi) for Codex specifically**: omp has its own sanctioned
  `openai-codex` provider (OAuth via `OPENAI_CODEX_OAUTH_TOKEN`, callback
  port 1455 matching Codex's own PKCE flow) and would have worked, but the
  user's explicit direction was to support Codex directly through OpenAI's
  own tooling rather than through a third harness, for the same
  TOS-adherence reasoning that keeps Claude on `claude-agent-sdk` directly.
  omp remains the answer for the local/arbitrary-endpoint backend, where
  there is no vendor SDK to be direct *with*.

## Codex: `openai-codex` Python SDK

Source: `https://learn.chatgpt.com/docs/codex-sdk`,
`https://github.com/openai/codex/tree/main/sdk/python`.

- `pip install openai-codex`. Apache License 2.0 (confirmed via
  `https://raw.githubusercontent.com/openai/codex/main/LICENSE`) - no
  copyleft, safe to embed in a commercial product.
- Python >=3.10. Ships a pinned `openai-codex-cli-bin` runtime dependency -
  no separate `codex` CLI install needed on the host. It is a thin wrapper
  around the local `codex app-server` JSON-RPC process
  (`https://learn.chatgpt.com/docs/app-server`), but as OpenAI's own client
  library managing that process, not mesh hand-rolling JSON-RPC.
- Public entry points: `Codex` (sync) / `AsyncCodex` (async parity,
  `async_client.py`'s `AsyncCodexClient` wraps the sync `CodexClient` via
  `asyncio.to_thread`).

### Auth

- `codex.login_chatgpt() -> ChatgptLoginHandle` (`.auth_url`, `.wait()`,
  `.cancel()`) - real browser OAuth PKCE flow.
- `codex.login_chatgpt_device_code() -> DeviceCodeLoginHandle`
  (`.verification_url`, `.user_code`, `.wait()`) - device-code flow for
  headless hosts.
- `codex.login_api_key(api_key)` - fallback, pay-per-token instead of the
  subscription.
- Existing `codex login` CLI sessions are reused automatically if present.
- `codex.account(refresh_token=False)`, `codex.logout()`.

### Sandbox

`Sandbox.read_only` / `Sandbox.workspace_write` / `Sandbox.full_access`,
settable on `thread_start`/`thread_resume`/`thread.run`/`thread.turn`. Same
role as mesh's own `SANDBOX_SETTINGS` for Claude, but provider-owned.

### Approval hook (undocumented in the curated guide, confirmed by reading
`client.py`/`async_client.py`/`_approval_mode.py` source directly - this is
the load-bearing finding of this research pass)

`ApprovalHandler = Callable[[str, JsonObject | None], JsonObject]`, passed
as `CodexClient(config, approval_handler=...)`. The reader thread's
`_reader_loop` dispatches every server-to-client JSON-RPC *request*
synchronously to this callback and writes its return value back as the
response (`client.py:_handle_server_request`). Two methods fire it:

- `item/commandExecution/requestApproval` (Bash-equivalent)
- `item/fileChange/requestApproval` (Edit/Write-equivalent)

Decision vocabulary, from the app-server protocol doc
(`https://learn.chatgpt.com/docs/app-server`, "Command execution decisions"/
"File change decisions" section):

- Command execution: `accept`, `acceptForSession`, `decline`, `cancel`, or
  `{"acceptWithExecpolicyAmendment": {...}}`.
- File change: `accept`, `acceptForSession`, `decline`, `cancel`.

`acceptForSession` is the app-server-session analogue of mesh's
`allow_always` grant. Default handler (`_default_approval_handler`)
auto-accepts both when the caller supplies no handler - mesh must always
supply one.

**Open question (Q1 in the roadmap):** the curated public `ApprovalMode`
enum (`_approval_mode.py`) only has two values:

```python
class ApprovalMode(str, Enum):
    deny_all = "deny_all"
    auto_review = "auto_review"
```

`auto_review` maps to `(AskForApproval.on_request, ApprovalsReviewer.auto_review)`.
`ApprovalsReviewer` itself (from `generated/v2_all.py`) has three values:
`user`, `auto_review`, `guardian_subagent`. The docstring on `ApprovalMode`
says "High-level approval behavior for **escalated** permission requests",
which reads as: the app-server's own `auto_review` reviewer decides most
requests itself and only escalates the ones it cannot decide to the client's
`approval_handler`. That would mean the curated `ApprovalMode.auto_review`
does **not** guarantee every write-class tool call reaches the human, unlike
Claude's `can_use_tool` (called for every non-pre-allowed tool). To get that
guarantee, `thread_start`/`thread_resume`'s raw `config={...}` passthrough
needs to force `ApprovalsReviewer.user` explicitly, bypassing the curated
enum. `config` is documented as a public passthrough parameter on
`thread_start`, but the exact key name/shape to set the reviewer to `user`
was not directly observed - it needs confirming against a real running
app-server before CodexSession is built. See `planning/tickets/phase3_codex-session.md`.

### Model listing and per-turn model selection

`codex.models(include_hidden=False) -> ModelListResponse` - real enumeration
API, usable to populate a webui model picker. `thread.run(...)` and
`thread.turn(...)` both accept `model=` and `effort=` as per-call overrides
(`effort` uses the same `low/medium/high/xhigh/max` vocabulary mesh already
validates in `settings.py`) - no reconnect needed to switch model mid-thread,
unlike Claude.

### Tool exposure - the real architectural wrinkle

Claude's `create_sdk_mcp_server`/`@tool` (used by mesh's `MeshTools`) is
in-process, function-level registration. The Codex app-server only attaches
*external* MCP servers (stdio or HTTP) - it has no in-process host-callback
tool mechanism. `ExternalMessage` (the other tool-shaped primitive in the
Python SDK) is explicitly documented as carrying **no** user authorization -
it is for untrusted notifications, not a tool-call replacement.

**Open question (Q2 in the roadmap):** exact config schema for pointing a
per-thread `config={...}` override at a remote MCP-over-HTTP server (key
names for `mcp_servers`/`experimental_use_rmcp_client` or similar) was not
directly verified in this pass - the app-server docs note the older
`codex mcp-server`/`codex-mcp-server` binary was removed, so the current
schema needs checking against the pinned runtime version, not assumed from
memory. See `planning/tickets/phase3_codex-tool-mcp-bridge.md`.

## omp / `omp-rpc` (local/arbitrary backend)

Source: `omp://rpc.md`, `omp://providers.md`, `omp://sdk.md`,
`omp://auth-broker-gateway.md`, web search on `can1357/oh-my-pi` license.

- License: MIT (`can1357/oh-my-pi`, the actively maintained fork this host
  runs, 14.7k stars, canonical over the abandoned original). No copyleft -
  confirmed clear for embedding in a commercial product.
- `omp_rpc.RpcClient` (Python package bundled with omp) drives
  `omp --mode rpc` as a subprocess over a documented newline-delimited JSON
  protocol; handles request correlation, v2 chunked framing, message
  pagination, extension UI, and host-owned tools/URI schemes internally.
- Local model support: built-in keyless discovery for `ollama`/`llama.cpp`/
  `lm-studio` (each with an env-var `_BASE_URL` override), plus fully custom
  `models.yml` providers (`baseUrl` + `api: openai-completions` +
  `apiKey: none` or a real key) pointing at any OpenAI-compatible endpoint.
  This is genuinely provider/endpoint-agnostic, unlike the Codex app-server.
- Live model switching, mid-session, no reconnect:
  `{type: "set_model", provider, modelId}`. `get_available_models` for
  listing.
- Tool exposure: `set_host_tools` (`{name, label, description, parameters}`)
  registers host-owned tools; the RPC server calls back with `host_tool_call`
  (`toolCallId`, `toolName`, `arguments`) and expects `host_tool_result`
  (`{content: [...]}`, `isError?` for a tool failure). In-process callback
  over the same stdio transport - no separate MCP server needed, unlike
  Codex.
- Permission translation: `extension_ui_request{method:"confirm", title,
  message, timeout}` / `extension_ui_response{confirmed: boolean}`. Coarser
  than Claude/Codex - one boolean, no native "remember for session" verb, so
  mesh's own remembered-grant set has to intercept before forwarding to the
  human rather than delegating that to omp.
- `codex`-equivalent OAuth exists too (`openai-codex` provider,
  `OPENAI_CODEX_OAUTH_TOKEN`, callback port 1455) but is not used here per
  the "support Codex directly" decision above; noted only because it
  confirms omp's OAuth integrations are first-party, not scraped.

## Settled architecture

Three `AgentSession` implementations behind the unchanged
`session/base.py` Protocol seam, switched by a new `backend` setting:

| Backend | Driver | Package | License | OAuth | Tool exposure | Live model switch |
|---|---|---|---|---|---|---|
| `claude` | `SdkSession` (existing) | `claude-agent-sdk` | - | `CLAUDE_CODE_OAUTH_TOKEN` | in-process MCP | reconnect required (unverified, Q3) |
| `codex` | `CodexSession` (new) | `openai-codex` | Apache-2.0 | `login_chatgpt()` | MCP-over-HTTP bridge (Q2) | native per-turn `model=` |
| `local` | `OmpSession` (new) | `omp_rpc` (bundled with omp, MIT) | MIT | n/a (`auth: none` or a configured key) | native host-tool callback | native `set_model` |

Full phase breakdown, work items, tickets and open-questions table: see
`planning/roadmap.md`.
