"""The embedded-agent layer, kept apart from Mesh's 3D domain so it can become
the shared ``annealage-agent`` package (plan: ``planning/agent-package.md``).

Nothing here imports ``annealage_mesh`` outside this subpackage or knows about
models, CAD or the viewer (``tests/test_agent_boundary.py`` checks both). What
it needs from the product it runs in comes through ``product.py``.

Layout:

    product.py     the Product contract: identity, tool builder, settings keys,
                   events and inbound frames; install()/current()
    app.py         generic app assembly (routes, CSP, security headers, event
                   publisher, tool server, session) and serve()
    launch.py      the backend switch: one broker, one session per run
    tools.py       ToolSpec, READ/VIEW/WRITE Grading, ok/fail, the pause gate,
                   failure mapping, ToolServer (SDK server, pre-allowed list,
                   tool_table)
    files.py       path safety, guarded fixed-file reads/appends, atomic
                   replace, image sniffing, images/ and review/ files
    settings.py    generic settings keys, layering, product key registration
    sessions.py    <state dir>/sessions and state.json
    lock.py        <state dir>/lock (pid and port, never a token)
    protocol.py    /ws frames, generic and product inbound frame specs
    viewers.py     ViewerRegistry and ViewerBus
    net.py         bind modes, tokens, Origin/Host allowlists, banner
    backends.py    which agent backends are installed, and choosing one
    diagnostics.py what doctor and GET /settings report
    session/       AgentSession, generic events, the Claude, Codex, omp and
                   fake sessions, permissions, workspace trust, secret paths,
                   turn images, the event log, the Codex stdio MCP bridge
    http/          shared route helpers, /ws, chat (/upload, export),
                   /settings and /mcp routes
"""
