# session/omp.py: OmpSession (local/arbitrary-endpoint driver)

Phase: 4
Depends on: Phase 1 (`Decision` type), Phase 2 (`backend`, `local_base_url`,
`local_api_key` settings), Phase 3's `tools/registry.py` `tool_table()`
accessor
Written: 2026-09-19 at HEAD 58f78db34e
Revalidated: 2026-09-19 at HEAD 94e9dbf - Q4 DECIDED (roadmap.md): read omp://providers.md's "Custom providers in models.yml" section directly. Schema confirmed: providers.<id>.{baseUrl, api: "openai-completions", apiKey (env-var-name-or-literal; omit + auth: none for keyless), models: [{id, name, contextWindow, maxTokens}]}. No drift on this ticket's own file anchors (tools/registry.py's tool_table() accessor, added by Phase 3, not yet re-verified against Phase 3's landed shape - do that at this phase's actual entry, not now).

## Context

`omp_rpc.RpcClient` (MIT, bundled with the `omp` install, confirmed clear for
commercial embedding) is the local/arbitrary-OpenAI-compatible-endpoint
backend. Unlike Codex, its host-tool and permission sub-protocols are both
in-process callbacks over the same stdio transport it already uses - no
second server/transport to build, which is why this phase is sequenced after
Phase 3 rather than in parallel with it (it reuses Phase 3's `tool_table()`
accessor with a much simpler adapter). Full research trail:
`planning/20260919_multi-backend-research.md`'s "omp / omp-rpc (local/
arbitrary backend)" section, and `omp://rpc.md` directly for the wire
protocol.

## Scope

In scope: `session/omp.py`'s `OmpSession`; custom-provider config generation
from `local_base_url`/`local_api_key`; host-tool adapter; permission
adapter including the broker-side remembered-grant workaround (RPC's
`confirm` has no native "remember" verb); `diagnostics.py` wiring.

Out of scope: anything Codex-specific; the live `set_model` webui feature
(`phase5_live-model-selection.md` - this ticket's driver only needs to accept
a model at construction, live switching is a separate ticket even though
omp's RPC protocol supports it natively, to keep this ticket's surface
matched to Phase 4's stated goal).

## Files and anchors

- `session/base.py:313-395` - `AgentSession` Protocol, same contract as
  `CodexSession`.
- `session/sdk.py:168-263` (and full file) - structural reference, same as
  noted in `phase3_codex-session.md`.
- `tools/registry.py`'s `tool_table()` (added in Phase 3) - reused here with
  a `set_host_tools`/`host_tool_call`/`host_tool_result` adapter instead of
  Phase 3's MCP-HTTP bridge.
- `settings.py` (post-Phase-2) - `local_base_url`, `local_api_key` keys.
- `omp://providers.md` - the custom-provider (`models.yml`-shaped) schema
  this ticket must read in full before generating provider config from
  `local_base_url`/`local_api_key` - not fully read in this research pass
  (Q4).
- `cli.py:724-773` (post-Phase-2) - the `backend == "local"` branch replaces
  its Phase-2 stub.
- `diagnostics.py:78-183` - add `_omp_info` alongside `_codex_cli_info`
  (Phase 3), gated on `backend == "local"`; checks omp's own version and
  `local_base_url` reachability (a simple connect/HTTP-HEAD-style check, not
  a full model call).

## Design constraints

- `omp_rpc.RpcClient`'s host-tool and permission sub-protocols run over its
  own stdio transport with the subprocess `RpcClient` owns - unlike Codex's
  synchronous reader-thread callback, confirm (read the `omp-rpc` Python
  package's actual client code, not just the protocol doc) whether its
  event-delivery model is already asyncio-native or needs the same
  thread-bridging pattern `CodexSession` uses. The protocol doc describes the
  wire shape, not the Python client's own concurrency model - do not assume
  parity with Codex's threading without checking.
- RPC's `extension_ui_response{confirmed: boolean}` has no `"remember for
  session"` value, unlike Codex's `acceptForSession`. `OmpSession` must
  therefore consult `PermissionBroker`'s own granted-tools state (already
  present, reused unchanged from Phase 1) *before* forwarding a `confirm`
  request to the human at all, rather than delegating "was this tool already
  granted" to the RPC layer the way `CodexSession` partially can via
  `acceptForSession`. Get this ordering right: check broker state first,
  only construct/forward the `extension_ui_request` handling if the broker
  itself decides a human decision is needed.
- `local_api_key` may be absent (`None`) - the common case for a genuinely
  local, unauthenticated endpoint (`ollama`, `llama.cpp` server). Generated
  provider config must produce omp's own `auth: none`-equivalent in that
  case, not send an empty-string `Authorization` header.

## Approach sketch

```python
class OmpSession:
    def __init__(self, on_event, *, cwd, session_id, broker=None,
                 model=None, base_url=None, api_key=None, ...):
        ...

    async def start(self) -> None:
        provider_config = _build_custom_provider(self._base_url, self._api_key)
        self._client = RpcClient(provider=..., model=self._model,
                                  command=[...])  # own the child command per
                                  # the omp-rpc README's `command=` override,
                                  # rather than relying on a global omp config
                                  # file this run may not want to touch
        self._client.set_host_tools(_host_tool_defs(self._tool_table))
        ...
```

Provider config generation (`_build_custom_provider`) now has a confirmed
shape (Q4, `roadmap.md`) to build against - a dict matching
`omp://providers.md`'s custom-provider schema, written to wherever
`RpcClient`'s `provider=`/config-file argument expects it (confirm the
exact injection point - a `models.yml`-shaped dict passed directly, vs. a
temp file path - against `omp-rpc`'s actual client code, not assumed):

```python
def _build_custom_provider(base_url: str, api_key: str | None) -> dict:
    provider = {"baseUrl": base_url, "api": "openai-completions"}
    if api_key:
        provider["apiKey"] = api_key  # literal string; resolved as
        # env-var-name-or-literal by omp, so this is correct even in the
        # edge case where the string happens to match a real env var name
    else:
        provider["auth"] = "none"
    return provider
```

## Acceptance criteria and tests

- Fake stdio-transport harness drives `OmpSession` through canned RPC
  frames: `set_host_tools` -> `host_tool_call` -> `host_tool_result`
  round-trip for a READ-class tool, and a `confirm` extension-UI round-trip
  for a WRITE-class one, plus a case where the tool is already
  broker-granted and no `extension_ui_request` should be sent at all.
- Manual integration pass against a real local endpoint (an Ollama or
  llama.cpp instance is sufficient) completing an actual write-class turn
  end to end.
- `local_api_key = None` produces a provider config that does not send an
  empty/malformed auth header.

## Workflow shape

Implementation on sonnet, automated tests on haiku, standard + adversarial
review on opus - adversarial pass specifically targets the remembered-grant
workaround (confirm the broker's own granted-tools check runs before any
`extension_ui_request` is sent, not after, and that it cannot be raced by a
second concurrent tool call). Loop until clean.

## Open questions

- Whether the confirmed Q4 schema is passed to `RpcClient` as an inline
  dict, a generated `models.yml` fragment written to a temp file, or
  another mechanism - `omp-rpc`'s actual client code (not the protocol doc)
  needs reading to confirm the injection point.
- Whether `omp_rpc.RpcClient`'s Python-side event delivery is already
  asyncio-compatible or needs the same thread-bridge pattern as
  `CodexSession`'s approval handler - check the package's own source, the
  protocol doc alone does not answer this.
