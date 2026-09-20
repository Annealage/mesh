"""Tests for the Codex tool-exposure bridge
(``planning/tickets/phase3_codex-tool-mcp-bridge.md``): ``POST /mcp``
(``http/routes_mcp.py``) and the stdio-to-HTTP proxy
(``session/codex_mcp_stdio_bridge.py``).

Three layers, tested separately and then together:

- ``/mcp`` in isolation, against a bare ``Microdot`` app (``register_mcp_routes``
  called directly, no ``create_app``): the token/Origin gate, ``tools/list``,
  and ``tools/call`` including the write-class broker gate this bridge is
  responsible for wiring (``routes_mcp.py``'s own docstring explains why that
  gate lives here rather than in ``tools/registry.py``).
- The stdio proxy's ``Server`` (``build_server``) against a mocked HTTP
  authority, pinning that its ``list_tools``/``call_tool`` handlers forward
  correctly and nothing else.
- The full chain end to end: a real bound socket running ``create_app``, a
  real ``httpx`` connection, and a fake app-server (a genuine
  ``mcp.ClientSession`` connected in-memory to the proxy's own ``Server`` -
  ``mcp.shared.memory.create_connected_server_and_client_session``, the same
  helper the ``mcp`` SDK's own test suite uses to connect any client to any
  server) - the shape the ticket's own acceptance criteria ask for.

The last section proves the proxy subprocess itself exits cleanly when its
stdin closes (what a stdio-launched MCP server sees when its launcher goes
away), with no orphan left behind.
"""

import asyncio
import contextlib
import json
import socket
import subprocess
import sys

import httpx
import pytest
from microdot import Microdot
from microdot.test_client import TestClient

from annealage_mesh import app as app_module
from annealage_mesh.http.routes_mcp import register_mcp_routes
from annealage_mesh.session.codex_mcp_stdio_bridge import authority_url, build_server
from annealage_mesh.session.fake import FakeSession
from annealage_mesh.session.permissions import Decision, PermissionBroker
from annealage_mesh.tools.registry import MeshTools

pytestmark = pytest.mark.asyncio

TOKEN = "mcp-bridge-test-token"


class FakeBus:
    """The two members every mesh tool handler depends on
    (``tests/test_tools.py``'s ``FakeBus``, mirrored here rather than
    imported across test files)."""

    def __init__(self):
        self.paused = False

    async def call(self, method, params=None, *, timeout=None):
        return {}


class CountingBroker:
    """Counts calls into ``ask()`` and returns a canned ``Decision`` - what
    the write-class-gate tests below assert against, isolated from a live
    viewer/approval-card round trip (a real ``PermissionBroker`` is exercised
    directly in the end-to-end section further down)."""

    def __init__(self, decision):
        self.calls = []
        self.decision = decision

    async def ask(self, tool_name, input_data, context):
        self.calls.append((tool_name, input_data))
        return self.decision


@pytest.fixture
def project(tmp_path):
    (tmp_path / "cube.stl").write_bytes(b"solid cube\nendsolid cube\n")
    return tmp_path


@pytest.fixture
def mesh_tools(project):
    return MeshTools(FakeBus(), project, "sess-1")


def _mcp_app(mesh_tools, *, broker, token=TOKEN, allowed_origins=()):
    app = Microdot()
    register_mcp_routes(
        app, mesh_tools=mesh_tools, broker=broker, token=token, allowed_origins=allowed_origins
    )
    return app


def _client(app):
    return TestClient(app, host="127.0.0.1:8765")


async def _post(client, body, *, path="/mcp", token=TOKEN):
    query = ("?t=%s" % token) if token is not None else ""
    return await client.post(
        path + query,
        headers={"Content-Type": "application/json"},
        body=json.dumps(body),
    )


# ---------------------------------------------------------------------------
# the token/Origin gate, shared with /ws and /settings
# ---------------------------------------------------------------------------


async def test_no_token_is_refused_like_ws(mesh_tools):
    app = _mcp_app(mesh_tools, broker=None)
    client = _client(app)
    res = await _post(client, {"method": "tools/list"}, token=None)
    assert res.status_code == 403
    assert res.body == b"forbidden"


async def test_wrong_token_is_refused(mesh_tools):
    app = _mcp_app(mesh_tools, broker=None, token="the-real-one")
    client = _client(app)
    res = await _post(client, {"method": "tools/list"}, token="not-it")
    assert res.status_code == 403
    assert res.body == b"forbidden"


async def test_disallowed_origin_is_refused(mesh_tools):
    app = _mcp_app(mesh_tools, broker=None, allowed_origins={"http://127.0.0.1:8765"})
    client = _client(app)
    res = await client.post(
        "/mcp?t=%s" % TOKEN,
        headers={"Content-Type": "application/json", "Origin": "http://evil.example"},
        body=json.dumps({"method": "tools/list"}),
    )
    assert res.status_code == 403


# ---------------------------------------------------------------------------
# tools/list
# ---------------------------------------------------------------------------


async def test_tools_list_returns_every_mesh_tool_with_schema_and_description(mesh_tools):
    app = _mcp_app(mesh_tools, broker=None)
    client = _client(app)
    res = await _post(client, {"method": "tools/list"})
    assert res.status_code == 200
    tools = {t["name"]: t for t in res.json["result"]["tools"]}
    assert set(tools) == set(mesh_tools.tool_table())
    one = tools["list_models"]
    assert one["description"]
    assert one["inputSchema"]["type"] == "object"


# ---------------------------------------------------------------------------
# tools/call: read-class bypasses the broker entirely
# ---------------------------------------------------------------------------


async def test_read_class_call_never_touches_the_broker(mesh_tools):
    broker = CountingBroker(Decision(allow=True))
    app = _mcp_app(mesh_tools, broker=broker)
    client = _client(app)
    res = await _post(
        client, {"method": "tools/call", "params": {"name": "list_models", "arguments": {}}}
    )
    assert res.status_code == 200
    result = res.json["result"]
    assert not result.get("isError")
    assert result["content"][0]["type"] == "text"
    assert not broker.calls


# ---------------------------------------------------------------------------
# tools/call: write-class reaches PermissionBroker exactly once
# ---------------------------------------------------------------------------


async def test_write_class_call_reaches_broker_exactly_once_when_allowed(mesh_tools, project):
    broker = CountingBroker(Decision(allow=True))
    app = _mcp_app(mesh_tools, broker=broker)
    client = _client(app)
    res = await _post(
        client,
        {
            "method": "tools/call",
            "params": {
                "name": "add_callout",
                "arguments": {"point": [1, 2, 3], "comment": "hi"},
            },
        },
    )
    assert res.status_code == 200
    result = res.json["result"]
    assert not result.get("isError")
    assert len(broker.calls) == 1
    tool_name, input_data = broker.calls[0]
    # namespaced, matching Claude's own can_use_tool convention
    # (session/sdk.py) exactly - see routes_mcp.py's docstring for why.
    assert tool_name == "mcp__mesh__add_callout"
    assert input_data == {"point": [1, 2, 3], "comment": "hi"}
    # The handler really ran: a callout landed on disk.
    from annealage_mesh import paths

    assert paths.callouts_path(project).exists()


async def test_write_class_call_is_blocked_and_the_handler_never_runs_when_denied(
    mesh_tools, project
):
    from annealage_mesh import paths

    broker = CountingBroker(Decision(allow=False, message="not right now"))
    app = _mcp_app(mesh_tools, broker=broker)
    client = _client(app)
    res = await _post(
        client,
        {
            "method": "tools/call",
            "params": {
                "name": "add_callout",
                "arguments": {"point": [1, 2, 3], "comment": "hi"},
            },
        },
    )
    assert res.status_code == 200
    result = res.json["result"]
    assert result["isError"] is True
    assert result["content"][0]["text"] == "not right now"
    assert len(broker.calls) == 1
    # The handler itself never ran: no callouts file was ever written.
    assert not paths.callouts_path(project).exists()


async def test_write_class_call_fails_closed_with_no_broker_configured(mesh_tools):
    app = _mcp_app(mesh_tools, broker=None)
    client = _client(app)
    res = await _post(
        client,
        {
            "method": "tools/call",
            "params": {
                "name": "add_callout",
                "arguments": {"point": [1, 2, 3], "comment": "hi"},
            },
        },
    )
    result = res.json["result"]
    assert result["isError"] is True
    assert "no permission broker" in result["content"][0]["text"]


async def test_view_class_call_also_bypasses_the_broker(mesh_tools):
    """VIEW_CLASS is pre-allowed for Claude too (session/sdk.py's own
    allowed_tools) - only WRITE_CLASS goes through the broker here, matching
    that convention rather than inventing a stricter one for Codex."""
    broker = CountingBroker(Decision(allow=True))
    app = _mcp_app(mesh_tools, broker=broker)
    client = _client(app)
    res = await _post(
        client,
        {"method": "tools/call", "params": {"name": "fit_view", "arguments": {}}},
    )
    assert res.status_code == 200
    assert not broker.calls


# ---------------------------------------------------------------------------
# malformed requests and unknown tools
# ---------------------------------------------------------------------------


async def test_unknown_tool_name_is_an_error_result_not_a_protocol_error(mesh_tools):
    app = _mcp_app(mesh_tools, broker=None)
    client = _client(app)
    res = await _post(
        client, {"method": "tools/call", "params": {"name": "no_such_tool", "arguments": {}}}
    )
    assert res.status_code == 200
    result = res.json["result"]
    assert result["isError"] is True
    assert "no_such_tool" in result["content"][0]["text"]


async def test_unknown_method_is_a_400(mesh_tools):
    app = _mcp_app(mesh_tools, broker=None)
    client = _client(app)
    res = await _post(client, {"method": "prompts/list"})
    assert res.status_code == 400
    assert res.json["ok"] is False


async def test_missing_method_is_a_400(mesh_tools):
    app = _mcp_app(mesh_tools, broker=None)
    client = _client(app)
    res = await _post(client, {})
    assert res.status_code == 400


# ---------------------------------------------------------------------------
# the proxy's own Server, against a mocked HTTP authority
# ---------------------------------------------------------------------------


async def test_proxy_list_tools_forwards_to_the_authority():
    import mcp.types as types

    fake_result = {
        "result": {
            "tools": [
                {
                    "name": "list_models",
                    "description": "d",
                    "inputSchema": {"type": "object", "properties": {}},
                }
            ]
        }
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params.get("t") == TOKEN
        body = json.loads(request.content)
        assert body == {"method": "tools/list", "params": {}}
        return httpx.Response(200, json=fake_result)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        url = authority_url("127.0.0.1", 8765, "/mcp").copy_merge_params({"t": TOKEN})
        server = build_server(client, url)
        list_tools = server.request_handlers[types.ListToolsRequest]
        result = await list_tools(types.ListToolsRequest(method="tools/list"))
        tools = result.root.tools
        assert [t.name for t in tools] == ["list_models"]


async def test_proxy_call_tool_forwards_name_and_arguments_and_returns_the_result():
    import mcp.types as types

    fake_list_result = {
        "result": {
            "tools": [
                {
                    "name": "list_models",
                    "description": "d",
                    "inputSchema": {"type": "object", "properties": {}},
                }
            ]
        }
    }
    fake_call_result = {"result": {"content": [{"type": "text", "text": "ok"}], "isError": False}}
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        # call_tool's own wrapper warms its schema cache with a tools/list
        # round trip before validating input (Server._get_cached_tool_
        # definition) - the real authority sees this too, not only tools/call.
        if body["method"] == "tools/list":
            return httpx.Response(200, json=fake_list_result)
        assert body == {
            "method": "tools/call",
            "params": {"name": "list_models", "arguments": {"x": 1}},
        }
        return httpx.Response(200, json=fake_call_result)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        url = authority_url("127.0.0.1", 8765, "/mcp").copy_merge_params({"t": TOKEN})
        server = build_server(client, url)
        call_tool = server.request_handlers[types.CallToolRequest]
        req = types.CallToolRequest(
            method="tools/call",
            params=types.CallToolRequestParams(name="list_models", arguments={"x": 1}),
        )
        result = await call_tool(req)
        assert result.root.content[0].text == "ok"
        assert result.root.isError is False
        assert [c["method"] for c in calls] == ["tools/list", "tools/call"]


async def test_authority_http_failure_raises_authority_error():
    """The forwarding helper's own failure path: an unreachable/erroring
    authority raises ``AuthorityError`` rather than returning a silent empty
    result. Left to propagate out of the registered ``call_tool`` handler
    uncaught, the ``mcp`` SDK's own ``Server._handle_request`` turns any
    exception into a JSON-RPC error response rather than crashing the stdio
    session (verified by reading that function directly, not merely
    assumed - see ``codex_mcp_stdio_bridge.py``'s own ``AuthorityError``
    docstring), so this only needs to pin that the exception really is
    raised here.
    """
    from annealage_mesh.session.codex_mcp_stdio_bridge import AuthorityError, _call_authority

    transport = httpx.MockTransport(lambda request: httpx.Response(500))
    async with httpx.AsyncClient(transport=transport) as client:
        url = authority_url("127.0.0.1", 8765, "/mcp").copy_merge_params({"t": TOKEN})
        with pytest.raises(AuthorityError):
            await _call_authority(client, url, "tools/call", {"name": "x", "arguments": {}})


# ---------------------------------------------------------------------------
# end to end: fake app-server -> proxy Server -> real HTTP -> real /mcp ->
# tool_table()'s handler -> back. The ticket's own acceptance criteria.
# ---------------------------------------------------------------------------


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _run_real_server(project, *, token, broker):
    """Starts ``app_module.run`` as a background task on a real loopback
    socket, with a ``FakeSession`` standing in for the agent (no SDK, no
    subprocess) and ``broker`` wired the same way ``cli.py``'s own
    ``build_session`` wires it (``bus.broker = broker``, read by
    ``create_app`` once ``build_session`` returns - see ``app.py``'s own
    comment on that channel). Returns ``(port, task)``; the caller cancels
    ``task`` and awaits it to shut down.

    ``sessions.create_session`` scaffolds ``.mesh/sessions/<id>/`` first,
    the same way ``cli.py`` does before ever building an app: ``create_app``
    in agent mode opens ``events.jsonl`` inside that directory unconditionally
    (``session/events.py``'s ``EventLog``), which does not create its own
    parent directory.
    """
    from annealage_mesh import sessions as sessions_module

    mesh_session_id = sessions_module.create_session(project)
    port = _free_port()
    ready = asyncio.Event()

    def build_session(on_event, *, bus):
        bus.broker = broker
        return FakeSession(on_event)

    task = asyncio.ensure_future(
        app_module.run(
            project,
            "127.0.0.1",
            port,
            on_ready=ready.set,
            token=token,
            mesh_session_id=mesh_session_id,
            build_session=build_session,
        )
    )
    await asyncio.wait_for(ready.wait(), timeout=5.0)
    return port, task


async def _stop_real_server(task):
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5.0)


async def test_fake_app_server_lists_and_calls_a_read_tool_through_the_real_http_hop(project):
    from mcp.shared.memory import create_connected_server_and_client_session

    token = "e2e-read-token"
    broker = PermissionBroker(lambda e: None, timeout=2.0)
    port, task = await _run_real_server(project, token=token, broker=broker)
    try:
        url = authority_url("127.0.0.1", port, "/mcp").copy_merge_params({"t": token})
        async with httpx.AsyncClient() as client:
            proxy_server = build_server(client, url)
            async with create_connected_server_and_client_session(proxy_server) as session:
                tools = await session.list_tools()
                names = {t.name for t in tools.tools}
                assert "list_models" in names

                result = await session.call_tool("list_models", {})
                assert not result.isError
                payload = json.loads(result.content[0].text)
                rels = [m["rel"] for m in payload["models"]]
                assert "cube.stl" in rels
    finally:
        await _stop_real_server(task)


async def test_fake_app_server_write_call_reaches_the_broker_exactly_once_end_to_end(project):
    """The full chain's version of the write-class acceptance criterion:
    the fake app-server's call_tool for a write-class tool blocks on a real
    PermissionBroker.ask, which this test answers exactly once
    (broker.decide), and the tool only takes effect after that answer."""
    from mcp.shared.memory import create_connected_server_and_client_session

    from annealage_mesh import paths
    from annealage_mesh.session.base import PermissionRequest

    token = "e2e-write-token"
    events = []
    broker = PermissionBroker(events.append, timeout=5.0, no_viewer_grace=0.05)
    broker.viewer_connected()
    port, task = await _run_real_server(project, token=token, broker=broker)
    try:
        url = authority_url("127.0.0.1", port, "/mcp").copy_merge_params({"t": token})
        async with httpx.AsyncClient(timeout=10.0) as client:
            proxy_server = build_server(client, url)
            async with create_connected_server_and_client_session(proxy_server) as session:

                async def approve():
                    # Poll for the PermissionRequest broker.ask emitted, then
                    # answer it exactly once.
                    for _ in range(200):
                        request = next(
                            (e for e in events if isinstance(e, PermissionRequest)), None
                        )
                        if request is not None:
                            await broker.decide(request.request_id, "allow")
                            return
                        await asyncio.sleep(0.02)
                    raise AssertionError("no PermissionRequest was ever emitted")

                approver = asyncio.ensure_future(approve())
                result = await session.call_tool(
                    "add_callout", {"point": [1, 2, 3], "comment": "from codex"}
                )
                await approver

        assert not result.isError
        assert paths.callouts_path(project).exists()
        request_count = sum(1 for e in events if isinstance(e, PermissionRequest))
        assert request_count == 1
    finally:
        await _stop_real_server(task)


# ---------------------------------------------------------------------------
# the proxy subprocess itself: exits cleanly with no orphan left behind
# ---------------------------------------------------------------------------


async def test_proxy_subprocess_exits_cleanly_when_its_stdin_closes(project):
    """Simulates 'the parent codex app-server process exits': closing the
    subprocess's stdin is exactly what a stdio-launched MCP server sees when
    its launcher goes away (``mcp.server.stdio``'s own read loop ends on
    EOF, and ``Server.run()`` returns normally, per
    ``session/codex_mcp_stdio_bridge.py``'s own docstring) - proving no
    orphaned proxy process is left running once that happens.
    """
    token = "subproc-exit-token"
    broker = PermissionBroker(lambda e: None, timeout=2.0)
    port, task = await _run_real_server(project, token=token, broker=broker)
    proc = None
    try:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "annealage_mesh.session.codex_mcp_stdio_bridge",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--token",
                token,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        # A short wait so this does not race the subprocess's own startup
        # (stdio_server's async context manager spawning its reader/writer
        # tasks); closing stdin before it is listening would still be a
        # clean EOF either way, this only keeps the "still running" check
        # below meaningful rather than racy.
        await asyncio.sleep(0.3)
        assert proc.poll() is None, "the proxy exited before its stdin was ever closed"

        proc.stdin.close()

        loop = asyncio.get_running_loop()
        returncode = await asyncio.wait_for(loop.run_in_executor(None, proc.wait), timeout=10.0)
        assert returncode == 0
        proc = None
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        await _stop_real_server(task)
