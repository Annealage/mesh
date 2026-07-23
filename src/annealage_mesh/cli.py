"""Command-line entry point for Annealage Mesh."""

import argparse
import sys
import webbrowser
from pathlib import Path

from . import __version__
from .server import (
    VIEWER_HTML,
    callouts_path,
    comments_log_path,
    comments_path,
    create_server,
    port_in_use,
)

DEFAULT_PORT = 8765
DEFAULT_HOST = "0.0.0.0"


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
    ap.add_argument("--host", default=DEFAULT_HOST,
                     help="bind address (default: %(default)s)")
    ap.add_argument("--no-open", action="store_true",
                     help="do not try to open a browser automatically")
    ap.add_argument("--version", action="version", version="annealage-mesh %s" % __version__)
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)

    serve_dir = Path(args.dir).resolve()
    if not serve_dir.is_dir():
        sys.stderr.write("error: directory does not exist: %s\n" % serve_dir)
        return 2
    if not VIEWER_HTML.exists():
        sys.stderr.write("error: packaged viewer.html missing: %s\n" % VIEWER_HTML)
        return 2
    if port_in_use(args.port, args.host):
        sys.stderr.write(
            "error: port %d is already in use — stop whatever is using it, "
            "or pass --port.\n" % args.port)
        return 1

    httpd = create_server(serve_dir, host=args.host, port=args.port)
    open_url = "http://localhost:%d/" % args.port

    sys.stdout.write("Annealage Mesh\n")
    sys.stdout.write("  serving STLs from : %s\n" % serve_dir)
    sys.stdout.write("  human comments    : %s  (written on submit)\n" % comments_path(serve_dir))
    sys.stdout.write("  comments log      : %s\n" % comments_log_path(serve_dir))
    sys.stdout.write("  agent callouts    : %s  (agent writes here to show pins)\n"
                      % callouts_path(serve_dir))
    sys.stdout.write("  open              : %s\n" % open_url)
    sys.stdout.write("  (Ctrl-C to stop)\n\n")
    sys.stdout.flush()

    if not args.no_open:
        try:
            webbrowser.open(open_url)
        except Exception:
            pass  # never fail startup just because a browser couldn't be opened

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        sys.stdout.write("\nshutting down\n")
    finally:
        httpd.server_close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
