# Annealage Mesh: in-viewer agent chat, architecture and build plan

Status: M1 to M8 implemented, so the headline feature works, the model can operate the viewer, and either side can point at the model with a picture: a three-pane viewer, a streaming chat pane driving a real agent session in the served directory, human-in-the-loop approval, session resume, and seventeen mesh tools on an in-process MCP server, graded so that reading the view and driving it never prompt while the four that write to the project always do, with a server-side pause switch the human holds over everything that changes anything, and image attachments in the composer alongside a sketch overlay that draws on the current view. Project scaffolding, three-layer settings with visible provenance, git, transcript export and the settings window are in, so the tool now sets a folder up, is configurable from the browser, and can write out what was said.

Two things in this document are records rather than instructions, and are worth reading before implementing anything further: section 2b, on why the served directory's own Claude configuration is not trusted, and section 6, which lists what was deliberately deferred.

**Anything not built is in section 6**, with the reasoning for why it was left. That list is the backlog: this document is finished as a build plan, so section 6 is the only part of it that describes future work.

Where the implementation departed from the plan as written, the plan has been amended rather than left to disagree with the code. Section 2's fact 2 carries a correction of that kind: its guess that `setting_sources` contains a settings file's allow rules is wrong, and facts 21 to 27 give what was measured instead.

**One capability was added that this plan never scoped, because the product it describes does not work without it.** The plan treats the served directory as a fixed set of models to review, so nothing in M1 to M8 watches them: the viewer loaded `/manifest` once and never again. That is fine for reviewing a folder someone hands you, and it breaks the loop the tool is actually for, which is the agent editing a CAD source, regenerating an STL, and the human looking at the result. Until `ModelsWatcher` existed the human went on seeing the previous geometry until they reopened the page. It follows `CalloutsWatcher`'s shape, pushes a payload-free `models_changed`, and drops the route layer's cached directory scan before announcing, since that one-second cache would otherwise answer the refetch its own event provoked with a scan from before the change. Its change signal is size and modification time with a content digest for the cases those cannot settle; see `tests/test_models_watcher.py` for which cases those are and why they are not hypothetical.

This document is the contract for the re-architecture. It is written to be sufficient for an implementer who has this file and the repo and nothing else. Where a claim about `claude-agent-sdk` matters, it is marked VERIFIED and the verification is stated, because several of these facts are counter-intuitive and one of them contradicts the SDK's own docstrings.

## 1. Goal

Today Mesh is a static-file server plus two JSON side-channels. A human clicks an STL in a browser to drop pin-comments; a separately-running agent reads and writes files on disk to exchange located feedback.

The target: `annealage-mesh` run in a folder scaffolds a project, launches the UI, and behaves like Claude Code in that folder, except the chat interface is a second pane beside the 3D viewer. Mesh operations are exposed to the model as tools so it can drive the viewer directly. The chat accepts image uploads and offers a sketch overlay on the 3D view. Transcript, images and models live in the project folder, which uses git by default when git is installed.

## 2. Verified SDK facts that constrain the design

All verified against `claude-agent-sdk` 0.2.135 with `claude` CLI 2.1.227, by source reading plus live probes. Do not re-derive these; do not trust the SDK docstrings over them.

1. **The model-visible name of an in-process MCP tool is `mcp__<server>__<tool>`.** VERIFIED by probe: a tool registered as `get_model_facts` on server `mesh` arrived as `ToolUseBlock(name='mcp__mesh__get_model_facts')`. The `create_sdk_mcp_server` docstring example showing `allowed_tools=["add", "multiply"]` is misleading. Every allow-list entry, deny-list entry and hook matcher must use the namespaced form or it silently never matches.

2. **Listing a tool in `allowed_tools` bypasses `can_use_tool` entirely.** VERIFIED by probe, which emitted `CanUseToolShadowedWarning: can_use_tool will not be invoked for: Read. An allowed_tools entry that allows a whole tool auto-approves it before the callback is consulted.` So `allowed_tools` is a list of decisions to never ask the human. Allow rules in settings files shadow the callback too and are invisible to the SDK; the guess in this fact's original wording, that controlling `setting_sources` is what contains that, is wrong, and facts 21 to 23 below give the measured behaviour and the control that does work.

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

18. **With the sandbox active, filesystem-confined bash is not prompted for, and the containment is real.** VERIFIED once `socat` was installed, against a broker that denied everything: for a task of ordinary file operations the broker was consulted **zero** times, a write inside the working directory succeeded, and writes to `$HOME` and to `/tmp` both failed with `Read-only file system`. The same probe with the flag off consulted the broker once and wrote nothing, so the flag is doing exactly what it says.

    **A network-touching command still reaches the broker even so.** In a separate probe with the sandbox active and the flag on, `curl https://example.com` **was** consulted and refused, while the file operations alongside it were not. So the auto-approval covers commands the CLI judges filesystem-confined, and the human still decides anything that wants to leave the machine. That is a better posture than "bash never prompts", and it is worth stating precisely because the looser wording would have promised the human less oversight than they actually have.

19a. **Sandbox network egress is a proxy reached through `socat`, which is why it is a hard dependency on Linux.** Bubblewrap puts the child in an isolated network namespace; inside it the CLI runs `socat TCP-LISTEN:3128,fork,reuseaddr UNIX-CONNECT:<socket>` and the same for 1080, and injects `all_proxy=http://srt.<id>:<secret>@localhost:3128` into the child's environment, so every outbound connection goes through a host-side proxy where the allow and deny policy actually lives. Git over SSH is proxied the same way, via `ProxyCommand='socat - PROXY:localhost:%h:%p'`. Without `socat` there is no route out of the namespace at all, so the CLI refuses to enable the sandbox rather than running it with filesystem isolation and no network. VERIFIED by reading those command strings out of the bundled CLI and by dumping the proxy environment from inside a sandboxed shell, where DNS resolution also failed, as an isolated namespace implies. `socatPath` is documented as Linux and WSL only; macOS and Windows use different mechanisms. Consequence for packaging: `bwrap` and `socat` are runtime dependencies of the **default** posture, not development tools, so `doctor` checks for them and the install documentation says so.

19. **The sandbox restricts writes and network, not reads.** In the same probe, `cat ~/.bashrc` succeeded, unprompted. This follows from `SandboxSettings`' own documentation, which says filesystem read restriction is configured with Read **deny rules** rather than by the sandbox, and it is the honest limit of the containment: a sandboxed bash can still read anything the user can, including `~/.ssh` and cloud credentials. That matters more here than in a general-purpose agent, because this tool reads STL comments and filenames, which are untrusted input, while holding a shell. Adding deny rules for the obvious secret locations is the mitigation; it is deliberately not done yet, because a deny list broad enough to help is also broad enough to break legitimate work, and choosing it is a product decision rather than an implementation detail. Recorded in section 6 as deferred, not overlooked.

20. **A requested sandbox cannot be confirmed active from the child's output alone.** The CLI announces a sandbox it could not engage and says nothing about one it could, and that announcement arrives when a bash command first runs, not at connect time. So a startup banner claiming ACTIVE purely because nothing has complained yet is claiming something it does not know, which is how the first implementation of this got it wrong on a host with `socat` missing. What is checkable at startup is the negative case, by looking for the dependencies the CLI's own message names, and the child's report supersedes that whenever it arrives.

### 2b. The served directory is executable configuration

These seven facts are why `session/workspace_trust.py` exists. Each is VERIFIED by live probe against 0.2.136 unless stated otherwise, and together they say that the directory Mesh is pointed at can decide what the agent is allowed to do, which is not a property a directory of STL files should have.

21. **A `.claude/settings.json` or `.claude/settings.local.json` in the served directory shadows `can_use_tool` entirely.** VERIFIED: with `{"permissions":{"allow":["Read"]}}` in either file, a `Read` ran with the broker consulted **zero** times and its contents reached the model. So a file in the reviewed directory, not the human, decided that call.

22. **`setting_sources` does not gate that.** VERIFIED against three values: `["user","project","local"]`, `["user"]`, and the field omitted entirely. In all three the directory's permission rules still applied and the broker was still skipped. Fact 13 is about which settings the CLI *loads for configuration*; it is not a containment control, and the guess that it was one was wrong.

23. **Settings files in the served directory can declare hooks, and hooks are shell commands that execute.** VERIFIED: a `.claude/settings.json` declaring `SessionStart` and `PreToolUse` command hooks ran both, proved by the marker files the commands themselves created. `SessionStart` runs when the session opens, before any prompt is sent and before any permission callback exists. So configuration in the served directory is arbitrary code execution at startup, and no in-process control can be early enough to intercept it. A `.mcp.json` is the same shape of problem, since its entries name commands to spawn; `strict_mcp_config=True` is set so only the servers this process names are used.

24. **The CLI's own defence against this is a dialog the SDK skips.** Not a probe: `claude --help` states under `--print` that "The workspace trust dialog is skipped when Claude is run in non-interactive mode (via -p, or when stdout is not a TTY...). Only use this in directories you trust." Every SDK session is non-interactive, so the trust decision is the embedding application's to make, and Mesh has to make it.

25. **A `PreToolUse` hook passed through `ClaudeAgentOptions` is upstream of both the settings allow rules and the sandbox's auto-approval.** VERIFIED four ways: with a settings file allowing `Read`, the hook still ran and its `permissionDecision: "deny"` blocked the call; the same held for `Bash` with a settings file allowing `Bash`; with the sandbox active and `autoAllowBashIfSandboxed` on, the hook ran for a contained command the broker was never consulted about, and could deny it; and returning `{}` expressed no opinion, leaving that same command to run unprompted. So a hook is the only control that sees every tool call, and a silent one costs the posture nothing.

    Returning `permissionDecision: "ask"` also works and re-enters the normal flow, so a settings-allowed call reaches `can_use_tool` after all. It is deliberately not used: it would also override Mesh's own `allowed_tools`, so the read-class mesh tools would start prompting, and it would override the sandbox's auto-approval, so every contained bash command would prompt too. The tripwire denies on a changed digest instead, which leaves both intact.

26. **A settings file written mid-session takes effect within that session.** VERIFIED: turn 1 got `.claude/settings.local.json` written, and turn 2's `Read` then ran with the broker consulted zero times. So the CLI re-reads these files while the session runs, and a startup gate alone is not sufficient; the digest has to be re-checked per tool call.

27. **`allowUnsandboxedCommands` defaults to true, and the model uses it.** VERIFIED: when the sandbox refused a write to `.claude/settings.local.json`, the model retried with the sandbox disabled and said so in its own words, "Retrying without the sandbox restriction since this is an explicit, legitimate request to write that exact file". The retry reaches the broker, so a human is asked, but they are asked to approve a command whose containment has already been dropped, on a card that looks like any other. Set false, both attempts failed with `Permission denied` and the escape was unavailable.

    Worth noting for its own sake: the CLI's sandbox already denies writes to `.claude/settings.local.json` specifically, which is a useful second layer but not a sufficient one, since the `Write` tool reaches the same path through the broker.

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

Known wart to document in code: microdot's body limits are class attributes and therefore process-global (default 16 KiB). `app.py` raises `Request.max_content_length` to admit an upload and sets `Request.max_body_length` to 0, which is what actually makes a body stream: microdot buffers into `req.body` whenever a declared length is at or under `max_body_length`, and `req.stream` is only the raw reader above it. Every route in this process that reads a body therefore reads it from the stream, bounded by `Content-Length`, and refuses a request that declares none rather than waiting on a socket that never ends.

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
    turn_images.py   200    image_path expansion: containment, caps, fact 6's ordering
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

Git: `git init` runs when git is installed and the folder is not already a repo or inside one, with one scaffold commit. Nothing is auto-committed afterwards, because a tool that silently commits a human's working folder destroys their ability to stage their own work. An explicit write-class `mesh_snapshot(message)` tool, letting the model propose a commit through the permission broker, is the intended way to allow one; it is described here but scheduled in no milestone and is not built, so it is listed in section 6 rather than claimed as present. If `user.email` is unset, skip the commit and say so rather than failing or inventing an identity. Binary bloat is left to the user: `.gitignore` does not exclude `models/` or `images/`, and the scaffold notes git-lfs as an option without configuring it.

### 3.9 Tool surface

Flat, seventeen tools, declared with `@tool` and registered through `create_sdk_mcp_server(name="mesh", ...)`. Flat rather than the two-tier `search_actions`/`execute_action` shape Canvas uses, because the probe showed the CLI already surfaces tools through its own deferred `ToolSearch` mechanism, so a second discovery layer would duplicate it. Tool descriptions are therefore search targets and must be written to be findable. Revisit tiering only past roughly twenty tools.

The tools are graded by what a mistake would cost, in three grades rather than two. Amended 2026-08-13 by the maintainer's decision, after M6 shipped the two-grade version this section originally specified; see the M6 record in section 4 for what was measured before and after.

**Read-class**, which changes nothing, pre-allowed in `allowed_tools` so it never prompts: `mcp__mesh__list_models`, `model_info`, `get_view`, `get_visibility`, `list_comments`, `list_callouts`, `capture_view`, `measure`.

**View-class**, which changes only what is on the screen, also pre-allowed: `set_view`, `fit_view`, `set_visibility`, `set_up_axis`, `select_pin`. Not because these are harmless in the abstract, but because an approval card is the wrong control for them. The loop this tool exists for has the model reframing a part it has just regenerated, several times a turn, in front of a human who is looking at that screen; a card per camera move is either clicked unread or disposed of with one standing grant, and both are worse than not asking. The control that fits is the pause switch below, which refuses all of them at once for as long as the human wants the view to hold still.

**Write-class**, which leaves something behind after the page closes, deliberately absent from `allowed_tools` so it reaches the broker and therefore the human: `add_callout`, `delete_callout`, `snapshot`, `export_transcript`.

`export_transcript` is write-class because an exported transcript may then be committed and can carry whatever was typed about the hardware under review, plus absolute paths and per-turn costs. The human's own export button bypasses the broker entirely, which is the right asymmetry.

The two sets the code acts on are therefore **not** the same set: what never prompts is read plus view, and what the pause switch refuses is view plus write. A tool that is in neither of those, or in the wrong one, is a different product decision each way, so both are asserted exhaustively in `tests/test_tools.py` rather than sampled.

`capture_view` returns an image content block per fact 8, not a file path. Geometry facts come from `stl.py`, a small dependency-free binary and ASCII STL reader for header, bounding box and triangle count, rather than a mesh library, because that is all the model needs and it avoids a dependency. Watertightness is deliberately not offered, because computing it properly needs real topology work.

Not offered as tools, deliberately: anything that types into the composer on the human's behalf, anything that submits the human's pins, and continuous camera animation. The model may move the camera, which is visible in the 3D pane, and a `paused` flag the human can set from the topbar makes every view-class and write-class mesh tool return an error, so the human can line up a view or edit a pin comment without the agent mutating underneath them. That idea is lifted from Canvas's read and write classification. Since the view-class tools prompt for nothing, this flag is the only control over them rather than a second one on top of a card, which is why it is enforced in the server and tested end to end.

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

`README.md` and `SKILL.md` both advertised zero runtime dependencies, Python 3.9+ and near-instant `uvx` start. Those claims are rewritten rather than quietly dropped: two runtime dependencies and Python 3.10+.

This section originally also required the install size to be stated plainly in the README, on the reasoning that a reader deciding whether to install should not be surprised by it. Reversed by the maintainer on 2026-08-13: 90 MB is unremarkable for installing an application, so calling it out gave it a prominence it does not warrant and made the install section read as an apology. The reasoning behind the dependency decision stays recorded here and in `pyproject.toml`, where someone asking why the package is that size will look.

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

M6 landed with the following differences from the above, and one fact from the plan's own section 3.9 that turned out to be wrong about a browser.

**Sixteen tools, not the plan's seventeen: `export_transcript` stays in M8.** Section 3.9 lists it among the write-class tools, and M8's brief lists it too, together with `POST /session/<sid>/export` and the pane button it shares an implementation with. Building the tool here would have meant building the export itself here; it is classified and listed nowhere in M6, so the classification check described below refuses to build a server that half-implements it.

**The pause flag is enforced in the server, and the browser's control is a display of it.** Plan section 3.9 said only that "a `paused` flag the human can set from the topbar makes all write-class mesh tools return an error", which is where the deleted first attempt at this went wrong: a checkbox that latched locally, sending a frame nothing served. The flag lives on the `ViewerBus`, an inbound `pause` frame is what moves it, and a `pause_changed` event announces every change to every viewer, with the current value also in the `hello` frame because a tab that connects later has no event to learn it from. The topbar button never changes its own appearance on click; it changes when the server says the flag moved. Read-class tools are deliberately not gated: pausing exists so the human can work without the view moving, and a model that goes on reading is better informed when the pause lifts.

**The `pause` frame is an addition to section 3.3's inbound catalogue**, alongside `build_refused` from M4. `PROTOCOL_VERSION` does not move for either: an older page never sends a `pause` frame, and both directions already ignore a type or an event kind they do not recognise.

**The classification is one list, and building refuses to proceed without it.** `tools/registry.py` holds the three tuples, `session/sdk.py` re-exports the pre-allowed set rather than restating it, and `MeshTools` raises at construction if a built tool is unclassified, a classified name is not built, or a name appears in two grades. The alternative was a tool added to a handler module quietly defaulting into a posture nobody chose, and every default is wrong for something: read removes both the card and the pause switch's hold on it, view removes the card alone, and write is a name the allow list can never match.

**The grading changed after M6 shipped, on the maintainer's decision of 2026-08-13, and both postures were measured against the live CLI.** M6 first landed the two grades section 3.9 originally specified, which put every camera move through the broker. Probed on the two-grade version: a turn calling `get_view`, `list_models` and `set_view` consulted the broker once, for `set_view`. Probed again after the five view-class tools moved to pre-allowed: the same turn consulted the broker **zero** times and the camera still moved, and a separate turn that hid a part and then added a callout consulted it exactly once, for `add_callout` alone, with the callouts file written only after that allow. So the split is real in both directions and neither half was taken on trust. What changed the decision was the reading of section 3.9's own pause paragraph: it justifies letting the model move the camera on the grounds that the move is visible in the 3D pane, which is an argument for the pause switch being the control and against a card that would be clicked unread.

**`MAX_WS_MESSAGE` moves from 256 KiB to 4 MiB, and the browser bounds its own capture.** A `result` frame answering `capture_view` carries a screenshot as a base64 data URL, which no other frame's size anticipated. The failure mode made this worth care rather than a bigger number: microdot raises `WebSocketError('Message too large')` on an oversized inbound frame, which the route cannot answer, so the connection simply drops and the human's tab reconnects with no explanation. `js/commands.js` therefore tries PNG then three JPEG qualities and returns a failure rather than a frame it knows the server will reject, and the server's ceiling is the backstop rather than the control.

**A capture is taken synchronously, render to `toDataURL`, and that is not a style choice.** The renderer has no `preserveDrawingBuffer`, so the drawing buffer is cleared when the browser next composites; an `await` anywhere between the render and the read lets a `requestAnimationFrame` land in between and returns a valid PNG of nothing. `tests/test_viewer_e2e.py` decodes the returned PNG's IHDR and checks both its dimensions and that it is not a trivially compressible blank, because a blank capture is exactly what this would look like if the ordering regressed.

**`fitView` now reports whether it framed anything.** With no mesh both loaded and visible it moves nothing, and the camera afterwards is indistinguishable from a successful fit, so a tool call that returned success would be telling the model the view is now showing something. The click handler and the keyboard shortcut ignore the return value.

**Measured against the live CLI, not assumed** (probe, 0.2.136, `claude-haiku-4-5`): the tools arrive as `mcp__mesh__<tool>`; a `capture_view` result carrying an image block reached the model as a real image, which it described correctly by colour, so fact 8 is now probe-verified rather than source-verified; and the paused refusal reached the model verbatim, which it quoted back while going on to use a read-class tool successfully. The broker measurements are in the paragraph above, since they are what the grading change turns on. The probe also confirmed section 3.9's premise directly: the model called the CLI's own `ToolSearch` before every mesh tool, so these tools are discovered by description, and a description written as a label rather than as a sentence about when to reach for the tool is a tool that will not be found.

**M7. Images and sketch overlay.** `POST /upload`, `images/`, composer paste, drag and drop and file picker, `js/sketch.js` with stroke capture and compositing, and the dual delivery of fact 6. Sketch ships image-only first, with stroke unprojection to 3D coordinates as a follow-on because it is the part most likely to need iteration. Tests: `tests/test_upload.py`, plus a Playwright sketch round-trip. Deliverable: point at the model by drawing on it. Demo: circle a wall, ask why it is thin, get an answer about that wall.

M7 landed with the following differences from the above, three of them from ceilings that live outside this process and one from a claim in section 3.2 that was not true of the code it described.

**Section 3.2's body-limit paragraph is corrected, and `Request.max_body_length` is 0.** That paragraph said the upload route "streams to disk via `req.stream` rather than buffering, so the raised limit is not a memory amplifier on other routes". Microdot buffers a request body into `req.body` whenever `content_length <= Request.max_body_length` and only then leaves `req.stream` as the raw reader, so with both limits at 8 MiB an upload was fully buffered before its handler ran and `req.stream` was an `AsyncBytesIO` over bytes already in memory. Streaming is now real: `max_content_length` stays the accept ceiling and `max_body_length` is 0, microdot's own documented value for "always read from the stream". That makes `/submit` a body reader with no buffer too, so it reads exactly `Content-Length` bytes from `req.stream` and answers 411 when no length is declared, which it has to, because reading a stream with no length to bound it waits on a socket that never ends. Two routes read a body in this process and both were converted; the same change is why `ws.py`'s explicit `max_message_length` is now load-bearing rather than merely tidy, since the `-1` fallback is `max_body_length` and an inherited ceiling of zero refuses every frame.

**What may be stored and what may be sent inline are two different ceilings.** `paths.MAX_IMAGE_BYTES` (8 MiB) bounds what lands under `images/`; `paths.MAX_INLINE_IMAGE_BYTES` (3.5 MiB decoded) bounds the base64 copy a turn carries, because the Messages API refuses an inline image whose payload runs past roughly 5 MB and fails the whole request rather than that one block. A file above the inline ceiling is stored, served, and named to the model in the turn's text with the tool that can read it, rather than being dropped or inlined into a request the API would refuse: a photograph a human attached deliberately is evidence worth keeping at full resolution, and failing the whole turn over it would lose the question as well as the picture. Downscaling instead would mean an image library, and two runtime dependencies is a property of this package worth more than the convenience.

**The SDK transport's 1 MiB read cap does not need raising, and finding that out took a probe rather than a reading.** Review of M7 argued a blocker: the transport refuses any stdout line longer than its buffer, 1 MiB by default, the pump ends the session after five consecutive parse failures, and fact 7 establishes that the CLI echoes a user message back with its image block intact, since the parser could not drop a block that never arrived. A conversation record on disk supported it, one line of 330,846 bytes carrying a 162,940-character base64 payload. `max_buffer_size` was set on that reasoning and then removed, because three probes against the live CLI say the premise is wrong:

- One turn carrying an inline base64 image, plain `-p` framing: the model named the colour correctly, and no stdout line carried the base64 back.
- Two turns with the exact flag set the SDK builds (`--output-format stream-json --verbose --include-partial-messages --input-format stream-json`, no `-p`), the first provoking a Bash call so a user-role line certainly appeared and the second requiring the image from history: 83 lines, the model answered from the image both times, `user` lines present at 439 bytes carrying the tool result, and again nothing carrying the base64.
- A `capture_view` returning a **2.40 MiB** base64 image through mesh's own `MeshTools` against a stub bus: the model described the image, so fact 8 held at that size, and the echoed `user` line carrying that tool result was **49,039 bytes**. The widest inbound message of the whole run was 53,283 bytes.

So the CLI elides image content from what it writes back, in both directions, and nothing in this design approaches the default. The 330,846-byte line is real but it is a transcript file the CLI writes for resume, not a line the SDK reads. What the episode is worth recording for is the shape of the mistake: the inference from fact 7 was reasonable, the supporting evidence was real, and both were about the wrong stream. `paths.MAX_INLINE_IMAGE_BYTES` keeps its own justification, which is the API's inline-image limit and nothing to do with the transport.

**An empty text block fails the whole turn, so the composer stopped sending one.** An attachment with no typed message is a case the composer supports, and it was appending `{"type": "text", "text": ""}` alongside the image. The API refuses empty and whitespace-only text blocks outright, and the bundled CLI carries a dedicated `empty_text_block` error class for that 400, so the turn would have failed with the image never seen. Neither test layer could catch it, since one ends at a fake session and the other at a fake transport. The composer now omits the block, and the expansion drops any blank one, because a hand-assembled `turn` frame can carry one too.

**The turn expansion is its own module.** `session/turn_images.py` holds it, because `session/sdk.py` owns the client, the options and the message pump and the expansion is none of the three: it is a pure function over a block list and a directory. That is also what lets its reordering and its refusals be asserted directly, rather than only through a real `ClaudeSDKClient` on a fake transport, which `tests/test_turn_images.py` still does for the separate question of whether the result is what actually leaves the process.

**A sketch is refused when the view moved under it.** Strokes mean "this spot on the model", which is only true of the view they were drawn against, and two routine things invalidate that between the first stroke and Attach: the human resizing the window, which reframes the render, and the agent calling `set_view` or `fit_view`, which are pre-allowed as of the grading change above and so need no approval card. The overlay stops OrbitControls from seeing a pointer event but cannot stop a programmatic move. Attach therefore compares the camera and the container box against what they were at the first `pointerdown` and refuses when they disagree, keeping the strokes so the human can put the view back rather than starting over. The composite is also bounded by `commands.js`'s own capture width and character ceiling, since it has the same destination as a `capture_view` result.

**Every exit past the open removes the file.** `POST /upload` streams onto a descriptor from `paths.create_unique_image_file`, and a peer that disappears mid-body (a closed tab, an aborted fetch, a dropped link) raises out of the stream read past every refusal the route decides for itself. Unhandled that leaks one descriptor and one truncated image per occurrence, ending at EMFILE with no upload succeeding again and truncated images left in a git-tracked directory `/asset` serves. One handler covers everything after the open, catching `BaseException` so a cancelled task is covered as well as a reset connection.

**`/upload` checks `Origin`, which section 3.10 item 3 asked of `/ws` alone.** A POST carrying a raw body is a CORS simple request, so no preflight stands in front of it, and this is the one route in the process that writes into the human's project. It was the one write route with nothing but the secret in front of it.

**The attachment strip is one list, in attach order.** A slot is reserved before its upload starts and filled when it lands, so the chips and the `image_path` blocks follow the order the human attached things rather than the order the network answered in, and the cap refuses a file before a byte leaves the page or a file is written that no message will reference. Send is refused while any slot is still uploading: sending regardless posted a turn without the image and carried it on the next message instead, which reads as the picture having been ignored.

**`.webp` is served as an image.** `_IMAGE_NAME_RE` and `review_tools`' snapshot suffixes both write webp, while `paths.ASSET_CONTENT_TYPES` had no entry for it, so a webp under `images/` was served as `application/octet-stream`.

**M8. Project scaffolding, settings, git, docs.** `project.py`, `cli.py` subcommands, `doctor`, `CLAUDE.md` generation, `.gitignore`, `git init` plus scaffold commit, the settings window and `GET`/`PUT /settings` with `platformdirs` wiring and provenance, the `export_transcript` tool plus `POST /session/<sid>/export` plus the pane button, and the README, SKILL.md, RELEASING.md and COMMERCIAL.md updates including the honest dependency claim, the `--host` release note and a section on `tailscale serve`. Tests: `tests/test_project.py`, `tests/test_cli.py`, `tests/test_settings.py`, `tests/test_export.py`. Deliverable: `annealage-mesh` in an empty folder produces a working project, configurable from the browser. Demo: `mkdir demo && cd demo && annealage-mesh`, then change the model from the settings panel and export a transcript.

M8 landed with the following differences from the above, one shipped bug it uncovered in a mode nothing was exercising, and one design distinction the brief did not anticipate.

**`settings.py` and `diagnostics.py` are their own modules.** Section 3.6's table lists only `project.py`, described as "scaffold, .gitignore, git init and first commit, config". Three-layer resolution with provenance, validation, two refusals and a TOML emitter is its own subject at about 600 lines, and this plan already specifies a separate `tests/test_settings.py`, so config lives in `settings.py` and `project.py` keeps the scaffold and git. `diagnostics.py` exists because section 3.7 wants the settings window's Diagnostics block to duplicate `annealage-mesh doctor`: duplicating the display is the point, duplicating the implementation is not, so `doctor` and `GET /settings` read one collector.

**Two new dependencies, and the README's dependency count was wrong either way.** `platformdirs` is what section 3.12 already planned. `tomli` is the one this plan missed: `tomllib` is stdlib only from 3.11 and this package's floor is 3.10, so reading a config file on 3.10 needs the backport that `tomllib` was vendored from. There is deliberately no TOML *writer* dependency, because what this package writes is a closed set of scalar keys it defines itself, so a small emitter with its own round-trip tests is less surface than another package. The README said "two runtime dependencies" and now says four, naming what each is for.

**Eight settings keys, not the fourteen section 3.8 lists.** A key that resolves and reports provenance but that nothing reads is a fabricated contract, so the table holds only keys with a consumer: `host`, `port`, `open_browser`, `model`, `effort`, `permission_mode`, `up_axis` and `tool_cards_collapsed`. Omitted, each for a stated reason rather than by oversight: the **upload size cap**, because the ceiling is `Request.max_content_length`, a process-global microdot class attribute set once in `app.configure_request_limits`, so a per-run setting above it would be a setting that does not work; **sketch stroke defaults**, because `js/sketch.js` has one fixed stroke and no per-user variation to feed; **chat pane width**, because `js/layout.js` is a media query with no resizer, so there is no width to persist; **project name**, because nothing displays one; and **default part visibility**, because it is list-valued and `js/models.js` has no boot-time visibility input. `effort` was kept only after checking that `ClaudeAgentOptions.effort` exists in 0.2.136, and it gained a `--effort` flag that section 3.5's flag list does not have, because every other agent-facing key has one and a key settable only by editing a file is a wart.

**"In effect" means different things for different keys, and conflating them is a lie either way round.** This is the distinction the brief for M8 did not have. A restart-effect key such as `port` is in effect as the process resolved it at startup, whatever the file says now, so `GET /settings` reports the startup value and puts anything newer on disk under `pending`; reporting the file would claim a port the server is not listening on. A load-effect key such as `up_axis` is applied by each page as it loads, so for a browser asking now the value on disk *is* the one in effect, and reporting the startup value would leave every page after a change applying a preference the human had already replaced. The first implementation treated both the same, and a browser test caught it: a saved `up_axis` never reached a freshly loaded page. The settings window's controls edit the *saved* value for the same reason, since a form pre-filled with the running value would silently revert the last change when someone pressed Save with no edits.

**`--force` is defined here, because section 3.5 lists the flag without saying what it forces.** It means: `init` regenerates the two generated files it otherwise keeps, `.gitignore` and `CLAUDE.md`. That is the only reading consistent with an idempotent `init` and with `CLAUDE.md` never being clobbered, and it is accepted on `init` alone.

**Scaffolding and git run synchronously, and after the lock.** Section 3.1 requires git and scaffold work to use `asyncio.create_subprocess_exec`, which is about not blocking a *running* loop; scaffolding happens in `cli.py` before any loop exists, so it uses `subprocess.run` and plain file IO, and introducing a loop to satisfy the letter of that rule would be worse code. It runs after the lock is acquired, so two starts against one project cannot both be writing those files, and after the workspace-trust gate, because git reads configuration out of the very directory being scaffolded. That last ordering is not sufficient on its own, and section 6 now records what it does not cover.

**The transcript filename is compact ISO 8601.** `transcript-20260813T154928Z.md`, not the extended form section 3.8 implies, because a colon in a filename is hostile on Windows and inside archives. The basic format is still ISO 8601.

**`annealage-mesh --no-agent` crashed on startup, and had since M5.** `create_app` dereferenced the session the factory returned without checking it, while the comment directly above it said the factory returns None for viewer-only. Nothing caught it because the two shapes look alike from outside: every `create_app` test passed `build_session=None`, the guarded branch, and every CLI test stubbed `app.run` so `create_app` never ran at all. It surfaced only because the `CLAUDECODE` check added here routes a bare invocation down the viewer path, which is also a reminder that the published skill's documented invocation was the broken one. `tests/test_app.py` now drives a factory that answers None, and the fix was confirmed to fail without it.

**`CLAUDECODE` is set in any shell Claude Code starts, which is where this suite is usually run.** Left alone, that made several CLI tests take the viewer branch, skip the lock they expected to be refused by, and serve a real port forever. `tests/conftest.py` clears it for the whole suite and `tests/test_cli.py` sets it deliberately, so what the variable does is asserted rather than inherited.

**Three near-identical `app.run` stubs, in three test files, all had to learn the new keyword.** They are left as three rather than consolidated, because each records different fields and merging them would rewrite assertions in all three; worth noting as the cost of the shape rather than as a defect.

**Ruff replaces the by-hand pyflakes pass, and `ruff format` is adopted at 100 columns.** `F` is pyflakes reimplemented, so nothing is lost. `E501` is off because the formatter owns line length and what E501 is left reporting is unwrapped docstring prose. `UP` is off because it wants some three hundred rewrites of the house `%` formatting and every `Optional[X]`. Three `PERF` rules are off because in this codebase they each argue for worse code, two of them for comprehensions that cannot replace a loop mutating an existing dict. What the selection did catch and what was fixed: nine `raise` statements inside `except` blocks that did not name their cause, four closures over a rebound loop variable in a threaded lock test, a `zip` of two sequences with no `strict=`, and two dict iterations that only used the values. `ASYNC` is selected deliberately: it enforces the no-blocking-IO-on-the-loop invariant that this design rests on and that had been resting on memory.

**The version is the git tag.** `hatch-vcs` resolves it at build time and `__init__.py` reads it back out of the installed package's metadata, so the two-file version bump `RELEASING.md` used to describe is gone, along with the possibility of the two disagreeing. Two consequences were verified rather than assumed: a build needs the tags present, since `actions/checkout` fetches none by default and would silently publish the fallback version, and the wheel that `uv build` produces from its own sdist has no `.git` and resolves the version from the sdist's `PKG-INFO`. `publish.yml` now also runs the whole suite against the released commit before uploading, because a version on PyPI cannot be replaced once it is there.

**Credential paths are refused, on top of the sandbox rather than by it.** Fact 19 stands unchanged: the sandbox restricts writes and network, not reads. What is new is a second `PreToolUse` hook, beside the configuration tripwire and sharing its matcher, that refuses any call naming one of nine credential locations: `~/.ssh`, `~/.aws`, `~/.config/gcloud`, `~/.kube`, `~/.gnupg`, `~/.netrc`, `~/.docker/config.json`, `~/.config/gh` and this agent's own `~/.claude/.credentials.json`. A hook is the mechanism rather than a settings `deny` rule for the reason fact 25 established: it is upstream of both a settings file's allow rules and the sandbox's auto-approval, and unlike a settings rule it cannot be shadowed by a file in the served directory.

The two halves are not equally strong and the difference is documented wherever the control is described. For a tool naming a file in an argument the check is exact: the path is expanded and `realpath`-resolved before comparison, so a symlink planted in the served directory pointing at `~/.ssh` is refused along with the direct spelling, and writes are refused as well as reads, since a write into `~/.ssh/authorized_keys` is the worse outcome. For `Bash` it is textual, so `cat ~/.ssh/id_ed25519` is refused while a path built from a variable, a glob, an encoding, or a `cd` first is not. `tests/test_secret_paths.py` asserts the uncovered cases as *allowed*, so the limit is a test rather than only a docstring and a later change claiming to close it has to come and say so.

Two things were verified against the real CLI rather than inferred, because both would have failed silently. The hook payload's field names, `tool_name` and `tool_input`, had never been read by this codebase: the configuration tripwire ignores them, so a wrong guess would have produced a control that installs, returns no opinion for everything, and reports nothing. And the deny had to actually stop a call the sandbox would otherwise auto-approve. A probe with the denied list pointed at a decoy directory showed both: two hook invocations, `Read` refused on the path and the model's `Bash` fallback refused on the command, the decoy's contents never reaching the model, and the model relaying the refusal and stopping rather than trying a third spelling. Worth recording that the probe first ran with `HOME` redirected, which broke the CLI's own authentication and produced an empty turn that looked like a field-name failure; redirecting the denied list instead is what made the plumbing observable.

The hook is installed unconditionally, which is a change from how the tripwire was wired: `hooks` used to be passed only when an accepted digest existed, and the credential refusal inherited that condition on first implementation. A session constructed without a digest is still a session holding a shell, so the two now share a matcher without sharing a precondition.

**Git's own configuration is guarded, conditionally, and the scope was measured rather than assumed.** A repository's `.git/config` can name commands git runs and `.git/hooks/` holds scripts it runs directly, and git is excluded from the sandbox by design (`SANDBOX_SETTINGS`) so it can work on the project's own repository. The first framing of this, recorded in section 6 after M8, was that scaffolding runs git in an untrusted directory and is therefore exposed. Measurement narrowed it: against a config whose `core.fsmonitor` writes a marker, `git rev-parse --is-inside-work-tree`, `git rev-parse --show-toplevel` and `git config user.email` execute nothing, while `git status` and `git add` execute it. Those first three are exactly what `project.py` runs against a directory it has not yet trusted, and it runs `add` and `commit` only inside a repository it has just created. So Mesh's own scaffolding is not the exposure; the agent's ordinary git commands are, and they run outside the sandbox with a human approving something that looks like `git status`.

The guard is therefore conditional, which is what makes it usable. Nearly every real project is a git repository, so gating on `.git/config` existing would ask about all of them and produce exactly the accept-without-reading habit that section 3.8 refuses to build for `CLAUDE.md`. Gating on the config naming a command asks about the ones that can act: `core.fsmonitor`, `core.pager`, `core.hooksPath`, an `alias` (any of them, since telling `!command` aliases from the safe kind is not worth getting wrong), a `filter` or `textconv` driver, `credential.helper`, `include.path`, and the rest of the list in `session/workspace_trust.py`. A config written by `git init` or `git clone` names none of them, and neither does this repository's own.

The same conditional keeps the in-session tripwire quiet during ordinary work, which is the property that would otherwise make the control unusable: the agent adding a remote does not change the digest, while the agent adding an alias does. Both directions are asserted. `.git/hooks` is guarded only for files that are executable and not the `.sample` set git ships, since a clone carries no hooks at all and the case that matters is a repository unpacked from an archive.

**A Content-Security-Policy, with `default-src 'none'` as the point of it.** Everything the viewer loads is same-origin and vendored, so the policy names each kind of fetch the page may make and refuses the rest: a source introduced later fails visibly rather than working quietly. The page's one inline script, the import map, is allowed by **hash** rather than by `'unsafe-inline'`, which would also allow whatever an injection managed to place in the markup. The hash is computed at startup from the packaged file that will actually be served, so editing the import map cannot leave a policy that blocks the page it is meant to allow, and a viewer file that cannot be read yields a policy allowing no inline script at all rather than falling back to allowing every one. The single inline `style` attribute in `viewer.html` moved into the stylesheet so `style-src` needs no exception either.

`img-src` allows `data:` because the sketch overlay composites strokes over a canvas snapshot through an `Image` whose src is a data URL; `connect-src` allows the WebSocket schemes because `/ws` is the transport. `frame-ancestors 'none'` and `base-uri 'none'` are not about this page's own fetches: they stop the viewer being framed by another origin and stop injected markup relocating every relative URL on it. `Referrer-Policy: no-referrer` goes with them, since the URL carries the per-run token.

What made this verifiable is that Playwright enforces CSP like any browser, so the fifty browser tests are the check that the policy does not break the viewer. Two of them failed on `unsafe-eval`, and both were the harness rather than the application: `page.wait_for_function` given a bare expression string evaluates it with `eval`, while the arrow-function form is compiled. The predicates were rewritten and the policy left alone, which is the right way round; a control weakened to suit a test harness protects nothing.

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
- Flat tool list, graded by what a mistake costs: read-class (changes nothing) and view-class (changes only the screen) are pre-allowed and never prompt; write-class (leaves something on disk) always prompts. The classification lives in exactly one place (`tools/registry.py`) and a tool that is not in it does not build, because every silent default is wrong for something.
- **The pause switch is server-side state, and it is the only control over the view-class tools.** It gates every view-class and write-class mesh tool, it is set by an inbound `pause` frame rather than by a browser-local toggle, and the browser's control shows what the server reports rather than what was clicked. A control that latched locally would read "paused" while the tools it claims to gate were still running, which is what the first attempt at it actually did.
- **A viewport capture is bounded by the browser, not by the server.** The page tries PNG then JPEG and fails the call rather than sending a frame over the socket's inbound ceiling, because an oversized frame is not refused: it drops the connection.
- **The served directory's Claude configuration is accepted by the human or the agent does not run.** `.claude/settings.json`, `.claude/settings.local.json`, `.claude/hooks/` and `.mcp.json` can each cause shell commands to run, one of them before any prompt is sent, so agent mode refuses to start when their content has not been accepted for that directory (facts 21 to 24). Acceptance is recorded per directory in the *user's* configuration directory, against a digest of the exact content reviewed, so a record can never ship inside the download it vouches for and any later edit asks again. A directory carrying none of these files, which is the ordinary folder-of-STLs case, is never asked about. `CLAUDE.md` is deliberately not gated: it cannot execute, every tool call it provokes still reaches the sandbox and the human, and gating it would prompt about nearly every real project and teach the human to accept without reading.
- **The same digest is re-checked on every tool call, and a change denies.** A settings file written mid-run takes effect immediately (fact 26), so trust established at startup does not persist by itself. The check is a `PreToolUse` hook because that is the only point upstream of both the settings allow rules and the sandbox's auto-approval (fact 25), and it watches the files rather than the ways they might be written, so it is indifferent to whether a change arrived through `Write`, a shell redirect or a `git checkout`. Silent while the digest holds, so the sandboxed-bash posture is unchanged.
- **The model may not drop its own containment.** `allowUnsandboxedCommands` is false (fact 27). The only ways to run outside the sandbox are the `excludedCommands` chosen here and viewer-only mode, which runs no agent at all.
- `capture_view` returns an image content block, not a path.
- **Agent mode is the default mode and viewer-only is the exception**, so the SDK is a base dependency rather than an extra, accepting a roughly 90 MB install. `requires-python >=3.10`; SDK pinned `>=0.2.136,<0.3`.
- Two fake layers: `AgentSession` fake for most tests, fake `Transport` as the SDK-upgrade canary.
- `/manifest` keeps `dir` and `path`; the test is fixed, not the response.
- `git init` plus one scaffold commit, and never an automatic commit after that.
- **The default agent posture is the full `claude_code` preset with bash sandboxed.** `sandbox={"enabled": True, "autoAllowBashIfSandboxed": True}`, so bash runs contained and is not prompted for, while Edit, Write and network access still reach the broker and therefore the human. Chosen over both alternatives (everything prompting; read-only until widened) because it is better than either on both axes at once: fewer approval cards for the human to clear, and filesystem plus network isolation that neither of the others has. Two consequences are not optional. The sandbox needs `bwrap` and `socat` present, so the banner reports whether it is **active** rather than merely requested (fact 16). And where it cannot engage, including Windows, the fallback is the everything-prompts posture, which fact 17 confirms happens automatically and safely; that fallback must be stated in the banner rather than being left for the human to infer from a sudden increase in prompts.

## 6. Deferred

- Stroke unprojection to 3D coordinates, after M7 ships image-only sketches.
- Per-user authentication for the LAN case, beyond the per-run token. `tailscale serve` covers the remote case better in the meantime.
- Closing the rest of the credential gap. The narrow refusal is built (see the record in section 4), and what it does not cover is a shell command that reaches a denied path indirectly, through a variable, a glob, an encoding, or by changing directory first. Closing that means intercepting at a layer that sees resolved paths rather than command text, which on Linux means the sandbox's own filesystem policy rather than a hook. Wanted; not attempted, because the honest partial control is worth more than a claimed complete one.
- `--fork`, mapping to `fork_session=True`, for branching a resumed session.
- A write-class `mesh_snapshot(message)` tool, so the model can propose a git commit and have it reach the human as a card. Section 3.8 describes it, no milestone scheduled it, and `tools/registry.py` refuses to build a tool it has not classified, so it is a deliberate gap rather than an oversight. The scaffold commit is the only commit this tool makes.
- An in-pane session picker, replacing the `-r` terminal listing. Wanted eventually, because a phone user has no terminal.
- Two-tier tool discovery, only if the tool count passes roughly twenty.
- Extracting a shared browser-control library across Annealage products. Canvas's ground truth was in-process LVGL state and it has no correlation table or pending-future map, so there is nothing to share yet. Revisit once Mesh's `viewers.py` and `commands.js` have proven themselves, and treat this plan's protocol as the candidate to generalise.
- JS unit tests, which would need a node toolchain. Front-end logic rides on Playwright until that trade changes.
- Git configuration reached indirectly. The trust gate now covers `.git/config` and `.git/hooks` when either names something executable (see the record in section 4), and what it does not follow is indirection: an `include.path` pointing at a file outside `.git/` is guarded by the key's presence, but the *included* file's content is not part of the digest, so editing that file changes what git runs without changing what was accepted. Following includes means resolving relative and `~`-relative paths the way git does, including conditional `includeIf` predicates, which is its own piece of work.

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
