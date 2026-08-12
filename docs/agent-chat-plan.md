# Annealage Mesh: in-viewer agent chat, architecture and build plan

Status: plan, not yet implemented. Branch `agent-chat`.

This document is the contract for the re-architecture. It is written to be sufficient for an implementer who has this file and the repo and nothing else. Where a claim about `claude-agent-sdk` matters, it is marked VERIFIED and the verification is stated, because several of these facts are counter-intuitive and one of them contradicts the SDK's own docstrings.

## 1. Goal

Today Mesh is a static-file server plus two JSON side-channels. A human clicks an STL in a browser to drop pin-comments; a separately-running agent reads and writes files on disk to exchange located feedback.

The target: `annealage-mesh` run in a folder scaffolds a project, launches the UI, and behaves like Claude Code in that folder, except the chat interface is a second pane beside the 3D viewer. Mesh operations are exposed to the model as tools so it can drive the viewer directly. The chat accepts image uploads and offers a sketch overlay on the 3D view. Transcript, images and models live in the project folder, which uses git by default when git is installed.

## 2. Verified SDK facts that constrain the design

All verified against `claude-agent-sdk` 0.2.135 with `claude` CLI 2.1.227, by source reading plus live probes. Do not re-derive these; do not trust the SDK docstrings over them.

1. **The model-visible name of an in-process MCP tool is `mcp__<server>__<tool>`.** VERIFIED by probe: a tool registered as `get_model_facts` on server `mesh` arrived as `ToolUseBlock(name='mcp__mesh__get_model_facts')`. The `create_sdk_mcp_server` docstring example showing `allowed_tools=["add", "multiply"]` is misleading. Every allow-list entry, deny-list entry and hook matcher must use the namespaced form or it silently never matches.

2. **Listing a tool in `allowed_tools` bypasses `can_use_tool` entirely.** VERIFIED by probe, which emitted `CanUseToolShadowedWarning: can_use_tool will not be invoked for: Read. An allowed_tools entry that allows a whole tool auto-approves it before the callback is consulted.` So `allowed_tools` is a list of decisions to never ask the human. Allow rules in settings files shadow the callback too and are invisible to the SDK, which is one reason to control `setting_sources` deliberately.

3. **A permission callback may block on a browser round-trip without deadlocking.** VERIFIED by source: `_internal/query.py:273` is the single reader loop; on a `control_request` (`:297`) it calls `_spawn_control_request_handler` (`:262`) which does `spawn_task(self._handle_control_request(...))`, and `can_use_tool` is awaited inside that spawned task (`:453`). The same applies to in-process MCP tool invocations, which arrive as control requests on the same path. This is what makes both human-in-the-loop approval and browser-round-trip tools possible at all.

4. **The SDK's own correlation pattern is worth mirroring.** `_internal/query.py:131` keeps `pending_control_responses: dict[str, anyio.Event]`; `_send_control_request` (`:546`) registers an event by request id and awaits it, cleans up in `finally` (`:581`, `:589`), and sweeps all pending events on shutdown (`:375`). The browser command channel should be shaped the same way.

5. **Outbound user turns are written verbatim.** `client.py:216-224`: given an `AsyncIterable[dict]`, `query()` writes each dict to the transport as one JSON line, injecting only `session_id`. Nothing is validated or transformed, so any content-block shape the CLI accepts can be sent.

6. **Inline base64 image blocks reach the model.** VERIFIED by probe: a user turn whose content was `[{"type":"image","source":{"type":"base64","media_type":"image/png","data":...}}, {"type":"text",...}]` produced a correct description of the image. Two caveats learned by failure: put the image block *before* the text that refers to it, and do not bury an image in a turn that also asks for unrelated tool work, or the model ignores the attachment and goes hunting the filesystem.

7. **Unknown inbound content blocks are silently dropped.** `_internal/message_parser.py:95-113` and `:150-170` match on `block["type"]` with cases for `text`, `thinking`, `tool_use`, `tool_result` and no default arm. An image block in an echoed user message is discarded with no error. Consequence: delivery is not observable, and the chat pane must render sent images from its own local record, never from the echo.

8. **A tool result may return an image.** `__init__.py:469-484` accepts an `image` item in a handler's returned `content` and builds `mcp.types.ImageContent`; `_internal/query.py:697-702` serialises it back with `type`, `data`, `mimeType`. So `return {"content": [{"type": "image", "data": b64, "mimeType": "image/png"}]}` is valid and a viewport capture can go to the model as a real image with no file write and no second round-trip.

9. **The client is loop-affine.** `client.py:59-65`: an instance cannot be used across async runtime contexts and holds a persistent anyio task group from `connect()` to `disconnect()`. `_internal/_task_compat.py` uses plain `loop.create_task` under asyncio, so hosting the client inside our own `asyncio.run()` is supported and needs no anyio dependency of our own.

10. **The wheel bundles a `claude` binary and prefers it over PATH.** `_internal/transport/subprocess_cli.py:247` `_find_cli()` calls `_find_bundled_cli()` first and returns immediately on success, only then trying `shutil.which("claude")` and a candidate path list. `_bundled/claude` is 304,282,632 bytes. Wheel tag is `py3-none-manylinux_2_17_x86_64`, so wheels are platform-specific. PyPI artifacts for 0.2.135: macOS arm64 82.3 MB, macOS x86_64 87.5 MB, manylinux aarch64 92.3 MB, manylinux x86_64 93.4 MB, win_amd64 92.4 MB, sdist 0.3 MB. `options.cli_path` overrides discovery entirely.

11. **`Transport` is a six-method ABC and is injectable.** `connect`, `close`, `write`, `read_messages`, `end_input`, `is_ready`, and `ClaudeSDKClient(options=..., transport=...)` accepts an instance. A fake drives the whole client with no subprocess, no binary and no network. It carries a warning that it is an internal API exposed for custom transports, which is why the dependency gets an upper pin.

12. **Token-level streaming requires `include_partial_messages=True`**, after which `StreamEvent` carries the deltas. Without it only whole assistant messages arrive, which is not acceptable for a chat pane.

13. **`setting_sources` defaults to `None`, which loads no settings files at all**, so a project `CLAUDE.md` is not read unless `"project"` is listed.

Observed cost: roughly $0.02 to $0.06 per trivial turn on `claude-haiku-4-5`. Per-turn cost is user-visible and belongs in the UI.

### 2a. Further facts verified against 0.2.136, which M5 depends on

The pin `>=0.2.135,<0.3` admits 0.2.136, and that is what is installed now. Every fact above was re-checked against it mechanically and all of them hold, including fact 7: `"image"` still appears nowhere in `_internal/message_parser.py`, so an image block in an echoed user message is still dropped. `ClaudeAgentOptions` has grown from 44 fields to 45. Four further facts, all VERIFIED by live probe, constrain M5:

14. **`can_use_tool` must return a `PermissionResultAllow` or `PermissionResultDeny`, never a plain dict.** A returned dict fails the call with `Tool permission callback must return PermissionResult (PermissionResultAllow or PermissionResultDeny), got <class 'dict'>`, and the model is told the permission infrastructure errored. Signatures: `PermissionResultAllow(behavior="allow", updated_input=None, updated_permissions=None)` and `PermissionResultDeny(behavior="deny", message="", interrupt=False)`. Many published examples show the dict form; it does not work here.

15. **A deny's `message` reaches the model verbatim.** The probe's `"denied by the probe's broker"` came back in the model's own summary of what happened. So the broker's refusal text is model-visible and should be written to tell the model what to do instead, not just to log the refusal.

16. **`sandbox={"enabled": True}` needs both `bwrap` and `socat` on Linux, and silently degrades without them.** With `socat` absent the CLI writes to stderr: `Sandbox disabled: sandbox is enabled but dependencies are missing: socat not installed ... Commands will run WITHOUT sandboxing. Network and filesystem restrictions will NOT be enforced.` Since the chosen posture is the sandboxed one, the startup banner and `doctor` must report whether the sandbox is **active**, not merely requested, which means `session/sdk.py` has to parse that line out of the captured stderr.

17. **`autoAllowBashIfSandboxed` is inert when the sandbox is inactive.** VERIFIED by running the same task with the flag true and false while the sandbox was degraded: both consulted the broker for the write and neither wrote anything. So the degraded mode fails safe, toward more prompting rather than toward unsandboxed auto-approval. That is what makes the sandboxed posture a safe default even on a machine missing the dependencies.

18. **With the sandbox active, the flag means bash is never prompted for, and the containment is real.** VERIFIED once `socat` was installed, against a broker that denied everything: the broker was consulted **zero** times, a write inside the working directory succeeded, and writes to `$HOME` and to `/tmp` both failed with `Read-only file system`. So "bash runs contained and unprompted" is literally what happens, and the same probe with the flag off consulted the broker once and wrote nothing.

19. **The sandbox restricts writes and network, not reads.** In the same probe, `cat ~/.bashrc` succeeded, unprompted. This follows from `SandboxSettings`' own documentation, which says filesystem read restriction is configured with Read **deny rules** rather than by the sandbox, and it is the honest limit of the containment: a sandboxed bash can still read anything the user can, including `~/.ssh` and cloud credentials. That matters more here than in a general-purpose agent, because this tool reads STL comments and filenames, which are untrusted input, while holding a shell. Adding deny rules for the obvious secret locations is the mitigation; it is deliberately not done yet, because a deny list broad enough to help is also broad enough to break legitimate work, and choosing it is a product decision rather than an implementation detail. Recorded in section 6 as deferred, not overlooked.

20. **A requested sandbox cannot be confirmed active from the child's output alone.** The CLI announces a sandbox it could not engage and says nothing about one it could, and that announcement arrives when a bash command first runs, not at connect time. So a startup banner claiming ACTIVE purely because nothing has complained yet is claiming something it does not know, which is how the first implementation of this got it wrong on a host with `socat` missing. What is checkable at startup is the negative case, by looking for the dependencies the CLI's own message names, and the child's report supersedes that whenever it arrives.

## 3. Architecture

### 3.1 Process topology

One `annealage-mesh` process running one asyncio event loop, created by `asyncio.run()` from `cli.main()`. On that loop: the HTTP and WebSocket server, the `ClaudeSDKClient`, every in-process MCP tool handler, the `can_use_tool` broker, the event log writer, and the callouts file watcher. The only other process we own is the `claude` child the SDK spawns itself. Total at runtime: two processes plus the browser.

One loop is chosen because of fact 3: tool handlers and permission callbacks already run as concurrent tasks off the SDK's read loop, so a handler that awaits a browser reply is one `asyncio.Future` resolved by the WebSocket receive task. No locks, no `run_coroutine_threadsafe`, no second cancellation model, no thread-safety audit of the pin store.

Rejected: keeping `ThreadingHTTPServer` and bridging to a dedicated SDK loop thread. It is the smaller diff and the larger design, because every new object straddles the boundary (the pending-command table is written by a tool coroutine and resolved by a handler thread; the subscriber set is written by threads and read by the loop; `interrupt()` needs `run_coroutine_threadsafe`), which means two concurrency models and a permanent lock discipline to save about 200 lines.

Rejected: an agent sidecar process. The genuinely risky child, the `claude` CLI, is already isolated by the SDK, and a sidecar adds a second hand-written IPC protocol beneath the browser protocol we must write anyway. The `AgentSession` protocol in 3.6 is the seam to slide one behind later if isolation is ever needed.

Blocking file IO no longer gets a free thread. A 40 MB STL read on the loop stalls token streaming, so static bodies are read through `loop.run_in_executor` and git and scaffold work uses `asyncio.create_subprocess_exec`. This is a correctness trap the threading server hid and it will regress silently if a later change adds a plain `read_bytes()`.

`stl.py` is squarely in this category and it is measured, not hypothetical: parsing 1M triangles takes about 1.8 seconds of blocking CPU. Any `mcp__mesh__model_info` handler must therefore hand it to `run_in_executor` rather than awaiting it inline, or a single `model_info` call on a large mesh freezes token streaming, every other in-flight tool, and the WebSocket writer for the better part of two seconds. Wire this from the first tool handler, not as a later optimisation.

### 3.2 HTTP layer: microdot

`microdot>=2.6,<3` as a base dependency. Pure Python, zero transitive dependencies, and it supplies the three things the standard library does not: an asyncio HTTP/1.1 server designed to be awaited inside an existing loop, a server-side WebSocket implementation with frame encode and decode, and an in-process `TestClient` that dispatches both HTTP requests and WebSocket sessions without opening a socket. It is also already the HTTP layer in Annealage Canvas, which keeps one framework in the product family.

What it buys, concretely: request parsing, keepalive, routing, the WebSocket handshake and framing, streaming responses, and socket-free tests. Hand-rolling that is 600 to 900 lines of protocol code with a long tail of bugs that present as "the chat pane froze once", and none of it is about STL review.

Rejected: starlette plus uvicorn, which is the strongest ecosystem answer but costs five or six base packages for a loopback tool and buys ASGI portability we never exercise. Rejected: aiohttp, a six-package compiled tree against microdot's zero. Rejected: hand-rolled asyncio HTTP plus SSE, which was a coherent position only while WebSocket framing was assumed to be ours to write.

Known wart to document in code: microdot's body limits are class attributes and therefore process-global (default 16 KiB). `app.py` raises `Request.max_content_length` for uploads and the upload route streams to disk via `req.stream` rather than buffering, so the raised limit is not a memory amplifier on other routes.

### 3.3 Browser transport: one WebSocket, versioned frames

A single WebSocket at `/ws` carries all bidirectional traffic. HTTP keeps the byte-shaped and history-shaped work: model files, image assets, uploads, transcript history pages, and the unchanged `/callouts` and `/submit` routes.

WebSocket rather than SSE plus POST because the load-bearing case is server-initiated requests that need replies. `get_camera`, `get_visibility`, `capture_view` and `pick_nearest_pin` are all round-trips into the page. SSE gives only the down-leg, so replies arrive as separate POSTs, which spreads correlation ids across two transports with two connection states and no ordering guarantee between a command and a racing reply. One socket gives one ordering guarantee, one liveness signal, one reconnect story and one place to enforce auth. What we give up is `EventSource`'s free reconnect, which is about 40 lines of backoff, and we need explicit sequence-based resync regardless.

Every frame is JSON carrying `{"v": 1, "type": ...}`. `PROTOCOL_VERSION` is a constant in `protocol.py` and the server closes with code 4400 on mismatch, which is how a stale cached page is separated from a newer server.

Server to browser:

```json
{"v":1,"type":"hello","seq":0,"session":{"id":"...","sdk_session_id":"...","cwd":"...","agent":"connecting|ready|unavailable"},"protocol":1}
{"v":1,"type":"event","seq":128,"event":{"kind":"text_delta","turn":7,"text":"..."}}
{"v":1,"type":"event","seq":129,"event":{"kind":"tool_use","turn":7,"tool_use_id":"tu_1","name":"mcp__mesh__set_view","input":{}}}
{"v":1,"type":"event","seq":130,"event":{"kind":"tool_result","tool_use_id":"tu_1","is_error":false,"text":"..."}}
{"v":1,"type":"event","seq":131,"event":{"kind":"permission_request","request_id":"pr_3","tool":"Bash","input":{},"suggestions":[]}}
{"v":1,"type":"event","seq":140,"event":{"kind":"turn_end","turn":7,"stop_reason":"end_turn","cost_usd":0.031}}
{"v":1,"type":"event","seq":141,"event":{"kind":"callouts_changed"}}
{"v":1,"type":"call","id":"c_12","method":"viewer.capture_view","params":{"width":1280,"include_sketch":false}}
{"v":1,"type":"ping","t":1750000000}
```

Browser to server:

```json
{"v":1,"type":"hello","token":"...","last_seq":128,"viewer":{"tab_id":"...","w":1600,"h":900}}
{"v":1,"type":"turn","blocks":[{"type":"text","text":"why is this wall thin?"},{"type":"image_path","path":"images/2026-08-12T101500-sketch.png"}]}
{"v":1,"type":"permission","request_id":"pr_3","decision":"allow|allow_always|deny","message":""}
{"v":1,"type":"result","id":"c_12","result":{"png":"data:image/png;base64,...","camera":{}}}
{"v":1,"type":"error","id":"c_12","error":{"code":"no_canvas","message":"..."}}
{"v":1,"type":"interrupt"}
{"v":1,"type":"state","state":{"camera":{},"visibility":{"lid":true},"selection":3,"mode":"annotate"}}
```

Correlation and timeouts, mirroring fact 4: `ViewerBus.call(method, params, timeout=10.0)` allocates an id, registers an `asyncio.Future` in a pending map, sends the `call` frame, and awaits with `asyncio.wait_for`. Cleanup happens in `finally`. When a connection closes, every pending future targeted at it is failed immediately rather than left to time out, so the model gets an actionable error instead of a ten-second stall. A late reply for an unknown id is logged and dropped. Domain errors come back as a `type:"error"` frame with a code, which the handler turns into `is_error: true` with the message; transport failures surface distinctly as timeout or disconnect text.

Two tabs: all connected viewers are broadcast subscribers of the same session, so both show the same conversation. For `call` frames exactly one viewer is primary, elected by most recent interaction, with a `viewer_primary` event broadcast on change so the other tab can show that it is not driving. Broadcast-and-take-first-reply is wrong because two tabs have different camera poses and `get_camera` would be a coin flip. With zero viewers connected, viewer tools fail fast with `is_error: true` and the text "no viewer connected; ask the human to open <url>", because there is no state to query and a silent hang teaches the model nothing.

Backpressure: per-connection outbound `asyncio.Queue(maxsize=256)` drained by one writer task. `include_partial_messages` makes token deltas the fastest producer, so the writer coalesces adjacent `text_delta` events for one turn and flushes at most every 30 ms. On overflow, drop `ping` first, then collapse pending deltas; never drop `call`, `permission_request` or `turn_end`, and if still saturated close with 1013 so the client reconnects and replays from `seq`. The policy lives in one place and is testable with a fake slow writer.

### 3.4 Event log, reload and resume

Every server-to-browser event carries a monotonic `seq`. `EventLog` is an append-only `events.jsonl` plus a bounded in-memory ring of 500 events. On reconnect the client sends `hello{last_seq}` and the server replays from the ring then goes live; anything older is paged over HTTP at `GET /session/<sid>/events?from=N`. Liveness on the socket, history as an HTTP resource, which makes "what did the agent say an hour ago" curl-able and cacheable rather than a replay feature of the socket.

The turn is owned by the session, not the socket. The SDK pump keeps consuming and appending regardless of whether any browser is attached, so browser reload mid-turn, browser closed and reopened, and process restart are all the same mechanism. Unanswered permission requests are live futures in the broker and are re-emitted on replay, so a reload mid-prompt re-shows the prompt.

**Restart starts a fresh session by default**, matching `claude` itself, with two explicit flags to do otherwise. A bare invocation silently reattaching to a conversation the user had forgotten is a class of surprise worth avoiding once explicit flags exist.

- **`-c` / `--continue` never takes an argument.** Mesh resolves the most recent session for this project itself, from `.mesh/state.json`, falling back to scanning `.mesh/sessions/` by mtime. The user never types or looks up an id. With no prior session it exits with a message naming the folder rather than quietly starting fresh.
- **`-r` / `--resume [SID]` is the only flag that accepts a session id.** `-r SID` resumes that session and errors if the id is unknown to this project. Bare `-r` lists sessions and exits 0 with id, start time, turn count, cost and a snippet of the first user turn, using the `list_sessions`, `get_session_info` and `project_key_for_directory` helpers. Bare `-r` never silently resumes anything, because that would duplicate `-c` and blur the two flags.
- `-c` and `-r` are mutually exclusive.

Both flags end up passing `ClaudeAgentOptions(resume=<id>)` with an id Mesh resolved, never `continue_conversation=True`. That option is implicitly scoped to the CLI's notion of cwd, our project root is not necessarily that, and a resolved id stays inspectable on disk. This is an internal detail and says nothing about what the user types. If resume fails, fall back to a new session and emit `session_reset` with the reason rather than failing startup.

Ctrl-C: `SIGINT` sets a shutdown event, then `await client.interrupt()`, wait up to 2 s for a terminal `ResultMessage`, fail in-flight viewer futures as tool errors, fsync the event log, close sockets with 1001, `await client.disconnect()` so the `claude` child is reaped, release the lock, exit 0. A second Ctrl-C inside that window hard-exits 130. Windows falls back from `loop.add_signal_handler` to `signal.signal` plus `call_soon_threadsafe`.

Second instance in the same folder is refused. `.mesh/lock` is created `O_CREAT|O_EXCL` holding pid, port and token. If the pid is live, print the running URL and exit 3; if dead, log and reclaim. Two SDK clients resuming one session id and two writers on one `events.jsonl` is corruption, so this is a hard failure. Viewer-only mode does not lock, preserving today's ability to run several viewers.

**Agent health never gates viewer health.** The HTTP server starts first and independently. A missing or unauthenticated CLI, or a dead child, becomes an `agent_error` event carrying captured stderr and remediation text plus a Retry button, while the viewer, pins and `/submit` keep working.

### 3.5 CLI surface and back-compat

The published MIT skill and the on-disk file contract survive unchanged. `mesh-comments.json`, `mesh-comments.log` and `mesh-callouts.json` stay at the project root with exactly the shapes `SKILL.md` documents, and new directories sit alongside them. No file gets two writers and the skill needs no edit beyond noting that a chat pane may also be present.

```
annealage-mesh [DIR]              agent mode: scaffold if needed, chat pane plus viewer. Default DIR is "."
annealage-mesh view [DIR]         viewer only, exactly today's behaviour, no SDK import, no scaffolding, no git
annealage-mesh init [DIR]         scaffold plus .gitignore plus git init, idempotent, no server
annealage-mesh doctor [DIR]       print python, claude CLI path and version, git, port, lock, extras; no server
```

Flags: `--port` (8765), `--host` (see the bind modes below), `--no-open`, `--version`, `--model`, `--permission-mode`, `-c` / `--continue`, `-r` / `--resume [SID]`, `--token`, `--no-git`, `--force`, `--settings` (print resolved settings with provenance and exit), and `--no-agent` as an alias for `view`.

Three bind modes, all first-class:

| `--host` | Binds | Use |
|---|---|---|
| `127.0.0.1` (default) | loopback only | local work |
| `tailscale` (alias) | the host's `tailscale0` IPv4, resolved at startup | remote review over the tailnet |
| explicit IP, or `0.0.0.0` | as given | anything else, including LAN |

`--host tailscale` is the recommended remote mode. It resolves the `tailscale0` interface address in the `100.64.0.0/10` range and binds only that, which matters because `0.0.0.0` binds every interface: on a typical laptop that includes the WiFi LAN and `docker0`, exposing the tool on whatever network the machine is attached to as a side effect of wanting tailnet access. Resolution failure, whether Tailscale is absent or down, is a clear startup error naming the interface, never a silent fall back to a wider bind.

Bare invocation is agent mode because that is the headline feature and the user's stated intent for running the tool in a folder. The compatibility risk is the skill's documented `annealage-mesh <dir> --no-open`, which would now scaffold and start an agent inside an existing agent session. Mitigation: when `CLAUDECODE` is set in the environment the default flips to viewer-only with a printed note, so a skill-driven invocation never spawns a nested agent, and `view` pins it explicitly. SKILL.md is updated in the same release to pass `view` explicitly rather than relying on detection.

The default changes from binding all interfaces to loopback, but non-loopback binding stays fully supported with no extra flag beyond `--host` itself, because remote review over Tailscale is a real workflow. What made the old default indefensible was not that it was reachable, it was that the server also served any file under the directory once past the traversal guard (`server.py:75-85`, `119-124`), which becomes an information-disclosure hole the moment the directory is a git-tracked project root with an agent in it. That fallback is deleted regardless of bind mode, and the default moves to loopback so that reaching the tool remotely is a decision rather than an accident. Release notes must call the default change out.

### 3.6 Module layout

```
src/annealage_mesh/
  __init__.py          5    version
  __main__.py          5    python -m annealage_mesh
  cli.py             230    argparse, subcommands, mode dispatch, lock, banner, signal wiring
  paths.py           130    ProjectPaths: layout resolution, legacy vs project, serve allowlist
  project.py         200    scaffold, .gitignore, git init and first commit, config
  app.py             190    build microdot app, supervise root tasks, ordered shutdown, wiring
  protocol.py        180    frame construction and validation, PROTOCOL_VERSION, error codes
  viewers.py         230    ViewerRegistry: connections, primary election, broadcast, call plus pending futures, backpressure
  stl.py             130    dependency-free binary and ascii STL header, bbox, triangle count
  http/
    routes_viewer.py 180    GET /, /manifest, /model/<rel>, /asset/<rel>, /callouts, POST /submit
    routes_chat.py   150    GET /session/<sid>, /session/<sid>/events, POST /upload, POST /interrupt
    ws.py            260    /ws: origin and token check, hello and replay, dispatch, writer task, ping
  session/
    base.py          130    AgentSession Protocol plus AgentEvent dataclasses. The fake-able seam
    sdk.py           330    ClaudeSDKClient impl: options build, message pump, interrupt, resume, stderr capture
    fake.py          150    scripted session for tests
    events.py        160    EventLog: seq, append-only jsonl, ring, replay, transcript.md render
    permissions.py   170    can_use_tool broker: futures, remembered rules, PermissionUpdate mapping
  tools/
    registry.py      160    @tool definitions, create_sdk_mcp_server
    viewer_tools.py  330    camera, visibility, selection, pins, measure, capture: all via ViewerBus.call
    review_tools.py  190    read comments, write callouts, snapshot
    model_tools.py   130    list_models, model_info
```

Roughly 3,150 Python lines from 304 today, plus about 1,200 of tests. That is a real maintenance commitment for one maintainer and the module boundaries are chosen so it can be reviewed in pieces.

`server.py` is deleted. Its manifest scan and submit writer move to `project.py`, its safe-path logic to `paths.py`.

Two protocols carry the testability of the whole design:

```python
class AgentSession(Protocol):
    state: Literal["idle", "connecting", "ready", "busy", "error"]
    async def start(self) -> None: ...
    async def send_turn(self, blocks: list[dict]) -> None: ...
    async def interrupt(self) -> None: ...
    def events(self) -> AsyncIterator[AgentEvent]: ...
    async def close(self) -> None: ...
```

and a `ViewerBus` protocol (`async def call(method, params, timeout) -> dict`, `def broadcast(event)`) that every tool handler depends on instead of the concrete registry. So HTTP and WebSocket tests run against `session/fake.py` with no `claude` binary and no 93 MB dependency, tool tests assert exact frames against a fake bus, and `session/sdk.py` has its own tests driving the real `ClaudeSDKClient` through a fake `Transport` (fact 11).

### 3.7 Front end

`viewer.html` becomes a shell of about 120 lines of markup plus `<link>` and `<script type="module">`. The front end splits into native ES modules with relative imports, which every target browser loads directly, so the no-build-step property is preserved with no bundler, no transpiler and no npm in CI. What is given up is "one file", which at roughly 2,900 lines of two-pane UI plus an event bus plus a command dispatcher is a liability: every agent-driven edit would rewrite a giant file and diffs would be unreviewable.

```
static/
  viewer.html      120   shell markup
  css/app.css      260   panes, mobile tabs, cards, chat
  js/main.js       120   bootstrap and wiring
  js/store.js      180   single mutable app state plus subscribers
  js/ws.js         180   connect, backoff, seq and replay, request and response dispatch
  js/commands.js   260   browser side of the viewer-control protocol: the method table
  js/three-scene.js 280  renderer, camera, lights, axes, ResizeObserver sizing
  js/models.js     180   manifest load, part list, visibility
  js/pins.js       300   user pins and agent callouts, markers, sprites, picking
  js/measure.js    160
  js/layout.js     140   pane split, mobile tab arbitration
  js/chat.js       320   transcript render, streaming deltas, uploads, permission cards
  js/sketch.js     160   overlay canvas, composite, upload
  js/settings.js   200   settings modal: sections, provenance display, restart-required labels
  js/vendor/       three.module.js, OrbitControls.js, STLLoader.js, VERSIONS.json
```

The settings modal opens from a topbar gear and has sections for Server, Agent, Viewer and Export, plus a read-only Diagnostics block showing the `claude` CLI path and version, git version, Python version, project root, current session id, bind address, reachable addresses, and whether the agent extra is installed. Diagnostics duplicates `annealage-mesh doctor` deliberately, so a remote user with no terminal can self-diagnose, which is the whole point when the tool is being used from a phone. Every field is labelled with when it takes effect: bind host and port are restart-required and the panel offers no live-rebind, because rebinding a live listener is complexity with a security-relevant failure mode and a restart is cheap.

**The narrow-viewport layout is required, not optional.** Remote review over Tailscale from a phone or tablet is a real workflow, so at or below 900 px the side and chat panels become tabs over the canvas rather than showing a "too narrow for chat" message.

`js/store.js` is the fix for two real defects the code audit found: per-part visibility is currently bound one-directionally (`viewer.html:250`), so a tool that hides a part cannot update the checkbox, and `agentList` would have two writers once callouts arrive by push as well as by file (`viewer.html:589-591`, `637-648`). Putting all scene-affecting state behind one writer with subscribers makes both the checkbox and the mesh `visible` flag readers, and removes the race by construction.

Layout: `#app` loses `position: fixed; inset: 0` and becomes a flex child, and the renderer is sized from its container box via `ResizeObserver` rather than `innerWidth`/`innerHeight`, because opening the chat pane changes the canvas box without firing a window resize (`viewer.html:11-12`, `185-186`, `318-321`). Wide viewports get three columns; at or below 900 px the side and chat panels become tabs over the canvas, extending the existing media-query precedent rather than arbitrating two independent overlay drawers.

three.js 0.160.0 is vendored and the jsdelivr importmap is dropped. The CDN is already a known single point of failure, which is why the four-second blank-screen watchdog exists, and it matters more once a model scripts a viewer with nobody watching. Cost is 1.27 MB in the wheel, a refresh script recording version and hash, and a `REUSE.toml` entry for three.js's MIT licence. The vendored files stay byte-for-byte upstream: the importmap resolves the addons' bare `three` specifier to the local module, so nothing is rewritten, the recorded hashes stay comparable against upstream, and the addons share one module instance rather than loading a second copy that would break every `instanceof` check inside them.

Static serving changes shape. The general "serve anything under the directory" fallback is deleted. Viewer assets are served from the package, never the project. Model files are served by `GET /model/<rel>` resolved through the manifest index, so only paths the scan actually listed are reachable, and images by `GET /asset/<rel>` restricted to `images/`. `GET /<name>.stl` stays as an alias into the manifest index for compatibility. Manifest scanning becomes recursive with dotdir, `.git` and `.mesh` exclusion and a file-count cap.

### 3.8 Project layout and git

```
<project>/
  models/                 STL and 3MF inputs and agent-generated output
  images/                 uploads, sketch composites, captured views, git-tracked
  mesh-comments.json      unchanged contract, project root
  mesh-comments.log       unchanged contract, project root
  mesh-callouts.json      unchanged contract, project root
  review/                 created lazily by the first transcript export, not scaffolded
  CLAUDE.md               generated stub, kept if present, never clobbered
  .gitignore              generated
  .mesh/config.toml       per-project overrides, committed
  .mesh/state.json        session ids, viewer prefs
  .mesh/permissions.toml  project-scoped allow-always grants
  .mesh/sessions/<sid>/events.jsonl   canonical event log, append-only, untracked
  .mesh/lock              pid, port, per-run token
```

Plus one file outside the project, at `platformdirs.user_config_dir("annealage-mesh")/settings.toml`, on Linux `~/.config/annealage-mesh/settings.toml`, holding person and machine preferences.

**No human-readable transcript is written unless asked for.** `.mesh/sessions/<sid>/events.jsonl` is the canonical log that scrollback, reconnect replay and resume all read, and it stays untracked because it churns per token delta. Nothing conversational is committed by default. Export is on demand through two entry points over one implementation in `session/events.py`: the `export_transcript` tool for the model, and a button in the chat pane header posting to `POST /session/<sid>/export` for the human. Default target is `review/transcript-<ISO8601>.md`, with `format` of `markdown` or `jsonl` and `include` of `text` or `full`. The tool is write-class so it prompts; the button is not, because a human initiating an export directly needs no approval.

`.gitignore` ignores `.mesh/` with a negation for `.mesh/config.toml`, which is shareable project configuration and holds no secret, since the per-run token lives in `.mesh/lock` and is regenerated every start. `models/` and `images/` stay tracked.

### Settings, three layers with visible provenance

Precedence, highest first: CLI flag, project `.mesh/config.toml`, user `settings.toml`, built-in default.

- **User settings** carry defaults for bind host, port, auto-open, model, effort, up-axis, upload size cap, sketch stroke defaults, chat pane width, and whether tool cards start collapsed.
- **Project config** carries model, permission mode, project name and default part visibility.

`GET /settings` returns each effective value together with the layer that supplied it, so the settings window can say "port 8765, from user settings" versus "from a command-line flag, not editable this run". Provenance is what makes a three-layer scheme comprehensible rather than mystifying, so it is part of the contract rather than a nicety.

**Never persisted, in either file.** `permission_mode: bypassPermissions` is accepted only as a single-run CLI flag with a printed warning, and is rejected with an explicit error if found in a config file; persisting "never ask me again" for an agent with shell access turns one careless moment into a standing vulnerability, and the settings window does not offer it. The per-run token is never written to config. Derived values such as a resolved Tailscale address are recomputed each run because they change.

Allow-always permission grants persist to `.mesh/permissions.toml`, project-scoped, never user-scoped, and never for `Bash`, so a broad grant made in a scratch folder cannot silently apply to real work.

Because `setting_sources` defaults to loading nothing (fact 13), the generated `CLAUDE.md` is only read if we pass `setting_sources=["user", "project", "local"]`, which we do, because "behaves like Claude Code run in that folder" requires it. The file is generated once if absent and never rewritten.

Git: `git init` runs when git is installed and the folder is not already a repo or inside one, with one scaffold commit. Nothing is auto-committed afterwards, because a tool that silently commits a human's working folder destroys their ability to stage their own work. An explicit write-class `mesh_snapshot(message)` tool lets the model propose a commit, which routes through the permission broker. If `user.email` is unset, skip the commit and say so rather than failing or inventing an identity. Binary bloat is left to the user: `.gitignore` does not exclude `models/` or `images/`, and the scaffold notes git-lfs as an option without configuring it.

### 3.9 Tool surface

Flat, about fourteen tools, declared with `@tool` and registered through `create_sdk_mcp_server(name="mesh", ...)`. Flat rather than the two-tier `search_actions`/`execute_action` shape Canvas uses, because the probe showed the CLI already surfaces tools through its own deferred `ToolSearch` mechanism, so a second discovery layer would duplicate it. Tool descriptions are therefore search targets and must be written to be findable. Revisit tiering only past roughly twenty tools.

Read-class, pre-allowed in `allowed_tools` so they never prompt: `mcp__mesh__list_models`, `model_info`, `get_view`, `get_visibility`, `list_comments`, `list_callouts`, `capture_view`, `measure`.

Write-class, deliberately absent from `allowed_tools` so they reach the broker and therefore the human: `set_view`, `fit_view`, `set_visibility`, `set_up_axis`, `add_callout`, `delete_callout`, `select_pin`, `snapshot`, `export_transcript`.

`export_transcript` is write-class because an exported transcript may then be committed and can carry whatever was typed about the hardware under review, plus absolute paths and per-turn costs. The human's own export button bypasses the broker entirely, which is the right asymmetry.

`capture_view` returns an image content block per fact 8, not a file path. Geometry facts come from `stl.py`, a small dependency-free binary and ASCII STL reader for header, bounding box and triangle count, rather than a mesh library, because that is all the model needs and it avoids a dependency. Watertightness is deliberately not offered, because computing it properly needs real topology work.

Not offered as tools, deliberately: anything that types into the composer on the human's behalf, anything that submits the human's pins, and continuous camera animation. The model may move the camera, which is visible in the 3D pane, but a `paused` flag the human can set from the topbar makes all write-class mesh tools return an error, so the human can edit a pin comment without the agent mutating underneath them. That idea is lifted from Canvas's read and write classification.

Ordinary Claude Code tools: `setting_sources=["user","project","local"]` and the `claude_code` tool preset, so Read, Edit and Bash are present, which is what "behaves like Claude Code in that folder" means. Nothing is added to `allowed_tools` for them.

**That does not mean every one of them prompts, and the difference is worth knowing before designing the pane around it.** VERIFIED by probe against SDK 0.2.136 with a broker that denied everything: `pwd`, `ls -1` and `echo hello` all ran without `can_use_tool` being consulted at all, while `cat /etc/hostname` and a `curl` to an external host were both consulted and refused. Claude Code classifies bash invocations itself, and one confined to the working directory with no side effects and no network never reaches our broker. So the permission pane shows writes, reads outside the directory, and network access; it will never show a plain directory listing. This is the CLI's own behaviour and is not configurable through the options this design sets.

### 3.10 Security

The change in kind is the point: an HTTP endpoint on a developer's machine can now make an agent edit files and run shell commands in their project.

Non-negotiable for the first release:

1. Default bind `127.0.0.1`, with `tailscale` and explicit non-loopback binds fully supported as deliberate choices. The startup banner prints the effective exposure **every run**, naming the bound address and the interfaces it is reachable on, because a persisted setting means the user is no longer typing a flag that would remind them.
2. A per-run token, `secrets.token_urlsafe(16)`, generated at startup, embedded in the URL we open, echoed in the `hello` frame, and required on `/ws`, `/upload`, `/settings` and `/submit` in agent mode. Viewer-only mode leaves `/submit` open so the published skill flow is unchanged. On any non-loopback bind the token stops being defence in depth and becomes the primary control.
3. An `Origin` allowlist on the WebSocket handshake, computed from the resolved bind rather than hardcoded to localhost, or remote access breaks. This is not optional and not redundant with the token: **WebSocket handshakes are not subject to the same-origin policy**, so without both checks any page the human visits could open `ws://127.0.0.1:8765/ws` and drive an agent with Bash access. A bug in the `ws.py` auth path is a remote-code-execution bug, not a UI bug, and it gets its own test file.
4. `Host` header validation, so DNS rebinding cannot turn an attacker domain into a localhost origin.
5. Deletion of the general static-file fallback, replaced by manifest-index-resolved model serving and `images/`-restricted asset serving. The current fallback serves any file under the directory once past the traversal guard, which becomes an information-disclosure hole the moment that directory is a git-tracked project root containing `.git` and source.
6. Upload filename sanitisation: generated names only, derived from a timestamp plus a slug, never the client-supplied name, which removes path separators, dotfiles and Windows reserved names as a class.
7. Model text is never inserted as HTML. The chat renderer escapes, and any markdown support is a small hand-written renderer over escaped text, not `innerHTML` of model output.

Deferred but recorded: a served `Content-Security-Policy`, which needs to be written against vendored assets rather than the CDN and is easier once vendoring lands; per-user authentication behind the token for the LAN case; and prompt-injection hardening for content the model reads out of STL comments and filenames, which is mitigated today only by every tool that acts being gated.

**There is no TLS, and what that costs depends on the bind.** On the tailnet it is acceptable, because WireGuard encrypts the transport and the token in the URL is protected in transit. On a plain LAN bind it is not: the token, the STL content and the conversation all travel in cleartext and are readable by anyone on that network. The banner says so explicitly for non-loopback, non-Tailscale binds. Documented as the recommended remote path, costing us no code: `tailscale serve https / http://127.0.0.1:8765` fronts a loopback-bound Mesh with TLS and tailnet device identity, which is strictly stronger than binding the tailnet address directly.

`PUT /settings` can change the bind address for the next run, so it is privilege-relevant. It validates and rejects unknown keys rather than merging blindly, refuses `bypassPermissions`, refuses any attempt to write a token, and gets its own tests alongside `tests/test_ws_auth.py`.

Nothing conversational is committed by default, so the transcript is no longer a standing disclosure risk. An exported transcript carries the warning at the point of export instead.

### 3.11 Tests and CI

The fake seam is two-layered on purpose. `session/fake.py` implements `AgentSession` and covers every HTTP, WebSocket, chat-pane and tool test with no SDK involvement at all. Separately, `tests/test_sdk_session.py` drives the real `ClaudeSDKClient` through a fake `Transport` (fact 11) that yields canned protocol dicts and records what `write()` receives, which is the only way to catch a break in the SDK's control protocol on an upgrade. Both are needed: the first keeps the suite fast and dependency-free, the second is the canary.

Layers: unit tests for `paths`, `project`, `protocol`, `stl`, settings and git helpers; protocol tests through microdot's in-process `TestClient`, including `TestClient.websocket()`, with no sockets; integration tests with a fake browser client speaking the real frame protocol against a fake session; and Playwright end-to-end gated by `pytest.importorskip`, limited to a handful of tests that assert what only a real browser can, namely that a tool call moves a real camera and that the three-pane layout resizes the canvas.

`tests/test_settings.py` covers the three-layer precedence, the provenance reported by `GET /settings`, rejection of unknown keys, refusal of `bypassPermissions` in either config file, and refusal of any attempt to write a token through `PUT /settings`.

Roles by model tier, for any agent-driven work on this plan: implementation and test authoring on sonnet, running the suite and building the wheel on haiku, standard and adversarial review on opus, with findings looped back to sonnet until reviews are clean and tests pass. A milestone is not done because the suite is green; it is done when the suite is green **and** review findings are cleared. Milestone 1 shipped with known majors precisely because its commit gate checked only the exit code. Playwright is justified precisely because the UI is now the primary surface, but it stays a small, skippable set.

Network isolation is enforced, not left to discipline: an autouse fixture points `cli_path` at a stub binary and monkeypatches the socket module for everything except the explicitly-marked `live` tests, which are deselected by default.

`asyncio` tests use `pytest-asyncio`. Timeouts are tested by injecting the timeout value, never by sleeping.

The pre-existing failure is fixed first: `tests/test_server.py::test_manifest_lists_stl` asserts exact dict equality while `/manifest` returns extra `dir` and `path` keys, and CI has been red since 2026-07-24. **The test is wrong, not the response.** `/manifest` is a documented contract consumed by the shipped viewer, `dir` and `path` are deliberate additions from commit `e43c065`, and removing them now would break the viewer's title rendering. The fix is a subset assertion on the keys the contract promises.

CI: matrix `3.10`, `3.12`, `3.13`, one job per version, each installing the base dependencies, which now include the SDK. The two-job split existed to keep the 90 MB download off the light leg; with the SDK a base dependency there is no light leg to protect, and a matrix leg that skipped it would be testing an install nobody performs. The 3.9 leg is deleted along with the classifier.

### 3.12 Packaging

```toml
requires-python = ">=3.10"
dependencies = [
    "microdot>=2.6,<3",
    "claude-agent-sdk>=0.2.136,<0.3",
    "platformdirs>=4,<5",
]

[project.optional-dependencies]
dev = ["pytest>=7", "pytest-asyncio>=0.23"]
```

**The SDK is a base dependency, not an extra**, decided by the maintainer on 2026-08-12: agent mode is the default mode and viewer-only is the exception, so the thing that makes the default mode work cannot be optional. The cost is the one the extra was invented to avoid and is accepted deliberately: the SDK's wheel bundles a roughly 300 MB `claude` binary, so installing this package downloads about 90 MB instead of tens of kilobytes. What that does **not** cost is portability of our own artifact: this package's wheel stays `py3-none-any`, and only the dependency resolves per platform, with upstream publishing wheels for macOS arm64 and x86_64, manylinux aarch64 and x86_64, and win_amd64. On a platform with no wheel, pip falls back to the SDK's 0.3 MB sdist, which carries no bundled binary, so `claude` then has to be on `PATH`; that is a real degradation and `doctor` should report which of the two is in use.

The floor moves from 0.2.135 to 0.2.136 because that is the version the permission-broker contract was verified against; see fact 14.

`platformdirs` 4.11.2 is MIT, pure Python with zero dependencies of its own, and its `requires-python >=3.10` matches our floor exactly. `appdirs` is the better-known name but its last release was 2020; `platformdirs` is its maintained successor with the same API shape.

The `<0.3` cap on the SDK is load-bearing because the design depends on `Transport`, documented as internal and subject to change.

Rejected, and worth recording because it was the plan's own position first: making agent mode an extra so that someone who only wants to look at an STL pays 40 KB rather than 90 MB. That reasoning is sound about the download and wrong about the product. It optimises for the viewer-only user, who is the exception, at the cost of making the default mode fail on a fresh install until a second, differently-spelled install command is found. A tool whose headline feature is behind an extra advertises that the feature is an afterthought.

`anyio`, `mcp` and `sniffio` are not declared; we never import them and they arrive under the extra as the SDK's own dependencies.

`README.md` and `SKILL.md` both advertised zero runtime dependencies, Python 3.9+ and near-instant `uvx` start. Those claims are rewritten rather than quietly dropped: two runtime dependencies, Python 3.10+, and a first install of roughly 90 MB because the SDK bundles the Claude Code CLI. The size is stated plainly where a reader decides whether to install, not buried in a changelog.

The single-file `force-include` of `viewer.html` in `pyproject.toml:50-51` is replaced by normal package-data inclusion of `src/annealage_mesh/static/**`. `REUSE.toml` gains an MIT entry for `static/js/vendor/*`, leaving the PolyForm-plus-MIT-skill arrangement intact.

## 4. Milestones

Each ends demonstrable and tested. No milestone exists only to refactor.

**M1. Green baseline and the STL reader.** Fix `test_manifest_lists_stl` to a subset assertion. Add `stl.py` with binary and ASCII parsing plus `tests/test_stl.py`. Deliverable: CI green for the first time since 2026-07-24, and a dependency-free geometry reader. Demo: `pytest -q` passes and `python -m annealage_mesh.stl model.stl` prints bbox and triangle count. Retires: the risk that every later failure lands on an already-red suite.

**M2. microdot port, behaviour identical.** Replace `server.py` with `app.py`, `paths.py`, `http/routes_viewer.py` on microdot, serving today's exact routes and file contract, plus recursive manifest scanning with exclusions, the manifest-index model route, and deletion of the static fallback. Port `tests/test_server.py` to `tests/test_routes_viewer.py` against microdot's `TestClient`. Deliverable: a release with no user-visible change except closing the file-disclosure hole. Demo: existing viewer works unchanged against the new server. Retires: the HTTP-layer choice, and the traversal exposure, before anything agent-shaped exists.

M2 landed with two scope changes worth carrying forward.

The model scan is **flat**, not recursive, and refuses symlinks and hardlinks. `viewer.html` keys its mesh table by `name` and fetches by `file`, so two models sharing either field collide in the browser, which makes recursion impossible without changing that file. A symlinked model cannot be validated safely either: checking where a link points means resolving a path, resolving is several lookups, and anything able to write to the served directory can change the entry between them. `rel` is already in the manifest, so **M3 owns introducing recursion together with a viewer that fetches by `rel`**, and should decide then whether symlinked models are worth supporting through an open-and-verify path rather than a resolve-and-trust one.

Routes **pin file identity**: the scan records each model's `(st_dev, st_ino)` and an open must land on that inode, retrying once after a fresh scan so a regenerated model is served rather than reported missing. Anything in M3 onward that serves bytes must go through `file_response` with an identity, or it reopens a hole that took several passes to close.

**M3. Front-end split with no behaviour change.** `viewer.html` to shell plus ES modules, `js/store.js` as the single writer for scene state, `ResizeObserver` sizing, the tabbed narrow-viewport layout at or below 900 px, vendored three.js, `REUSE.toml` entry. No chat pane yet. Deliverable: identical viewer, modular source, no CDN, usable on a phone. Demo: viewer works offline with the network disabled, and the narrow layout tabs correctly at 390 px. Retires: the layout and state-ownership risk, and the one-directional visibility binding, before the chat pane depends on them.

M3 landed with the following differences from the above.

**Two shared modules were added to the front-end list**, `js/ui.js` (the toast and the error banner) and `js/sprites.js` (the two canvas-texture sprite builders plus their disposal), because each is used by two feature modules and a shared helper beats either duplicating it or making one feature module import another. `js/sprites.js` also owns `disposeSprite`, since removing a sprite from a group frees neither its material nor its GPU texture, and agent callouts are torn down and rebuilt whenever the callout list changes.

**The manifest gains a `label` field**, unique across the listing, computed server-side as the shortest tail of `rel` that no other entry shares, falling back to the full `rel` and iterated **to a fixed point**. The fixed point is load-bearing rather than pedantry: a single dedup pass lets a substituted `rel` collide with another entry's already-chosen short label, which a real tree (`a/b.stl`, `a/b.STL`, `q/a/b.stl.stl`) produces, and two identical labels put two indistinguishable rows in the part list. The viewer fetches by `rel` and displays `label`.

**`GET /static/<path:rel>` serves the package's own tree through its own scanned index**, so the containment, symlink refusal and identity pinning models get apply to the viewer's own modules too, and a file sitting in that directory that the index rules do not match is unreachable. One deliberate asymmetry, argued at the check: the hard-link refusal does **not** apply to package files, because `uv` and pip install by hardlinking out of a local cache, so several links is normal there, and `site-packages` is not the untrusted input a served project directory is.

**Packaged assets carry a weak `ETag` and `Cache-Control: no-cache`; model bytes stay `no-store`.** The vendored three.js is 1.27 MB, not the 700 KB estimated in 3.7, and every page load wants it, which on the phone-over-Tailscale workflow this plan targets is the largest single cost of dropping the CDN. `no-cache` means the browser must revalidate, so a module edited during development is never served stale, while a reload costs one conditional request instead of the bundle. The validator is computed from a fresh `lstat` at request time rather than from the cached scan, and includes `st_dev` and `st_ino`, so a name relinked to another file cannot be affirmed as unchanged.

**`window.mesh` is a documented debug surface**, exposing `{store, scene, camera, controls, renderer, meshes}`. It is what the Playwright tests assert against and what a browser console drives, and M6 needs it.

**Known and deliberately not fixed in M3:** `fitView` frames a bounding sphere without accounting for the viewport's aspect ratio, so on a narrow viewport the model overflows horizontally. That behaviour is unchanged from the pre-split viewer, and M3's charter was no behaviour change, but it is a real usability problem on the phone layout this plan calls a required workflow, so it belongs to whichever milestone next touches the camera.

**M4. Event log and WebSocket, no agent.** `protocol.py`, `viewers.py`, `http/ws.py`, `session/events.py`, `session/fake.py`, plus the three bind modes with `tailscale` resolution, the always-printed exposure banner, the `Origin` allowlist computed from the resolved bind, `Host` validation and the token. The browser connects, receives `hello`, and the 1.5 s callouts poll is replaced by a server-side mtime watcher pushing `callouts_changed`. Tests: `tests/test_protocol.py`, `tests/test_ws_auth.py`, `tests/test_viewers.py`, `tests/test_events.py`, `tests/test_bind_modes.py`. Deliverable: live push with no agent, reachable over the tailnet. Demo: writing `mesh-callouts.json` by hand makes pins appear with no poll and no reload, from a phone over Tailscale. Retires: transport, auth, bind modes and replay, all testable without the SDK.

M4 landed with the following differences from the above, several of them forced by facts about microdot 2.6.2 that this plan assumed away.

**Auth refuses before the handshake, as an HTTP 403, and there is no close code 4403.** `microdot.websocket.WebSocket.close()` takes no arguments and sends an empty CLOSE frame, so a coded close has to be built by hand, and `websocket_upgrade` can be called partway through a route after the checks have run. Refusing pre-handshake is both stronger, since no connection object, queue or writer task is created for a caller that failed, and the only form of refusal that can be tested in process: **`TestClient.websocket()` silently discards every frame that is not TEXT or BINARY**, so a test asserting a close code through it asserts on a frame the harness never delivered and cannot fail. The one post-handshake coded close, 4400 for a version mismatch, is asserted by driving `dispatch_request` over a raw byte buffer and decoding the frame, plus in a real browser where `event.code` is visible.

**`Host` is checked on every route, and the check is exact set membership with no parsing.** The allowlist carries each name with and without the port, so nothing has to decide what a trailing dot, an embedded userinfo or an unbalanced IPv6 bracket means. A portless `Host` is safe to accept for the same reason an absent one is: a browser omits the port only when it is the scheme default. Note for anyone writing tests: microdot's `TestClient` defaults to `Host: example.com:1234`, which is exactly the DNS-rebinding shape this check exists to refuse, so a fixture has to send a `Host` naming the bind it built.

**A repeated `t` query parameter is refused rather than resolved.** `req.args` is a MultiDict returning the first value, so `?t=<real>&t=wrong` would otherwise authenticate. Harmless in itself, but it makes "which value counts" a property of one parser, and a proxy in front may pick the other.

**`max_message_length` is set explicitly.** Its default of `-1` means inbound frames inherit `Request.max_body_length`, which `app.py` raises to 8 MiB for pin submissions, so an unrelated route's upload allowance silently became the per-frame buffer ceiling on every socket.

**The greeting and the replay complete before the connection is registered.** Registering starts a writer task that also sends on that socket, and a replayed event carries an older seq than anything live; interleaved, a client would move its resync position backwards and replay what it had already rendered. A consequence worth knowing: a client that connects and never sends `hello` is never registered, so it receives nothing and holds no writer task, and replay from its `last_seq` covers the gap.

**Every viewer is pinged every 5 seconds, and the browser's liveness watchdog is 15 seconds.** The two numbers are a pair and neither works alone. Without pings, an idle but healthy connection delivers nothing and the watchdog closes it, which is observable in a browser as a socket that reopens every few seconds. Without the watchdog, a dead-but-open socket after a phone sleeps or a tailnet rekeys leaves the page reporting live and running neither the push nor the poll. A drop from a live socket must also clear the live state before the fallback timer is armed, or that timer's own guard reads a state the drop never cleared and the fallback never fires.

**Shutdown closes every viewer with 1001.** Closing a listening socket stops new connections and nothing more, and a WebSocket handler never returns on its own, so without this a shutdown leaves every viewer waiting out its full liveness timeout and makes the bounded shutdown drain spend its whole budget waiting on handlers that will not finish.

**The callouts watcher keys on a digest of the bytes read, not on size and modification time, and treats "does this parse as JSON" as the readiness test.** A stat signature holding still across two samples does not prove a write finished, since a same-size rewrite or a stall inside one `write` produces two identical samples of an incomplete file, and a stat signature can also miss a rewrite entirely when the filesystem's timestamp granularity is coarser than the gap between two writes. A file that never parses is announced once past a deferral bound, because a watcher waiting for quiet that never comes never fires at all. The first sample primes and announces nothing, since the page's own fetch on load already covers the state the watcher starts in.

**Measured, not assumed:** `callouts_changed` reaches a real socket in 24 ms against the 1500 ms poll it replaces, and a `0.0.0.0` bind's banner enumerates the WiFi LAN, the tailnet and `docker0`, which is the exposure that flag hides.

**Known and not fixed in M4:** emulating a browser as offline does not drop an established WebSocket. `navigator.onLine` flips and the `offline` event fires, but the socket stays open and keeps delivering pings, so the page correctly goes on reporting live. Any test of the drop path has to drop it for real, by stopping the server. Relatedly, `navigator.onLine` is deliberately **not** wired to close the socket: it reports link status rather than reachability, and closing a working socket on that signal would be a regression in robustness.

**M5. Agent session and the chat pane.** `session/base.py`, `session/sdk.py`, `session/permissions.py`, `js/chat.js`, the three-pane layout, streaming, interrupt, the lock file, the permission dialog, and session control: fresh by default with `-c` and `-r` as specified in 3.4. Tests: `tests/test_sdk_session.py` through a fake `Transport`, `tests/test_permissions.py` including the browser-gone and timeout paths, `tests/test_session_flags.py` covering `-c` with no prior session, `-r` with an unknown id, bare `-r` listing, and mutual exclusion. Deliverable: the headline feature. Demo: type in the browser, watch tokens stream, approve a `Bash` call from the pane, restart with `-c` and continue. Retires: the SDK integration and the human-in-the-loop flow.

**M6. Mesh tools and browser remote control.** `tools/registry.py`, `viewer_tools.py`, `review_tools.py`, `model_tools.py`, `js/commands.js`, the `paused` flag. Tests: `tests/test_tools.py` against a fake bus, plus the first Playwright test asserting a real camera move. Deliverable: the model operates the viewer. Demo: ask the model to hide a part and frame a pin, and watch it happen. Retires: the round-trip protocol under real tool dispatch.

**M7. Images and sketch overlay.** `POST /upload`, `images/`, composer paste, drag and drop and file picker, `js/sketch.js` with stroke capture and compositing, and the dual delivery of fact 6. Sketch ships image-only first, with stroke unprojection to 3D coordinates as a follow-on because it is the part most likely to need iteration. Tests: `tests/test_upload.py`, plus a Playwright sketch round-trip. Deliverable: point at the model by drawing on it. Demo: circle a wall, ask why it is thin, get an answer about that wall.

**M8. Project scaffolding, settings, git, docs.** `project.py`, `cli.py` subcommands, `doctor`, `CLAUDE.md` generation, `.gitignore`, `git init` plus scaffold commit, the settings window and `GET`/`PUT /settings` with `platformdirs` wiring and provenance, the `export_transcript` tool plus `POST /session/<sid>/export` plus the pane button, and the README, SKILL.md, RELEASING.md and COMMERCIAL.md updates including the honest dependency claim, the `--host` release note and a section on `tailscale serve`. Tests: `tests/test_project.py`, `tests/test_cli.py`, `tests/test_settings.py`, `tests/test_export.py`. Deliverable: `annealage-mesh` in an empty folder produces a working project, configurable from the browser. Demo: `mkdir demo && cd demo && annealage-mesh`, then change the model from the settings panel and export a transcript.

## 5. Decisions locked

Do not relitigate these without a stated reason.

- One process, one asyncio loop, hosting HTTP, WebSocket, the SDK client, tool handlers and the permission broker. No thread bridge, no sidecar.
- microdot as the HTTP and WebSocket layer, a base dependency.
- One WebSocket for all bidirectional traffic; HTTP for bytes and history.
- Monotonic `seq` on every event, append-only `events.jsonl` plus a 500-event ring, replay on reconnect, history over HTTP.
- Every `/ws` auth failure refuses **before** the handshake, as an HTTP 403, and the token, `Origin` and `Host` checks are three independent checks, each commented with what it defends against and why the other two do not cover it. `Host` applies to every route. Comparisons are exact set membership, never parsing.
- One primary viewer receives `call` frames; chat events broadcast; zero viewers means tools fail fast.
- Legacy JSON file contract stays at the project root, unchanged. The MIT skill keeps working.
- Bare `annealage-mesh` is agent mode; `view` is today's behaviour; `CLAUDECODE` flips the default to viewer-only.
- A bare invocation starts a fresh session. `-c` continues the most recent and never takes an argument; `-r` is the only flag accepting a session id, and bare `-r` lists and exits. Mutually exclusive.
- No human-readable transcript is written by default. Export is on demand, as a write-class tool for the model and an unprompted button for the human.
- Three bind modes: loopback default, `tailscale` resolving `tailscale0`, and explicit or `0.0.0.0`. Non-loopback is supported, not gated behind a scare-flag, and the exposure banner prints every run.
- Settings live in three layers, CLI over project over user over default, and `GET /settings` reports provenance per value.
- `bypassPermissions` is never persistable; the per-run token is never written to config; allow-always grants are project-scoped and never cover `Bash`.
- Narrow-viewport tabbed layout is required, because phone review over Tailscale is a real workflow.
- Default bind becomes `127.0.0.1`; token plus `Origin` plus `Host` checks; static fallback deleted.
- `viewer.html` splits into native ES modules, no build step; three.js vendored.
- `js/store.js` is the single writer for scene state, holding plain serialisable data only. No THREE object is ever a state value; whichever module created one keeps it in a side table and reconciles that table against store state in a subscriber. A mutator called from inside a subscriber is queued and drained by a flat loop, so no listener observes two changes out of the order they were made.
- **Symlinked models are refused, not supported.** Deciding where a link points means resolving a path, which is several lookups that anything able to write to the served directory can win, so a check applied to what a name pointed at a moment earlier proves nothing. A model outside the project tree should not be reachable, and one inside it needs no link. Hard links are refused for the same disclosure reason, with a deliberate exemption for the package's own installed files.
- **Multi-viewer is convenience, not collaboration**: several devices belonging to one person, not several people. No per-viewer identity appears in events or in pin authorship, because identity without authentication would be theatre and per-user auth is deferred. The forward seam is one nullable field: an event may carry the originating viewer's tab id, so a later collaborative mode has somewhere to put identity without a schema break.
- Flat tool list, read-class pre-allowed via `allowed_tools`, write-class always prompting.
- `capture_view` returns an image content block, not a path.
- **Agent mode is the default mode and viewer-only is the exception**, so the SDK is a base dependency rather than an extra, accepting a roughly 90 MB install. `requires-python >=3.10`; SDK pinned `>=0.2.136,<0.3`.
- Two fake layers: `AgentSession` fake for most tests, fake `Transport` as the SDK-upgrade canary.
- `/manifest` keeps `dir` and `path`; the test is fixed, not the response.
- `git init` plus one scaffold commit, and never an automatic commit after that.
- **The default agent posture is the full `claude_code` preset with bash sandboxed.** `sandbox={"enabled": True, "autoAllowBashIfSandboxed": True}`, so bash runs contained and is not prompted for, while Edit, Write and network access still reach the broker and therefore the human. Chosen over both alternatives (everything prompting; read-only until widened) because it is better than either on both axes at once: fewer approval cards for the human to clear, and filesystem plus network isolation that neither of the others has. Two consequences are not optional. The sandbox needs `bwrap` and `socat` present, so the banner reports whether it is **active** rather than merely requested (fact 16). And where it cannot engage, including Windows, the fallback is the everything-prompts posture, which fact 17 confirms happens automatically and safely; that fallback must be stated in the banner rather than being left for the human to infer from a sudden increase in prompts.

## 6. Deferred

- Stroke unprojection to 3D coordinates, after M7 ships image-only sketches.
- `Content-Security-Policy`, easier once assets are vendored.
- Per-user authentication for the LAN case, beyond the per-run token. `tailscale serve` covers the remote case better in the meantime.
- Read deny rules for secret locations (`~/.ssh`, cloud credential directories, keyrings). Fact 19: the sandbox contains writes and network but not reads, so a sandboxed bash can read anything its user can. Wanted, and left undone deliberately, because a list broad enough to help is broad enough to break real work and the choice is the maintainer's.
- `--fork`, mapping to `fork_session=True`, for branching a resumed session.
- An in-pane session picker, replacing the `-r` terminal listing. Wanted eventually, because a phone user has no terminal.
- Two-tier tool discovery, only if the tool count passes roughly twenty.
- Extracting a shared browser-control library across Annealage products. Canvas's ground truth was in-process LVGL state and it has no correlation table or pending-future map, so there is nothing to share yet. Revisit once Mesh's `viewers.py` and `commands.js` have proven themselves, and treat this plan's protocol as the candidate to generalise.
- JS unit tests, which would need a node toolchain. Front-end logic rides on Playwright until that trade changes.

## 7. Cut

- A hand-rolled asyncio HTTP and SSE server. microdot supplies framing and a socket-free test client, which was the whole argument for owning that code.
- SSE plus POST as the transport. It cannot carry server-initiated requests needing replies without splitting correlation across two transports.
- An agent sidecar process.
- `search_actions` and `execute_action` meta-tools, duplicating the CLI's own `ToolSearch`.
- Watertightness checking in `stl.py`.
- A `--allow-multiple` escape hatch for two instances in one folder.

## 8. Open questions for the maintainer

Five earlier questions are now answered and recorded in section 5: the transcript is export-only, non-loopback binding stays supported with `--host tailscale` recommended, the narrow-viewport layout is in scope, symlinked models are refused, and multi-viewer is convenience rather than collaboration.

Every question is now answered. The last one, how locked down the agent should be by default, was decided by the maintainer on 2026-08-12 in favour of an option the SDK gained after this plan was written, and is recorded in section 5.
