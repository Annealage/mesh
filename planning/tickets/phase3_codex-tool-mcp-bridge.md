# CodexSession tool exposure: MCP-over-HTTP bridge on mesh's own server

Phase: 3
Depends on: `phase3_codex-session.md` (core driver must exist to attach
tools to); Phase 1's `tools/registry.py` accessor work item below
Written: 2026-09-19 at HEAD 58f78db34e
Revalidated: 2026-09-19 at HEAD 2a9fce9 - no direct drift on this ticket's own anchors, but phase3_codex-session.md's design changed substantially (CodexClient direct, not AsyncCodex - see 20260919_codex-approval-handler-finding.md). This ticket's "config={...} override" references now mean ThreadStartParams(config=...) constructed directly alongside approvals_reviewer=ApprovalsReviewer.user, not a kwarg on the public Codex.thread_start() wrapper. Q2 (MCP schema) itself remains unresolved.

## Context

Claude's `create_sdk_mcp_server`/`@tool` is in-process, function-level tool
registration - `tools/registry.py`'s `MeshTools.mcp_servers` returns that
object directly today. The Codex app-server has no equivalent: it only
attaches *external* MCP servers (stdio or HTTP transports). This ticket is
split out from the core `CodexSession` driver specifically because its
config schema (Q2) was not verified in this research pass, and the docs
noted the older `codex mcp-server`/`codex-mcp-server` binary was removed -
i.e. this surface has changed shape before and needs a fresh check, not an
assumption carried over from memory.

Full research trail: `planning/20260919_multi-backend-research.md`'s "Tool
exposure - the real architectural wrinkle" subsection.

## Scope

In scope: a new `tools/registry.py` accessor giving any driver the same
handler set Claude's `.mcp_servers` exposes, in a transport-neutral shape; an
MCP server (streamable-HTTP transport) mounted on mesh's own existing
microdot app; wiring `CodexSession` to point at it via `thread_start`'s
`config={...}` override.

Out of scope: the `OmpSession` host-tool adapter (`phase4_omp-session.md`) -
it consumes the same new `tools/registry.py` accessor but through a
different, in-process transport that needs no HTTP server at all.

## Files and anchors

- `tools/registry.py:209-243` - `MeshTools` class. Currently exposes
  `.mcp_servers` (a `{MESH_SERVER_NAME: self.server}` dict, per the elided
  return at line 243) built from Claude's `create_sdk_mcp_server`. Add a
  second accessor - e.g. `tool_table()` - returning `{name: (json_schema,
  async_handler)}` off the *same* already-`_wrap`-gated handler set (`_wrap`,
  `tools/registry.py:146-206`, applies the pause gate and failure mapping;
  do not duplicate that logic for the new accessor, derive it from the same
  wrapped handlers `.mcp_servers` is built from).
- `tools/__init__.py:27-53` - `MESH_SERVER_NAME`, `namespaced()`, `ok()`,
  `fail()` - the shared result-shape helpers every handler already returns
  through. The MCP-HTTP bridge's tool results must produce the same
  `{"content": [...], "is_error"?}` shape these already build, so
  `chat.js`'s tool-card rendering (`shortToolName`, `formatToolInput`,
  `static/js/chat.js:145-187`) needs no backend-specific branch.
- `app.py` (35.3 KB, not read in this research pass) - where mesh's microdot
  app assembles its routes (`http/routes_viewer.py`, `routes_chat.py`,
  `routes_settings.py`, `ws.py` are all registered somewhere in here or in
  `http/__init__.py`). Read this file before mounting a new route - find the
  existing registration pattern and match it, including how the token/origin
  checks (`http/ws.py:74-102,182-250`, `_token_is_allowed`/
  `_origin_is_allowed`) are shared across routes, so the new MCP endpoint
  reuses the same auth rather than inventing a second scheme.
- `session/codex.py` (from `phase3_codex-session.md`, now landed - read it
  before starting) - `start()`'s `CodexConfig(config_overrides=(...))`
  gains an entry registering the stdio-proxy subprocess this ticket adds,
  now that Q2 is resolved (`20260919_codex-mcp-bridge-finding.md`):
  `config_overrides` is a tuple of `--config key=value` CLI passthrough
  strings, not an inline JSON/dict value.

## Design constraints

- The MCP endpoint must require mesh's own run token, the same way `/ws` and
  `/settings` do - it is a tool-execution surface reachable from wherever
  Codex's app-server process runs, which for a `local_base_url`-style remote
  setup could be a different host than mesh's own; do not expose it
  unauthenticated even on `127.0.0.1`, since the app-server subprocess is not
  necessarily co-located with a human who already passed the browser's own
  token check.
- Reuse `tool_table()`'s wrapped handlers verbatim - the MCP bridge is a
  transport adapter, not a second place tool classification (READ/VIEW/WRITE,
  pause-gating) gets decided. If a future tool needs different behavior on
  Codex than on Claude, that is a `tools/registry.py` change, not something
  encoded in this bridge.
- Mount the HTTP/authority side on mesh's *own* existing HTTP server (no
  second long-lived process, no second port to manage or document in the
  startup banner) - this is a deliberate choice over spawning a standalone
  MCP server process, made explicit here so a future agent does not
  "simplify" it into a separate process without re-deriving why that was
  rejected (port/lifecycle/auth duplication). The stdio-proxy subprocess
  (new, per `20260919_codex-mcp-bridge-finding.md`) is unavoidable - Codex
  only launches stdio subprocesses for MCP, never a direct URL registration
  - but it should be a thin, short-lived translation shim with no tool logic
  of its own, not a second authority.

## Approach sketch

Two pieces, per `20260919_codex-mcp-bridge-finding.md`'s corrected design:

1. **HTTP/authority side** (unchanged from the original plan): the official
   `mcp` Python package's `Server`/`FastMCP` streamable-HTTP app, built from
   `tool_table()`'s entries, mounted at e.g. `/mcp` on mesh's microdot app
   (exact mounting mechanism depends on what `app.py`'s framework
   integration looks like - microdot and the reference MCP SDK's ASGI-style
   app may need an adapter; check before assuming a drop-in mount). Requires
   mesh's own run token the same way `/ws`/`/settings` do.
2. **stdio-to-HTTP proxy** (new): a small script Codex launches as a
   subprocess per thread, speaking MCP over stdio to Codex on one side and
   MCP over streamable-HTTP to mesh's own `/mcp` endpoint on the other,
   forwarding mesh's run token. Check first whether an MIT/Apache-licensed,
   dependency-light off-the-shelf proxy already does this (the `mcp-remote`
   pattern is common in the MCP ecosystem) before writing one from scratch -
   vendoring or depending on an existing, maintained proxy is preferable to
   a bespoke reimplementation of MCP's own stdio framing.

`CodexSession.start()` registers the proxy via `CodexConfig`'s
`config_overrides` (a tuple of `--config key=value` strings, confirmed by
reading `client.py:246-256`'s exact launch-argument construction - this is
Codex's standard TOML-dotted-path override mechanism, not a JSON/dict
value):

```python
config_overrides = (
    f'mcp_servers.mesh.command="{proxy_command}"',
    # whether a table-shaped value (args=[...], env={...}) is accepted the
    # same way TOML would, or only scalar key=value pairs, needs a live
    # check - see this ticket's Open questions.
)
```

## Acceptance criteria and tests

- A fake or real `codex app-server` process, launched with the
  `config_overrides` registration, can list mesh's tools (a
  `read_stl_view`/similar READ-class tool is a safe first target) through
  the stdio proxy -> HTTP endpoint chain and get a result in the same
  `{"content": [...]}` shape Claude's in-process tools return.
- A WRITE-class tool call routed through this bridge reaches
  `PermissionBroker` exactly once, not zero or two times (a plausible
  transport-layer bug: MCP's own protocol-level confirmation semantics
  double-counting against the approval-handler's).
- The HTTP endpoint refuses a request with no/wrong token the same way
  `/ws` does (`refusal()`, `http/ws.py:74-76`).
- The stdio proxy subprocess exits cleanly when its parent `codex
  app-server` process exits (no orphaned proxy processes accumulating
  across turns/sessions).

## Workflow shape

Implementation on sonnet, automated tests on haiku (fake app-server driving
the stdio proxy, and a fake/real HTTP client driving the `/mcp` endpoint
directly), standard + adversarial review on opus - adversarial pass
specifically checks the auth-reuse constraint (no unauthenticated tool
execution surface reachable through either hop) and the double-approval
risk above. Loop until clean.

## Open questions

- Whether `config_overrides` accepts a table-shaped value
  (`mcp_servers.mesh.args=[...]`, `mcp_servers.mesh.env={...}`) the same way
  TOML would, or only flat scalar `key=value` pairs - needs a live check
  against a real `codex app-server` process before the `config_overrides`
  construction in the approach sketch is implemented literally.
- Whether an existing MIT/Apache-licensed stdio-to-HTTP MCP proxy is
  suitable to depend on/vendor, or whether mesh needs to write its own -
  not researched in this pass.
- Whether microdot supports mounting a streamable-HTTP ASGI-style app
  directly, or needs a thin adapter route that proxies to an in-process MCP
  server object - not determined in this research pass since `app.py` was
  not read.

