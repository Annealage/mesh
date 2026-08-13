"""Command-line entry point for Annealage Mesh.

Four invocations, one of which is bare:

    annealage-mesh [DIR]        scaffold if needed, then the viewer with a chat
                                 pane driving an agent in DIR
    annealage-mesh view [DIR]   the viewer alone: no agent, no scaffold, no git,
                                 no lock, and the agent SDK is never imported
    annealage-mesh init [DIR]   scaffold plus git, idempotent, no server
    annealage-mesh doctor [DIR] what this machine has and what this project
                                 looks like, then exit

Bare invocation is agent mode because that is what the tool is for. The
exception is an environment that already has an agent in it: when ``CLAUDECODE``
is set, the default flips to the viewer and says so, so a skill telling Claude
Code to run this tool never starts a second agent inside the first. ``view``
pins that explicitly and is what the published skill passes.

A subcommand is recognised only when the first argument matches one of the
three names exactly, so a directory of models that happens to be called
``doctor`` is still servable as ``./doctor``.
"""

import argparse
import asyncio
import os
import socket
import sys
import webbrowser
from pathlib import Path

from . import __version__, diagnostics, lock, net, paths, sessions
from . import app as app_module
from . import project as project_module
from . import settings as settings_module
from .http.routes_viewer import VIEWER_HTML

DEFAULT_PORT = 8765

#: Subcommand names, recognised only as an exact first argument.
SUBCOMMANDS = ("view", "init", "doctor")

#: Environment variable Claude Code sets in its own shells. Its presence means
#: this invocation is already inside an agent session, so agent mode would nest
#: one agent inside another.
NESTED_AGENT_ENV = "CLAUDECODE"

# ``-r``/``--resume`` given with no id lists sessions and exits, rather than
# resuming anything (plan section 3.4: that would duplicate ``-c`` and blur
# the two flags). A private sentinel, not a string, so no session id
# (``sessions.new_session_id``'s format, or any other value a future
# ``-r`` accepts) could ever collide with "the flag was given bare".
_RESUME_BARE = object()


def _add_dir(ap):
    ap.add_argument(
        "dir",
        nargs="?",
        default=".",
        help="directory of STL files to serve (default: current directory)",
    )


def build_parser():
    """The parser for the two server modes, agent and viewer.

    Every settings-backed flag defaults to ``None`` (or, for ``--no-open``, to
    the absence of the flag) so that "not given" is distinguishable from "given
    the value that happens to be the default". Without that distinction a
    project or user setting could never outrank a default, which is the whole
    point of the layering.
    """
    ap = argparse.ArgumentParser(
        prog="annealage-mesh",
        description="Build 3D-printable parts with an agent: a local 3D viewer beside a chat pane.",
        epilog="Subcommands: view (viewer only), init (scaffold a project), "
        "doctor (report what this machine has). A directory named like a "
        "subcommand is still servable as ./view, ./init or ./doctor. "
        "-r/--resume takes its SID from the next token unconditionally, "
        'so "annealage-mesh -r mydir" reads mydir as SID, not as the '
        "directory to serve. Put DIR before -r/-c, or spell the id as "
        "--resume=SID.",
    )
    _add_dir(ap)
    ap.add_argument(
        "--port",
        type=int,
        default=None,
        help="TCP port (default: %d, or whatever your settings say)" % DEFAULT_PORT,
    )
    ap.add_argument(
        "--host",
        default=None,
        help="bind address: an IP, a resolvable name, 0.0.0.0 for every "
        'interface, or the alias "tailscale" to bind this host\'s '
        "tailnet address (default: 127.0.0.1, this machine only)",
    )
    ap.add_argument(
        "--origin",
        action="append",
        default=[],
        metavar="ORIGIN",
        help="an additional allowed browser Origin, repeatable; needed "
        'when a reverse proxy or "tailscale serve" fronts this '
        "server under another name or scheme",
    )
    ap.add_argument(
        "--token", default=None, help="use this per-run token instead of a generated one"
    )
    ap.add_argument(
        "--no-open", action="store_true", help="do not try to open a browser automatically"
    )
    ap.add_argument(
        "--model",
        default=None,
        help="model for the agent to use (default: whatever the claude CLI is configured for)",
    )
    ap.add_argument(
        "--effort",
        default=None,
        choices=("low", "medium", "high", "xhigh", "max"),
        help="how much thinking the agent does per turn (default: "
        "whatever the claude CLI is configured for)",
    )
    ap.add_argument(
        "--permission-mode",
        default=None,
        choices=("default", "acceptEdits", "plan"),
        help="permission mode for the agent. bypassPermissions is "
        "deliberately not offered: it is never persistable and "
        "not selectable here",
    )
    ap.add_argument(
        "--no-agent",
        action="store_true",
        help="viewer only: the same thing the view subcommand does",
    )
    ap.add_argument(
        "--no-git",
        action="store_true",
        help="do not run git init, and do not make the scaffold commit",
    )
    ap.add_argument(
        "--settings",
        action="store_true",
        help="print every setting with the layer it came from, then exit",
    )
    ap.add_argument(
        "--trust-project-config",
        action="store_true",
        help="accept the Claude configuration in this directory "
        "(.claude/, .mcp.json), which can declare shell commands "
        "the agent's CLI runs. Recorded per directory against the "
        "exact content present, so a later change asks again",
    )
    session_group = ap.add_mutually_exclusive_group()
    session_group.add_argument(
        "-c",
        "--continue",
        dest="continue_",
        action="store_true",
        help="continue the most recent session for this project; takes no "
        "argument, Mesh resolves which session itself",
    )
    session_group.add_argument(
        "-r",
        "--resume",
        dest="resume",
        nargs="?",
        const=_RESUME_BARE,
        default=None,
        metavar="SID",
        help="resume session SID; given with no SID, list this project's sessions and exit",
    )
    ap.add_argument("--version", action="version", version="annealage-mesh %s" % __version__)
    return ap


def build_init_parser():
    ap = argparse.ArgumentParser(
        prog="annealage-mesh init",
        description="Create the directories and files a mesh project uses, and "
        "start a git repository if git is installed. Idempotent: "
        "anything already there is left alone.",
    )
    _add_dir(ap)
    ap.add_argument(
        "--no-git",
        action="store_true",
        help="do not run git init, and do not make the scaffold commit",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="rewrite the generated .gitignore and CLAUDE.md, which "
        "are otherwise kept exactly as they are",
    )
    return ap


def build_doctor_parser():
    ap = argparse.ArgumentParser(
        prog="annealage-mesh doctor",
        description="Report what this machine has, what this project looks like, "
        "and which settings files are in play. Starts no server.",
    )
    _add_dir(ap)
    return ap


def _format_session_line(info):
    cost = "$%.2f" % info.cost_usd
    snippet = info.first_user_text or "(no turn yet)"
    return "  %s  %s  %d turn%s  %s  %s" % (
        info.session_id,
        info.started_at or "unknown start",
        info.turn_count,
        "" if info.turn_count == 1 else "s",
        cost,
        snippet,
    )


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


def flags_from(args):
    """The settings flags this invocation actually gave, as ``{name: value}``.

    Only keys whose flag was present appear, which is what lets a project or
    user file outrank a built-in default while a flag still outranks both.
    ``--no-open`` is the one negative form, and its absence is not a request
    for a browser, only the absence of a request against one.
    """
    flags = {}
    for name, value in (
        ("host", args.host),
        ("port", args.port),
        ("model", getattr(args, "model", None)),
        ("effort", getattr(args, "effort", None)),
        ("permission_mode", getattr(args, "permission_mode", None)),
    ):
        if value is not None:
            flags[name] = value
    if getattr(args, "no_open", False):
        flags["open_browser"] = False
    return flags


def settings_report(resolved, serve_dir):
    """Lines describing every setting, its value, and where it came from.

    Named files rather than layer names alone, because "from user settings" is
    only actionable once you know which file that is; the point of reporting
    provenance at all is that someone can go and change it.
    """
    origins = {
        settings_module.FLAG: "command-line flag (this run only)",
        settings_module.PROJECT: "project config: %s"
        % settings_module.project_config_path(serve_dir),
        settings_module.USER: "user settings: %s" % settings_module.user_settings_path(),
        settings_module.DEFAULT: "built-in default",
    }
    width = max(len(key.name) for key in settings_module.SETTING_KEYS)
    lines = ["settings for %s" % serve_dir]
    for key in settings_module.SETTING_KEYS:
        value = resolved[key.name]
        shown = "(unset)" if value is None else repr(value)
        lines.append(
            "  %-*s  %-22s %s" % (width, key.name, shown, origins[resolved.provenance(key.name)])
        )
    return lines


def scaffold_report(result, project_dir):
    """Lines describing what a scaffold did, or nothing if it did nothing.

    Silent when a project was already complete, because the common case is
    every run after the first and a list of things that did not happen is
    noise. Git's outcome is always reported when it has one to report: a
    skipped commit is a thing someone needs to know about their own repository.
    """
    lines = []
    for label, names in (("created", result.created), ("regenerated", result.regenerated)):
        if names:
            lines.append("  %s: %s" % (label, ", ".join(names)))
    git = result.git
    if git is not None:
        if git.initialised and git.committed:
            lines.append("  git: repository created and the scaffold committed")
        elif git.initialised:
            lines.append(
                "  git: repository created, nothing committed (%s)"
                % (git.skipped_reason or "no reason given")
            )
        elif git.skipped_reason:
            lines.append("  git: %s" % git.skipped_reason)
    if lines:
        lines.insert(0, "project %s" % project_dir)
    return lines


def diagnostics_report(facts):
    """Lines for ``doctor``, in the order someone debugging reads them."""
    lines = ["Annealage Mesh %s" % facts["mesh_version"]]
    python = facts["python"]
    lines.append("  python           : %s  (%s)" % (python["version"], python["executable"]))

    cli = facts["claude_cli"]
    if cli["source"] == "missing":
        lines.append(
            "  claude CLI       : NOT FOUND. Agent mode cannot run; "
            "install the agent SDK or put claude on PATH"
        )
    else:
        lines.append(
            "  claude CLI       : %s  (%s, %s)"
            % (
                cli["version"] or "version not reported",
                cli["path"],
                "bundled with the SDK" if cli["source"] == "bundled" else "found on PATH",
            )
        )

    git = facts["git"]
    lines.append(
        "  git              : %s"
        % (
            "%s  (%s)" % (git["version"] or "version not reported", git["path"])
            if git
            else "not installed; a project will not be a repository"
        )
    )

    sandbox = facts["sandbox"]
    if not sandbox["dependencies"]:
        lines.append("  sandbox          : provided by this platform, nothing to install")
    elif sandbox["missing"]:
        lines.append(
            "  sandbox          : MISSING %s. Agent mode refuses to start "
            "without them; the viewer alone needs neither" % ", ".join(sandbox["missing"])
        )
    else:
        lines.append("  sandbox          : %s present" % ", ".join(sandbox["dependencies"]))

    lines.append("  project          : %s" % facts["project_root"])
    files = facts["settings_files"]
    lines.append(
        "  user settings    : %s%s"
        % (files["user"], "" if files["user_present"] else "  (not written yet)")
    )
    lines.append(
        "  project config   : %s%s"
        % (files["project"], "" if files["project_present"] else "  (not written yet)")
    )

    held = facts["lock"]
    if held is None:
        lines.append("  lock             : free")
    elif held.get("error"):
        lines.append(
            "  lock             : unreadable (%s); remove %s by hand once "
            "you have checked nothing is running" % (held["error"], held.get("path"))
        )
    else:
        lines.append(
            "  lock             : held by pid %s on port %s (%s)"
            % (
                held.get("pid"),
                held.get("port"),
                "running" if held.get("live") else "stale, will be reclaimed",
            )
        )

    reachable = facts["reachable"]
    if reachable:
        lines.append(
            "  addresses        : %s"
            % ", ".join("%s on %s" % (entry["address"], entry["interface"]) for entry in reachable)
        )
    return lines


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


def _resolved_dir(raw):
    """``(path, None)`` for a servable directory, or ``(None, message)``."""
    resolved = Path(raw).resolve()
    if not resolved.is_dir():
        return None, "directory does not exist: %s" % resolved
    return resolved, None


def init_command(argv):
    """``annealage-mesh init``: scaffold, git, and nothing else."""
    args = build_init_parser().parse_args(argv)
    serve_dir, message = _resolved_dir(args.dir)
    if serve_dir is None:
        sys.stderr.write("error: %s\n" % message)
        return 2
    result = project_module.ensure_project(serve_dir, git=not args.no_git, force=args.force)
    lines = scaffold_report(result, serve_dir)
    if not lines:
        sys.stdout.write("project %s is already set up; nothing to do\n" % serve_dir)
    else:
        sys.stdout.write("\n".join(lines) + "\n")
    for name in result.kept:
        sys.stdout.write("  kept as it is: %s\n" % name)
    return 0


def doctor_command(argv):
    """``annealage-mesh doctor``: report and exit, taking no lock and no port."""
    args = build_doctor_parser().parse_args(argv)
    serve_dir, message = _resolved_dir(args.dir)
    if serve_dir is None:
        sys.stderr.write("error: %s\n" % message)
        return 2
    facts = diagnostics.collect(serve_dir)
    sys.stdout.write("\n".join(diagnostics_report(facts)) + "\n")
    return 0


def _split_command(argv):
    """``(command, rest)``, where ``command`` is None for the bare form."""
    if argv and argv[0] in SUBCOMMANDS:
        return argv[0], argv[1:]
    return None, argv


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    command, rest = _split_command(argv)
    if command == "init":
        return init_command(rest)
    if command == "doctor":
        return doctor_command(rest)

    args = build_parser().parse_args(rest)

    serve_dir, message = _resolved_dir(args.dir)
    if serve_dir is None:
        sys.stderr.write("error: %s\n" % message)
        return 2
    if not VIEWER_HTML.exists():
        sys.stderr.write("error: packaged viewer.html missing: %s\n" % VIEWER_HTML)
        return 2

    # Viewer-only when asked for, and also when this process is already inside
    # an agent's own shell: a skill that runs this tool would otherwise start a
    # second agent inside the first, each with its own approval flow.
    nested = command is None and not args.no_agent and os.environ.get(NESTED_AGENT_ENV)
    viewer_only = command == "view" or args.no_agent or bool(nested)
    mode = "viewer" if viewer_only else "agent"

    agent_only = [
        name
        for name, given in (
            ("-c/--continue", args.continue_),
            ("-r/--resume", args.resume is not None),
            ("--model", args.model is not None),
            ("--effort", args.effort is not None),
            ("--permission-mode", args.permission_mode is not None),
            ("--trust-project-config", args.trust_project_config),
        )
        if given
    ]
    if viewer_only and agent_only and not nested:
        sys.stderr.write(
            "error: %s %s the agent, and this run has none (%s)\n"
            % (
                ", ".join(agent_only),
                "configure" if len(agent_only) > 1 else "configures",
                "view" if command == "view" else "--no-agent",
            )
        )
        return 2

    try:
        resolved_settings = settings_module.resolve(serve_dir, flags=flags_from(args))
    except settings_module.SettingsError as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 2

    if args.settings:
        sys.stdout.write("\n".join(settings_report(resolved_settings, serve_dir)) + "\n")
        return 0

    port = resolved_settings["port"]

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
        bind = net.resolve_bind(resolved_settings["host"])
    except net.BindError as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 2

    if nested:
        sys.stdout.write(
            "note: %s is set, so this is the viewer alone with no agent, to "
            'avoid starting one inside another. Pass "view" to say so '
            "explicitly, or unset %s to run the agent here.\n"
            % (NESTED_AGENT_ENV, NESTED_AGENT_ENV)
        )

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
                "annealage-mesh view\n"
                % (
                    " and ".join(sdk_requirements.SANDBOX_DEPENDENCIES),
                    ", ".join(missing),
                    sdk_requirements.SANDBOX_PACKAGES,
                )
            )
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
                sys.stderr.write(
                    workspace_trust.refusal_message(serve_dir, workspace_trust.present(serve_dir))
                )
                return 2

        if args.continue_:
            mesh_sid = sessions.resolve_continue(serve_dir)
            if mesh_sid is None:
                sys.stderr.write(
                    "error: no prior session for %s; run without -c to start one\n" % serve_dir
                )
                return 1
            resumed = True
        elif args.resume is not None:
            if sessions.get_session_info(serve_dir, args.resume) is None:
                sys.stderr.write(
                    "error: session %r is not known to %s\n" % (args.resume, serve_dir)
                )
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
            held_lock = lock.acquire(sessions.mesh_dir(serve_dir), port, token)
        except lock.LockHeld as exc:
            sys.stderr.write(
                "error: %s\n  it is serving: %s\n"
                % (exc, net.viewer_url(bind, exc.port, exc.token))
            )
            return 3
        except lock.LockCorrupt as exc:
            sys.stderr.write(
                "error: %s\n  remove it by hand once you have checked nothing is "
                "actually running, then retry\n" % exc
            )
            return 1

        # Scaffolded while the lock is held, so two starts against one project
        # cannot both be writing these files, and after the trust gate above,
        # because git reads configuration out of this very directory and a
        # hostile .git/config can name commands git will run.
        scaffold = project_module.ensure_project(serve_dir, git=not args.no_git)

        if mesh_sid is None:
            mesh_sid = sessions.create_session(serve_dir)
        sessions.record_last_session(serve_dir, mesh_sid)
    else:
        scaffold = None
        token = net.generate_token(args.token)

    if port_in_use(port, bind.address):
        sys.stderr.write(
            "error: port %d is already in use on %s, stop whatever is using it, "
            "or pass --port.\n" % (port, bind.address)
        )
        if held_lock is not None:
            held_lock.release()
        return 1

    open_url = net.viewer_url(bind, port, token)

    # Held so on_ready can report the posture the session actually got, rather
    # than the one that was asked for. A list with one slot because the factory
    # below runs inside create_app, after this closure is defined.
    built_session = []

    def build_session(on_event, *, bus):
        """Construct the agent session, or None for viewer-only mode.

        Called by ``create_app`` with the callback that appends an event to the
        log and broadcasts it, plus the ``ViewerBus`` the mesh tools drive the
        browser through, which is why this is a factory: the session must not
        exist before either of the things it uses.

        Imported here rather than at module scope so that viewer-only mode, and
        anything that only wants the CLI's argument parsing, never pays for
        importing the SDK.
        """
        if mode != "agent":
            return None
        from .session.permissions import PermissionBroker
        from .session.sdk import SdkSession
        from .tools.registry import MeshTools

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
            # The mesh tool server. The tools that never prompt are already in
            # the session's own allow list, so nothing further is passed for
            # them; the write-class ones are absent from every allow list, which
            # is what makes them reach the broker above and therefore the human.
            # The session id goes with it for the one tool that writes the
            # conversation out.
            mcp_servers=MeshTools(bus, serve_dir, mesh_sid).mcp_servers,
            model=resolved_settings["model"],
            effort=resolved_settings["effort"],
            permission_mode=resolved_settings["permission_mode"],
            # The SDK resumes only a conversation it already knows; a
            # freshly created mesh session has no SDK id to resume yet.
            resume=_resumable_sdk_id(serve_dir, mesh_sid) if resumed else None,
            on_sdk_session_id=lambda sdk_id: sessions.set_sdk_session_id(
                serve_dir, mesh_sid, sdk_id
            ),
            # What the gate above accepted, so the session can refuse tool
            # calls if it stops being true while the run is in progress.
            trusted_config_digest=trusted_digest,
        )
        built_session.append(session)
        return session

    async def on_ready():
        sys.stdout.write("Annealage Mesh\n")
        sys.stdout.write("  serving STLs from : %s\n" % serve_dir)
        sys.stdout.write(
            "  human comments    : %s  (written on submit)\n" % paths.comments_path(serve_dir)
        )
        sys.stdout.write("  comments log      : %s\n" % paths.comments_log_path(serve_dir))
        sys.stdout.write(
            "  agent callouts    : %s  (agent writes here to show pins)\n"
            % paths.callouts_path(serve_dir)
        )
        if mode == "agent":
            sys.stdout.write(
                "  session           : %s%s\n"
                % (mesh_sid, "  (resumed)" if resumed else "  (fresh)")
            )
        if scaffold is not None:
            for line in scaffold_report(scaffold, serve_dir):
                sys.stdout.write(line + "\n")
        # The exposure banner prints every run, whatever the bind, because a
        # default or a persisted setting means the user never typed a flag
        # that would have reminded them what this is reachable on.
        sys.stdout.write("%s\n" % net.format_banner(bind, port, token))
        # The session, when one was built, so this reports the posture that is
        # actually in effect: a requested sandbox that could not engage says so
        # here, naming what is missing, rather than leaving the human to infer
        # it from a sudden increase in approval prompts.
        for line in describe_agent_posture(mode, built_session[0] if built_session else None):
            sys.stdout.write(line + "\n")
        sys.stdout.write("  (Ctrl-C to stop)\n\n")
        sys.stdout.flush()
        if resolved_settings["open_browser"]:
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
        asyncio.run(
            app_module.run(
                serve_dir,
                bind.address,
                port,
                on_ready=on_ready,
                token=token,
                extra_origins=tuple(args.origin),
                mesh_session_id=mesh_sid,
                build_session=build_session,
                settings=resolved_settings,
            )
        )
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
