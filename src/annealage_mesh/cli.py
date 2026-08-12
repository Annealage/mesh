"""Command-line entry point for Annealage Mesh."""

import argparse
import asyncio
import socket
import sys
import webbrowser
from pathlib import Path

from . import __version__
from . import app as app_module
from . import net
from . import paths
from .http.routes_viewer import VIEWER_HTML

DEFAULT_PORT = 8765


def build_parser():
    ap = argparse.ArgumentParser(
        prog="annealage-mesh",
        description="Local web tool for pin-comment review of 3D-print STL models.",
    )
    ap.add_argument(
        "dir",
        nargs="?",
        default=".",
        help="directory of STL files to serve (default: current directory)",
    )
    ap.add_argument("--port", type=int, default=DEFAULT_PORT,
                     help="TCP port (default: %(default)s)")
    ap.add_argument("--host", default=None,
                     help="bind address: an IP, a resolvable name, 0.0.0.0 for every "
                          "interface, or the alias \"tailscale\" to bind this host's "
                          "tailnet address (default: 127.0.0.1, this machine only)")
    ap.add_argument("--origin", action="append", default=[], metavar="ORIGIN",
                     help="an additional allowed browser Origin, repeatable; needed "
                          "when a reverse proxy or \"tailscale serve\" fronts this "
                          "server under another name or scheme")
    ap.add_argument("--token", default=None,
                     help="use this per-run token instead of a generated one")
    ap.add_argument("--no-open", action="store_true",
                     help="do not try to open a browser automatically")
    ap.add_argument("--version", action="version", version="annealage-mesh %s" % __version__)
    return ap


def port_in_use(port, host="0.0.0.0"):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
        except OSError:
            return True
    return False


def main(argv=None):
    args = build_parser().parse_args(argv)

    serve_dir = Path(args.dir).resolve()
    if not serve_dir.is_dir():
        sys.stderr.write("error: directory does not exist: %s\n" % serve_dir)
        return 2
    if not VIEWER_HTML.exists():
        sys.stderr.write("error: packaged viewer.html missing: %s\n" % VIEWER_HTML)
        return 2

    # Resolved before the port check, so a --host that cannot be resolved is
    # reported as the naming problem it is rather than as a bind failure on
    # whatever address a fallback would have chosen. resolve_bind raises rather
    # than widening a bind, which is the whole point of the tailscale alias.
    try:
        bind = net.resolve_bind(args.host)
    except net.BindError as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 2

    if port_in_use(args.port, bind.address):
        sys.stderr.write(
            "error: port %d is already in use on %s, stop whatever is using it, "
            "or pass --port.\n" % (args.port, bind.address))
        return 1

    token = net.generate_token(args.token)
    open_url = net.viewer_url(bind, args.port, token)

    async def on_ready():
        sys.stdout.write("Annealage Mesh\n")
        sys.stdout.write("  serving STLs from : %s\n" % serve_dir)
        sys.stdout.write("  human comments    : %s  (written on submit)\n"
                          % paths.comments_path(serve_dir))
        sys.stdout.write("  comments log      : %s\n" % paths.comments_log_path(serve_dir))
        sys.stdout.write("  agent callouts    : %s  (agent writes here to show pins)\n"
                          % paths.callouts_path(serve_dir))
        # The exposure banner prints every run, whatever the bind, because a
        # default or a persisted setting means the user never typed a flag
        # that would have reminded them what this is reachable on.
        sys.stdout.write("%s\n" % net.format_banner(bind, args.port, token))
        sys.stdout.write("  (Ctrl-C to stop)\n\n")
        sys.stdout.flush()
        if not args.no_open:
            # webbrowser.open can shell out and block on the child process
            # (GenericBrowser.open does Popen(cmd).wait() whenever BROWSER
            # names a command with no "%s" placeholder), so it runs in the
            # default executor rather than inline on the event loop that is
            # meant to already be serving connections by the time this
            # callback fires.
            loop = asyncio.get_running_loop()
            try:
                await loop.run_in_executor(None, webbrowser.open, open_url)
            except Exception:
                pass  # never fail startup just because a browser couldn't be opened

    try:
        asyncio.run(app_module.run(
            serve_dir, bind.address, args.port, on_ready=on_ready,
            token=token, extra_origins=tuple(args.origin)))
    except KeyboardInterrupt:
        pass
    sys.stdout.write("\nshutting down\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
