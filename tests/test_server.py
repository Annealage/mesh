"""Headless server-contract tests for Annealage Mesh (no browser required)."""

import json
import socket
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from annealage_mesh.server import make_handler
from http.server import ThreadingHTTPServer


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_port(host, port, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError("server did not start listening on %s:%d" % (host, port))


@pytest.fixture
def served_dir(tmp_path):
    (tmp_path / "widget.stl").write_bytes(b"solid widget\nendsolid widget\n")
    return tmp_path


@pytest.fixture
def running_server(served_dir):
    host = "127.0.0.1"
    port = _free_port()
    handler_cls = make_handler(served_dir)
    httpd = ThreadingHTTPServer((host, port), handler_cls)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    _wait_for_port(host, port)
    base = "http://%s:%d" % (host, port)
    try:
        yield base, served_dir
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _get(url):
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.getcode(), dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read()


def _post_json(url, payload):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST",
                                  headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.getcode(), json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _post_raw(url, raw_bytes):
    req = urllib.request.Request(url, data=raw_bytes, method="POST",
                                  headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.getcode(), json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def test_index_serves_viewer_html(running_server):
    base, _ = running_server
    code, headers, body = _get(base + "/")
    assert code == 200
    assert "text/html" in headers.get("Content-Type", "")
    text = body.decode("utf-8")
    assert 'id="topbar"' in text
    assert "/manifest" in text


def test_manifest_lists_stl(running_server):
    base, _ = running_server
    code, headers, body = _get(base + "/manifest")
    assert code == 200
    assert "application/json" in headers.get("Content-Type", "")
    data = json.loads(body.decode("utf-8"))
    assert data == {"models": [{"name": "widget", "file": "widget.stl"}]}


def test_get_stl_file(running_server):
    base, served_dir = running_server
    code, headers, body = _get(base + "/widget.stl")
    assert code == 200
    assert body == (served_dir / "widget.stl").read_bytes()


def test_get_missing_file_404(running_server):
    base, _ = running_server
    code, _, _ = _get(base + "/does-not-exist.stl")
    assert code == 404


def test_path_traversal_blocked(running_server):
    base, _ = running_server
    code, _, _ = _get(base + "/../pyproject.toml")
    assert code == 404
    code, _, _ = _get(base + "/..%2f..%2fpyproject.toml")
    assert code == 404


def test_callouts_empty_when_absent(running_server):
    base, _ = running_server
    code, headers, body = _get(base + "/callouts")
    assert code == 200
    assert json.loads(body.decode("utf-8")) == {"annotations": []}


def test_callouts_served_when_present(running_server):
    base, served_dir = running_server
    payload = {"annotations": [{"id": 1, "author": "agent", "part": "widget",
                                 "label": "+Z", "point": [1, 2, 3], "comment": "hi"}]}
    (served_dir / "mesh-callouts.json").write_text(json.dumps(payload))
    code, headers, body = _get(base + "/callouts")
    assert code == 200
    assert json.loads(body.decode("utf-8")) == payload

    code, headers, body = _get(base + "/callouts.json")
    assert code == 200
    assert json.loads(body.decode("utf-8")) == payload


def test_submit_writes_comments(running_server):
    base, served_dir = running_server
    pins = [{"id": 1, "part": "widget", "label": "+X", "point": [1.0, 2.0, 3.0],
             "normal": [1.0, 0.0, 0.0], "faceIndex": 4, "comment": "looks thin here"}]
    code, data = _post_json(base + "/submit", pins)
    assert code == 200
    assert data["ok"] is True
    assert data["count"] == 1

    comments_file = served_dir / "mesh-comments.json"
    assert comments_file.is_file()
    record = json.loads(comments_file.read_text())
    assert record["count"] == 1
    assert record["annotations"] == pins
    assert "submitted_at" in record

    log_file = served_dir / "mesh-comments.log"
    assert log_file.is_file()
    log_lines = log_file.read_text().strip().splitlines()
    assert len(log_lines) == 1
    assert json.loads(log_lines[0])["annotations"] == pins


def test_submit_invalid_json_400(running_server):
    base, _ = running_server
    code, data = _post_raw(base + "/submit", b"{not json")
    assert code == 400
    assert data["ok"] is False


def test_submit_non_array_400(running_server):
    base, _ = running_server
    code, data = _post_json(base + "/submit", {"not": "an array"})
    assert code == 400
    assert data["ok"] is False
