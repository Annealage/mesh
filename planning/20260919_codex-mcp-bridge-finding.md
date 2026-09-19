# Design note: Codex has no remote/HTTP MCP server registration - stdio only

Date: 2026-09-19
HEAD: 3ac8734
Verified against: `openai-codex` 0.154.0's generated JSON-RPC protocol
(`openai_codex/generated/v2_all.py`, `openai_codex/client.py`), installed and
read directly in the same scratch venv as
`20260919_codex-approval-handler-finding.md`. Not verified against a live
`codex app-server` process. This closes Q2 in `roadmap.md` and corrects
`planning/tickets/phase3_codex-tool-mcp-bridge.md`'s premise.

## What Q2 originally asked

What is the current config schema for pointing Codex at a remote
MCP-over-HTTP server, given `codex mcp-server`/`codex-mcp-server` was
removed?

## What source-reading found

There is no MCP server *registration*/launch-config type anywhere in the
generated protocol (`v2_all.py`'s 39 `Mcp*` symbols are all runtime
interactions - status, tool-call requests/responses, OAuth login,
event-stream notifications - never a "here is a server to add" schema). The
only evidence of how a server gets registered at all is the real,
already-installed `~/.codex/config.toml` on this workstation (a different
Codex installation, not touched for testing, only read for its shape):

```toml
[mcp_servers.claude-net]
command = "/home/corona/.claude-net/bin/claude-net-plugin-linux-x64"

[mcp_servers.claude-net.env]
CLAUDE_NET_HUB = "https://telie.story-kettle.ts.net:4815"

[mcp_servers.pod]
command = "/home/corona/.local/bin/pod-mcp"
```

Command-launched (stdio subprocess), not URL-based. No `url`/HTTP-transport
field is evidenced anywhere in the config shape or the generated protocol
types. Codex's MCP support, at least as of this version, appears to be
**stdio-launched subprocess only** - the "MCP-over-HTTP" premise the ticket
was written against does not have a corresponding config surface to target.

## How to register a server without touching the user's real `~/.codex/config.toml`

`CodexConfig.config_overrides: tuple[str, ...]` (`client.py:205`) is not a
JSON-RPC concept at all - it is a list of `--config key=value` CLI flags
passed to the launched `codex app-server` process, confirmed by reading the
exact launch-argument construction (`client.py:246-256`):

```python
args = [str(codex_bin)]
for kv in self.config.config_overrides:
    args.extend(["--config", kv])
args.extend(["app-server", "--listen", "stdio://"])
```

This is Codex's standard TOML-dotted-path config-override mechanism (the
same one `codex -c key=value` exposes on the CLI). `CodexSession` can
therefore register a server scoped to its own launched process via e.g.
`config_overrides=('mcp_servers.mesh.command="/path/to/bridge"',
'mcp_servers.mesh.args=["--port", "12345"]')` without ever writing to the
user's real config file.

## Corrected design for the tool-exposure bridge

The original ticket's core decision - tools execute against mesh's one live
process via its own `tool_table()` accessor, exposed over HTTP with mesh's
own run-token auth, not duplicated per backend - still stands and is still
the right shape for the in-process/authority side. What changes is the last
hop: Codex cannot register that HTTP endpoint directly, since it only
launches stdio subprocesses. The fix is one more hop, not a different
design: mesh ships a small stdio-to-HTTP proxy script (a genuine MCP client
speaking stdio to Codex and a genuine MCP client speaking streamable-HTTP to
mesh's own mounted endpoint, translating one to the other) and registers
*that* via `config_overrides`, not the HTTP endpoint directly. This is the
same pattern several MCP ecosystem tools already use for exposing a remote
server to a stdio-only client (`mcp-remote` and similar), not a novel
invention.

```
codex app-server (per-thread subprocess)
  │  stdio, MCP protocol
  ▼
annealage_mesh's stdio-bridge subprocess (launched via config_overrides)
  │  streamable-HTTP, MCP protocol, mesh's own run token
  ▼
mesh's main process: /mcp endpoint (mounted on the existing HTTP server,
  per the original ticket) -> tool_table()'s wrapped handlers
```

Net effect on the ticket: add one new deliverable (the stdio-bridge script,
likely a `python -m annealage_mesh.session.codex_mcp_stdio_bridge` entry
point Codex launches with `codex_bin`-equivalent semantics, or reuse an
existing off-the-shelf stdio<->HTTP MCP proxy if one is MIT/Apache-licensed
and dependency-light enough to vendor - check before writing one from
scratch) and one new work item (constructing `config_overrides` in
`CodexSession.start()`'s `CodexConfig`). The HTTP-mount work (auth reuse,
`tool_table()` accessor, double-approval-count risk) is unchanged.

## Status

Not yet verified against a live `codex app-server` - no dedicated Codex
account/live process was used for this research (see
`20260919_codex-approval-handler-finding.md`'s note on why `~/.codex` was
deliberately not used as a live test rig). The absence of any
registration/launch-config type in the generated protocol is unambiguous
from source alone; whether `config_overrides` accepts a *table* value
(`mcp_servers.mesh.args=[...]`, a JSON-array-shaped string) the same way TOML
would, or only scalar `key=value` pairs, needs a live check before the
`config_overrides` construction in the approach sketch above is implemented
literally.
