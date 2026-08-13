"""What ``annealage-mesh doctor`` prints and what ``GET /settings`` returns
under its own ``diagnostics`` key: one collector, read by both, because the
settings window's Diagnostics block exists to duplicate the terminal
command for a person with no terminal, and a second function computing the
same facts a second way would give the two a chance to disagree.

``collect`` never raises. Every fact it gathers comes from a lookup that is
routinely absent or broken on someone else's machine: no ``git`` on PATH, a
platform whose ``claude-agent-sdk`` wheel carries no bundled binary and
whose PATH has no ``claude`` either, a ``.mesh/lock`` left behind by a crash
mid write. A doctor command that raises on the question "what is wrong with
this machine" has failed at its only job, so each such lookup becomes a
value describing what happened (``None``, a ``"source": "missing"``, a
version string that says a subprocess would not run) rather than an
exception a caller has to catch.

``claude_cli`` distinguishes a bundled binary from one found on PATH rather
than reporting a bare path, because which of the two is in play is a real
platform difference: most wheels bundle ``claude`` under
``claude_agent_sdk/_bundled``, but a platform with no published wheel falls
back to the SDK's dependency-free sdist, which bundles nothing, so
``claude`` then has to be installed and reachable on PATH like any other
tool. A human debugging "the agent will not start" needs to know which of
those two is true before anything else.

The bundled binary is located with ``importlib.util.find_spec`` rather than
``import claude_agent_sdk``: locating a package's install directory this
way runs no code in it. The ``sandbox`` field cannot avoid the real import,
because ``session.sdk`` is where ``SANDBOX_DEPENDENCIES`` and
``missing_sandbox_dependencies`` live and that module's own top-level
``from claude_agent_sdk import (...)`` pulls in the SDK as a side effect of
merely importing it, so the import is written inside ``collect`` rather
than at this module's top level. That import still happens every time
``collect`` actually runs; what it does not do is happen merely because
something imported this module, which matters for a viewer-only doctor
invocation and is what lets a test of that property import this module with
no SDK installed at all.

The lock record is read without ever calling ``lock.acquire``: acquiring an
unheld lock creates one, and a diagnostics report must never have that side
effect. What is read is also deliberately incomplete. ``.mesh/lock`` holds
the live per-run token guarding this project's agent-mode instance, and that
token is discarded rather than surfaced here, whether it names this
process's own run or, if a different pid holds the lock, someone else's: a
diagnostics report is not a channel for handing back a still-valid
credential.

Two lookups reach past ``net.py``'s and ``lock.py``'s public surface into a
private helper each: ``net._reachable_addresses`` for the addresses this
machine answers on, and ``lock._read_record`` plus ``lock._pid_is_live`` for
a read-only look at the lock. Both modules already compute exactly this
correctly, the second with a liveness rule (an ``EPERM`` on ``os.kill``
counts as alive) that is easy to get subtly wrong a second time, and neither
currently exposes a public, side-effect-free way to ask the question this
module needs answered. Reimplementing either would be the second copy of a
fact one function already gets right.
"""

import importlib.util
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

from . import __version__, lock, net, paths, sessions, settings

# Generous for a "--version" query and nothing more: a tool that cannot
# answer this within five seconds is not going to answer it, and doctor
# must return promptly even when a binary on PATH is hung or waiting on
# input it will never receive.
_VERSION_TIMEOUT_S = 5

_VERSION_RE = re.compile(r"\d+(?:\.\d+){1,3}")


def collect(
    project_dir=None,
    *,
    session_id=None,
    bind=None,
    port=None,
    run=subprocess.run,
    which=shutil.which,
):
    """Gather diagnostic facts, JSON-able, about this machine and, when
    ``project_dir`` is given, this project.

    ``session_id``, ``bind`` and ``port`` are carried through unchanged
    rather than resolved here: a caller with a running server already knows
    its own resolved bind and current session as plain values, and asking
    this function to interpret them would make it a second place deciding
    what a bind or a session id means. A standalone ``doctor`` invocation
    with no running server passes none of the three, and the fields are
    ``None``.

    ``run`` and ``which`` are the same seam ``net.py`` and ``project.py``
    use for their own subprocess calls, so a test can pin exactly what
    "git is missing" or "claude hangs" looks like without touching a real
    binary.
    """
    return {
        "python": {
            "version": platform.python_version(),
            "executable": sys.executable,
        },
        "mesh_version": __version__,
        "claude_cli": _claude_cli_info(run=run, which=which),
        "git": _detect_git(run=run, which=which),
        "sandbox": _sandbox_info(),
        "project_root": (
            str(paths.resolve_serve_dir(project_dir)) if project_dir is not None else None
        ),
        "session_id": session_id,
        "bind": bind,
        "port": port,
        "reachable": [
            {"interface": name, "address": address}
            for name, address in net._reachable_addresses(run=run)
        ],
        "lock": _lock_info(project_dir),
        "settings_files": _settings_files(project_dir),
    }


def _safe_which(which, name):
    """``which(name)``, or ``None`` if the lookup itself misbehaves.

    ``shutil.which`` is well-behaved in practice; an injected test double
    standing in for it is not guaranteed to be, and a doctor command must
    not crash on a fake any more than on the real thing.
    """
    try:
        return which(name)
    except Exception:
        return None


def _bundled_claude_path():
    """The path to the SDK's own bundled ``claude`` binary, if this
    platform's installed wheel carries one.

    ``importlib.util.find_spec`` locates the installed ``claude_agent_sdk``
    package's directory without executing its ``__init__.py``, so asking
    this question costs nothing beyond a filesystem lookup and never forces
    the import ``_sandbox_info`` defers.
    """
    try:
        spec = importlib.util.find_spec("claude_agent_sdk")
    except Exception:
        return None
    if spec is None or not spec.submodule_search_locations:
        return None
    cli_name = "claude.exe" if platform.system() == "Windows" else "claude"
    for location in spec.submodule_search_locations:
        candidate = Path(location) / "_bundled" / cli_name
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def _claude_cli_info(*, run, which):
    """``{"path", "version", "source"}`` for the ``claude`` binary the SDK
    would actually run, ``source`` one of ``"bundled"``, ``"path"`` or
    ``"missing"``.

    The bundled binary is checked first because that is the SDK's own
    resolution order (its transport looks for the bundled copy before
    falling back to ``PATH``), so this reports whichever one it would
    actually spawn rather than a copy that happens to also exist.
    """
    bundled = _bundled_claude_path()
    if bundled is not None:
        path = str(bundled)
        return {"path": path, "version": _tool_version(path, run=run), "source": "bundled"}
    found = _safe_which(which, "claude")
    if found:
        return {"path": found, "version": _tool_version(found, run=run), "source": "path"}
    return {"path": None, "version": None, "source": "missing"}


def _detect_git(*, run, which):
    """``{"path", "version"}``, or ``None`` when ``git`` is not on ``PATH``."""
    path = _safe_which(which, "git")
    if not path:
        return None
    return {"path": path, "version": _tool_version(path, run=run)}


def _tool_version(path, *, run, args=("--version",)):
    """Run ``path`` with ``args`` and report a version string that never
    raises.

    ``path`` only ever reaches here as a hit from ``which`` or a bundled
    binary this process already found on disk, so what remains to go wrong
    is the subprocess itself: it may not spawn, may exit non-zero, or may
    not return within the timeout, and each of those is exactly as ordinary
    as the binary behaving correctly, so the returned string names what
    happened instead of propagating it.
    """
    try:
        result = run(
            [path, *args], capture_output=True, text=True, timeout=_VERSION_TIMEOUT_S, check=False
        )
    except subprocess.TimeoutExpired:
        return "unknown (timed out after %ss)" % _VERSION_TIMEOUT_S
    except Exception as exc:
        return "unknown (%s: %s)" % (type(exc).__name__, exc)

    try:
        returncode = getattr(result, "returncode", None)
        stdout = getattr(result, "stdout", "") or ""
        stderr = getattr(result, "stderr", "") or ""
    except Exception as exc:
        return "unknown (%s: %s)" % (type(exc).__name__, exc)

    if returncode not in (0, None):
        text = (stdout or stderr).strip()
        return "unknown (exit %s%s)" % (returncode, ": " + text if text else "")
    return _parse_version(stdout or stderr) or "unknown (no version in output)"


def _parse_version(text):
    """Pull the first dotted version number out of a ``--version`` banner
    (``"git version 2.43.0"`` becomes ``"2.43.0"``), falling back to the
    first line verbatim when nothing matches that shape."""
    match = _VERSION_RE.search(text or "")
    if match:
        return match.group(0)
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return None


def _sandbox_info():
    """``{"dependencies", "missing"}`` from ``session.sdk``'s own view of
    the sandbox's external requirements.

    Imported here rather than at this module's top level: ``session.sdk``'s
    own top-level import pulls in ``claude_agent_sdk``, and a caller that
    only wants the python, git or claude_cli facts, or a test proving this
    module carries no SDK dependency of its own, must not pay for that
    import merely by importing this one.
    """
    from .session import sdk

    return {
        "dependencies": list(sdk.SANDBOX_DEPENDENCIES),
        "missing": list(sdk.missing_sandbox_dependencies()),
    }


def _lock_info(project_dir):
    """The current state of ``.mesh/lock`` for ``project_dir``, or ``None``
    when no lock file exists, read without ever calling ``lock.acquire``
    (which would create one where none is held).

    A corrupt or unreadable record is reported by ``lock.LockCorrupt``'s own
    message rather than left to raise: the same condition stops
    ``lock.acquire`` from starting a second instance, and a human looking at
    doctor's output wants to know a record is unreadable for the same
    reason ``acquire`` refuses to guess at it.
    """
    if project_dir is None:
        return None
    path = lock.lock_path(sessions.mesh_dir(project_dir))
    try:
        pid, held_port, _token = lock._read_record(path)
    except FileNotFoundError:
        return None
    except lock.LockCorrupt as exc:
        return {"path": str(path), "corrupt": True, "error": str(exc)}
    return {
        "path": str(path),
        "corrupt": False,
        "pid": pid,
        "port": held_port,
        "alive": lock._pid_is_live(pid),
    }


def _safe_is_file(path):
    try:
        return path.is_file()
    except OSError:
        return False


def _settings_files(project_dir):
    """``{"user", "user_present", "project", "project_present"}``, the two
    config files ``settings.resolve`` reads, so the settings window can
    show a human exactly which files are backing what they see.

    The user file's path never depends on ``project_dir``, so it is always
    reported; the project file only exists relative to a project, so its
    entry is ``None``/``False`` when ``collect`` was called with none.
    """
    user_path = settings.user_settings_path()
    info = {
        "user": str(user_path),
        "user_present": _safe_is_file(user_path),
        "project": None,
        "project_present": False,
    }
    if project_dir is not None:
        project_path = settings.project_config_path(project_dir)
        info["project"] = str(project_path)
        info["project_present"] = _safe_is_file(project_path)
    return info
