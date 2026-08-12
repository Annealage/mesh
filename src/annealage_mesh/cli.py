"""Command-line entry point for Annealage Mesh."""

import argparse
import asyncio
import socket
import sys
import webbrowser
from pathlib import Path

from . import __version__
from . import app as app_module
from . import lock
from . import net
from . import paths
from . import sessions
from .http.routes_viewer import VIEWER_HTML

DEFAULT_PORT = 8765

# ``-r``/``--resume`` given with no id lists sessions and exits, rather than
# resuming anything (plan section 3.4: that would duplicate ``-c`` and blur
# the two flags). A private sentinel, not a string, so no session id
# (``sessions.new_session_id``'s format, or any other value a future
# ``-r`` accepts) could ever collide with "the flag was given bare".
_RESUME_BARE = object()


def build_parser():
    ap = argparse.ArgumentParser(
        prog="annealage-mesh",
        description="Local web tool for pin-comment review of 3D-print STL models.",
        epilog="-r/--resume takes its SID from the next token unconditionally, so "
               "\"annealage-mesh -r mydir\" reads mydir as SID, not as the directory "
               "to serve. Put DIR before -r/-c, or spell the id as --resume=SID.",
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
    ap.add_argument("--model", default=None,
                     help="model for the agent to use (default: whatever the "
                          "claude CLI is configured for)")
    ap.add_argument("--permission-mode", default=None,
                     choices=("default", "acceptEdits", "plan"),
                     help="permission mode for the agent. bypassPermissions is "
                          "deliberately not offered: it is never persistable and "
                          "not selectable here")
    ap.add_argument("--no-agent", action="store_true",
                     help="viewer only: no agent session, no lock, several may run "
                          "at once against the same directory")
    ap.add_argument("--trust-project-config", action="store_true",
                     help="accept the Claude configuration in this directory "
                          "(.claude/, .mcp.json), which can declare shell commands "
                          "the agent's CLI runs. Recorded per directory against the "
                          "exact content present, so a later change asks again")
    session_group = ap.add_mutually_exclusive_group()
    session_group.add_argument(
        "-c", "--continue", dest="continue_", action="store_true",
        help="continue the most recent session for this project; takes no "
             "argument, Mesh resolves which session itself")
    session_group.add_argument(
        "-r", "--resume", dest="resume", nargs="?", const=_RESUME_BARE, default=None,
        metavar="SID",
        help="resume session SID; given with no SID, list this project's "
             "sessions and exit")
    ap.add_argument("--version", action="version", version="annealage-mesh %s" % __version__)
    return ap


def _format_session_line(info):
    cost = "$%.2f" % info.cost_usd
    snippet = info.first_user_text or "(no turn yet)"
    return "  %s  %s  %d turn%s  %s  %s" % (
        info.session_id, info.started_at or "unknown start", info.turn_count,
        "" if info.turn_count == 1 else "s", cost, snippet)


def _print_session_list(serve_dir):
    infos = sessions.list_sessions(serve_dir)
    if not infos:
        sys.stdout.write("no sessions recorded for %s\n" % serve_dir)
        return
    sys.stdout.write("sessions for %s:\n" % serve_dir)
    for info in infos:
        sys.stdout.write(_format_session_line(info) + "\n")


class _SdkRequirements:
    """Lazy accessor for the sandbox requirement helpers in ``session.sdk``.

    Importing that module pulls in the agent SDK, which a viewer-only run has no
    reason to load, so it is imported on first attribute access rather than at
    the top of this file.
    """

    def __getattr__(self, name):
        from .session import sdk
        return getattr(sdk, name)


sdk_requirements = _SdkRequirements()


def _resumable_sdk_id(serve_dir, mesh_sid):
    """The SDK conversation id recorded for ``mesh_sid``, or None.

    None is an ordinary outcome, not an error: a Mesh session whose client never
    connected has no conversation to resume, and it is still resumable as a Mesh
    session, just as a fresh conversation in the same folder.
    """
    info = sessions.get_session_info(serve_dir, mesh_sid)
    return info.sdk_session_id if info is not None else None


def describe_agent_posture(mode, session):
    """Banner lines describing the effective agent posture, printed every
    run regardless of ``mode``.

    ``mode`` is ``"viewer"`` (no agent, no lock, today's behaviour) or
    ``"agent"``. ``session`` is whatever this run constructed to drive the
    agent, or ``None`` when nothing has been constructed, which in agent mode
    means the factory never ran and the posture is genuinely unknown rather
    than merely unreported.

    ``AgentSession.sandbox_status()`` supplies the answer: ``requested``,
    ``active``, and ``missing``, a tuple of dependency names taken from the
    sandboxed CLI child's own stderr rather than from a guess about which
    binaries happen to be on PATH.

    A human who chose the sandboxed posture and silently got the prompting
    one has been misled, and the only way to avoid that is to say which
    one is in effect on every run, not only when it differs from what was
    requested.
    """
    if mode == "viewer":
        return ["  agent: not running (viewer-only, no lock)"]

    status = session.sandbox_status() if session is not None else None

    if status is None:
        return [
            "  agent posture: sandbox requested (bash contained once active; "
            "edit, write and network always need your approval)",
            "  agent posture: sandbox engagement not yet confirmed in this "
            "build; treat bash as prompting until it is",
        ]
    if status.active:
        return [
            "  agent posture: sandbox ACTIVE - bash runs contained and "
            "unprompted; edit, write and network still need your approval",
        ]
    if status.requested:
        missing = ", ".join(status.missing) if status.missing else "an unreported dependency"
        return [
            "  agent posture: sandbox REQUESTED but INACTIVE (missing: %s) - "
            "bash will prompt for approval like every other write-class "
            "action" % missing,
        ]
    return [
        "  agent posture: sandbox not requested - every write-class action, "
        "including bash, prompts for approval",
    ]


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

    if args.no_agent and (args.continue_ or args.resume is not None):
        sys.stderr.write(
            "error: -c/--continue and -r/--resume resolve an agent session; "
            "--no-agent runs none\n")
        return 2

    # Bare -r lists and exits before anything else opens: it names no
    # session to resume, starts no server, and needs neither a resolved
    # bind nor a free port to answer "what sessions exist here".
    if args.resume is _RESUME_BARE:
        _print_session_list(serve_dir)
        return 0

    # Resolved before the port check, so a --host that cannot be resolved is
    # reported as the naming problem it is rather than as a bind failure on
    # whatever address a fallback would have chosen. resolve_bind raises rather
    # than widening a bind, which is the whole point of the tailscale alias.
    try:
        bind = net.resolve_bind(args.host)
    except net.BindError as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 2

    mode = "viewer" if args.no_agent else "agent"
    mesh_sid = None
    resumed = False
    held_lock = None
    trusted_digest = None

    if mode == "agent":
        # Checked before a session is resolved, a lock taken or a port bound, so
        # a host that cannot sandbox is told once and clearly, rather than
        # starting and printing a posture line that says the agent's shell is
        # unprotected and leaving the human to notice. Viewer-only mode runs no
        # agent and so carries no such requirement, which is why the message
        # points at it.
        missing = sdk_requirements.missing_sandbox_dependencies()
        if missing:
            sys.stderr.write(
                "error: agent mode runs the agent's shell sandboxed, and that needs "
                "%s on this platform.\n"
                "  missing: %s\n"
                "  install: apt install %s   (or your distribution's equivalent)\n"
                "  or run the viewer alone, which needs neither: "
                "annealage-mesh --no-agent\n"
                % (" and ".join(sdk_requirements.SANDBOX_DEPENDENCIES),
                   ", ".join(missing), sdk_requirements.SANDBOX_PACKAGES))
            return 2

        # Before the session opens, because what this gate prevents is a
        # session-start hook running, and by the time a session exists that has
        # already happened. Viewer-only mode starts no agent CLI, so nothing in
        # the directory is ever read as configuration and the gate is moot.
        from .session import workspace_trust
        trusted_digest = workspace_trust.config_digest(serve_dir)
        if trusted_digest != workspace_trust.EMPTY_DIGEST:
            store = workspace_trust.TrustStore()
            if args.trust_project_config:
                store.accept(serve_dir, trusted_digest)
            elif not store.accepted(serve_dir, trusted_digest):
                sys.stderr.write(workspace_trust.refusal_message(
                    serve_dir, workspace_trust.present(serve_dir)))
                return 2

        if args.continue_:
            mesh_sid = sessions.resolve_continue(serve_dir)
            if mesh_sid is None:
                sys.stderr.write(
                    "error: no prior session for %s; run without -c to start one\n"
                    % serve_dir)
                return 1
            resumed = True
        elif args.resume is not None:
            if sessions.get_session_info(serve_dir, args.resume) is None:
                sys.stderr.write(
                    "error: session %r is not known to %s\n" % (args.resume, serve_dir))
                return 1
            mesh_sid = args.resume
            resumed = True

        token = net.generate_token(args.token)
        # Locked before the port check: a live lock names the conflict
        # precisely (this project, this pid, this port), which a bare
        # "address already in use" from the socket layer below cannot,
        # and two SDK clients resuming one session id or two writers on
        # one events.jsonl is corruption (plan section 3.4), so this has
        # no --allow-multiple escape hatch.
        try:
            held_lock = lock.acquire(sessions.mesh_dir(serve_dir), args.port, token)
        except lock.LockHeld as exc:
            sys.stderr.write(
                "error: %s\n  it is serving: %s\n"
                % (exc, net.viewer_url(bind, exc.port, exc.token)))
            return 3
        except lock.LockCorrupt as exc:
            sys.stderr.write(
                "error: %s\n  remove it by hand once you have checked nothing is "
                "actually running, then retry\n" % exc)
            return 1

        if mesh_sid is None:
            mesh_sid = sessions.create_session(serve_dir)
        sessions.record_last_session(serve_dir, mesh_sid)
    else:
        token = net.generate_token(args.token)

    if port_in_use(args.port, bind.address):
        sys.stderr.write(
            "error: port %d is already in use on %s, stop whatever is using it, "
            "or pass --port.\n" % (args.port, bind.address))
        if held_lock is not None:
            held_lock.release()
        return 1

    open_url = net.viewer_url(bind, args.port, token)

    # Held so on_ready can report the posture the session actually got, rather
    # than the one that was asked for. A list with one slot because the factory
    # below runs inside create_app, after this closure is defined.
    built_session = []

    def build_session(on_event):
        """Construct the agent session, or None for viewer-only mode.

        Called by ``create_app`` with the callback that appends an event to the
        log and broadcasts it, which is why this is a factory: the session must
        not exist before the log and registry it publishes through.

        Imported here rather than at module scope so that ``view`` mode, and
        anything that only wants the CLI's argument parsing, never pays for
        importing the SDK.
        """
        if mode != "agent":
            return None
        from .session.permissions import PermissionBroker
        from .session.sdk import SdkSession

        broker = PermissionBroker(
            on_event,
            permissions_path=sessions.mesh_dir(serve_dir) / "permissions.toml",
            viewer_url=open_url,
        )
        session = SdkSession(
            on_event,
            cwd=serve_dir,
            session_id=mesh_sid,
            broker=broker,
            model=args.model,
            permission_mode=args.permission_mode,
            # The SDK resumes only a conversation it already knows; a
            # freshly created mesh session has no SDK id to resume yet.
            resume=_resumable_sdk_id(serve_dir, mesh_sid) if resumed else None,
            on_sdk_session_id=lambda sdk_id: sessions.set_sdk_session_id(
                serve_dir, mesh_sid, sdk_id),
            # What the gate above accepted, so the session can refuse tool
            # calls if it stops being true while the run is in progress.
            trusted_config_digest=trusted_digest,
        )
        built_session.append(session)
        return session

    async def on_ready():
        sys.stdout.write("Annealage Mesh\n")
        sys.stdout.write("  serving STLs from : %s\n" % serve_dir)
        sys.stdout.write("  human comments    : %s  (written on submit)\n"
                          % paths.comments_path(serve_dir))
        sys.stdout.write("  comments log      : %s\n" % paths.comments_log_path(serve_dir))
        sys.stdout.write("  agent callouts    : %s  (agent writes here to show pins)\n"
                          % paths.callouts_path(serve_dir))
        if mode == "agent":
            sys.stdout.write("  session           : %s%s\n"
                              % (mesh_sid, "  (resumed)" if resumed else "  (fresh)"))
        # The exposure banner prints every run, whatever the bind, because a
        # default or a persisted setting means the user never typed a flag
        # that would have reminded them what this is reachable on.
        sys.stdout.write("%s\n" % net.format_banner(bind, args.port, token))
        # The session, when one was built, so this reports the posture that is
        # actually in effect: a requested sandbox that could not engage says so
        # here, naming what is missing, rather than leaving the human to infer
        # it from a sudden increase in approval prompts.
        for line in describe_agent_posture(mode, built_session[0] if built_session else None):
            sys.stdout.write(line + "\n")
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
            token=token, extra_origins=tuple(args.origin), mesh_session_id=mesh_sid,
            build_session=build_session))
    except KeyboardInterrupt:
        pass
    finally:
        # Released on every exit from the server loop, including a signal:
        # a lock still held after this process is gone would refuse every
        # future start against this project with no way to tell the
        # refusal apart from a real conflict.
        if held_lock is not None:
            held_lock.release()
    sys.stdout.write("\nshutting down\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
