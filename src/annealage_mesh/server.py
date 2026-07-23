"""HTTP server for Annealage Mesh.

Serves the packaged three.js STL viewer, auto-discovers ``*.stl`` files in a
served directory, accepts human pin-comment submissions, and serves
agent-authored callouts so both show up as pins in the viewer.

Files written/read in the served directory:
    mesh-comments.json   human submissions (overwritten on each /submit)
    mesh-comments.log    human submissions (appended, timestamped)
    mesh-callouts.json   agent-authored callouts (read-only from the server's
                         point of view; an agent writes this directly)
"""

import datetime
import json
import socket
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parent / "static"
VIEWER_HTML = STATIC_DIR / "viewer.html"

COMMENTS_JSON_NAME = "mesh-comments.json"
COMMENTS_LOG_NAME = "mesh-comments.log"
CALLOUTS_JSON_NAME = "mesh-callouts.json"

# Minimal extension -> content-type map (stdlib mimetypes misses .stl/.3mf).
CONTENT_TYPES = {
    ".stl": "application/vnd.ms-pki.stl",
    ".3mf": "model/3mf",
    ".step": "application/step",
    ".stp": "application/step",
    ".json": "application/json",
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".svg": "image/svg+xml",
}


def make_handler(serve_dir):
    """Build a request-handler class bound to ``serve_dir``.

    A fresh class is created per call (rather than mutating module globals)
    so multiple servers — e.g. in tests — can serve different directories
    without stepping on each other.
    """
    serve_dir = Path(serve_dir).resolve()
    comments_json = serve_dir / COMMENTS_JSON_NAME
    comments_log = serve_dir / COMMENTS_LOG_NAME
    callouts_json = serve_dir / CALLOUTS_JSON_NAME

    class Handler(BaseHTTPRequestHandler):
        server_version = "annealage-mesh/0.1"

        def log_message(self, fmt, *args):  # noqa: N802 - keep request log tidy
            sys.stderr.write("  %s - %s\n" % (self.address_string(), fmt % args))

        # --- helpers ---------------------------------------------------
        def _send(self, code, body=b"", content_type="text/plain; charset=utf-8"):
            if isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _safe_path(self, url_path):
            """Resolve a URL path to a file inside serve_dir, or None if it escapes."""
            rel = url_path.lstrip("/").split("?", 1)[0].split("#", 1)[0]
            if not rel:
                return None
            target = (serve_dir / rel).resolve()
            try:
                target.relative_to(serve_dir)
            except ValueError:
                return None
            return target

        # --- HTTP methods ------------------------------------------------
        def do_HEAD(self):  # noqa: N802
            self.do_GET()

        def do_GET(self):  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                if not VIEWER_HTML.exists():
                    self._send(500, "viewer.html not found in package static dir")
                    return
                self._send(200, VIEWER_HTML.read_bytes(), CONTENT_TYPES[".html"])
                return
            if path == "/manifest":
                models = sorted(
                    (
                        {"name": p.stem, "file": p.name}
                        for p in serve_dir.iterdir()
                        if p.is_file() and p.suffix.lower() == ".stl"
                    ),
                    key=lambda m: m["name"],
                )
                self._send(200, json.dumps({"models": models}), CONTENT_TYPES[".json"])
                return
            if path in ("/callouts", "/callouts.json"):
                # Agent-authored callouts. The agent writes this file directly;
                # return an empty record (not 404) until it exists so the poll
                # loop in the viewer stays quiet.
                if callouts_json.is_file():
                    self._send(200, callouts_json.read_bytes(), CONTENT_TYPES[".json"])
                else:
                    self._send(200, json.dumps({"annotations": []}), CONTENT_TYPES[".json"])
                return

            target = self._safe_path(path)
            if target is None or not target.is_file():
                self._send(404, "not found: %s" % path)
                return
            ctype = CONTENT_TYPES.get(target.suffix.lower(), "application/octet-stream")
            self._send(200, target.read_bytes(), ctype)

        def do_POST(self):  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path != "/submit":
                self._send(404, "not found: %s" % path)
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
            except (TypeError, ValueError):
                length = 0
            raw = self.rfile.read(length) if length else b""
            try:
                data = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as exc:
                self._send(400, json.dumps({"ok": False, "error": "invalid JSON: %s" % exc}),
                           CONTENT_TYPES[".json"])
                return
            if not isinstance(data, list):
                self._send(400, json.dumps({"ok": False, "error": "body must be a JSON array"}),
                           CONTENT_TYPES[".json"])
                return

            ts = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
            record = {"submitted_at": ts, "count": len(data), "annotations": data}

            try:
                comments_json.write_text(json.dumps(record, indent=2) + "\n")
                with comments_log.open("a") as fh:
                    fh.write(json.dumps(record) + "\n")
            except OSError as exc:
                self._send(500, json.dumps({"ok": False, "error": "write failed: %s" % exc}),
                           CONTENT_TYPES[".json"])
                return

            self._print_summary(record, comments_json)
            self._send(200, json.dumps({"ok": True, "count": len(data),
                                        "path": str(comments_json)}),
                       CONTENT_TYPES[".json"])

        def _print_summary(self, record, comments_path):
            line = "=" * 64
            out = sys.stdout
            out.write("\n%s\n" % line)
            out.write("ANNEALAGE MESH COMMENTS SUBMITTED  %s  (%d pins)\n"
                      % (record["submitted_at"], record["count"]))
            out.write("wrote: %s\n" % comments_path)
            out.write("%s\n" % line)
            for a in record["annotations"]:
                n = a.get("id", "?")
                part = a.get("part", "?")
                label = a.get("label", "?")
                p = a.get("point", [None, None, None])
                try:
                    loc = "[% .1f, % .1f, % .1f]" % (p[0], p[1], p[2])
                except (TypeError, IndexError):
                    loc = str(p)
                comment = (a.get("comment") or "").strip() or "(no comment)"
                out.write("#%s  %s  %s  @ %s mm\n" % (n, part, label, loc))
                out.write("     %s\n" % comment)
            out.write("%s\n\n" % line)
            out.flush()

    return Handler


def port_in_use(port, host="0.0.0.0"):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
        except OSError:
            return True
    return False


def create_server(serve_dir, host="0.0.0.0", port=8765):
    """Build (but do not start) a ThreadingHTTPServer bound to serve_dir."""
    handler_cls = make_handler(serve_dir)
    return ThreadingHTTPServer((host, port), handler_cls)


def comments_path(serve_dir):
    return Path(serve_dir).resolve() / COMMENTS_JSON_NAME


def comments_log_path(serve_dir):
    return Path(serve_dir).resolve() / COMMENTS_LOG_NAME


def callouts_path(serve_dir):
    return Path(serve_dir).resolve() / CALLOUTS_JSON_NAME
