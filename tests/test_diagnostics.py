"""Tests for ``diagnostics.py``, the one collector behind both ``doctor``
and ``GET /settings``'s ``diagnostics`` key.

Two properties matter more than the field list, so most tests are organised
around them rather than one test per key. First, ``collect`` never raises:
every external lookup it makes (a subprocess, a lock file, a config file) is
routinely absent or broken on someone else's machine, and each such failure
has to land as a reported value rather than an exception. Second, the
``claude`` CLI's source is determined by actually looking at the installed
package's directory, not assumed from whether the SDK is installed at all,
because a platform with no published wheel falls back to an sdist that
bundles no binary (plan section 3.12), and which of the two is in play is
exactly what a human debugging a broken agent needs told to them.

The real environment this suite runs in has an actual bundled ``claude``
binary (``claude-agent-sdk`` is a base dependency), so every test that is
not specifically about bundled-versus-PATH detection monkeypatches
``diagnostics._bundled_claude_path`` to ``None``, otherwise the real bundled
binary would silently win over whatever ``which``/``run`` fakes a test
supplies and the test would not be exercising the branch it claims to.
"""

import builtins
import errno
import json
import os
import platform
import subprocess
import sys

from annealage_mesh import diagnostics, lock, settings


class _Completed:
    """Just enough of ``subprocess.CompletedProcess`` for a stubbed ``run``."""

    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class _FakeSpec:
    """Just enough of an ``importlib`` module spec for ``_bundled_claude_path``
    to walk: the list of directories the installed package lives in."""

    def __init__(self, locations):
        self.submodule_search_locations = locations


def _dispatch(table):
    """A ``run(argv, ...)`` stub that answers only the exact argv tuples in
    ``table`` and raises on anything else, so a subprocess call this test
    did not expect is a test failure rather than a silently accepted
    default."""

    def run(argv, **kwargs):
        key = tuple(argv)
        if key not in table:
            raise AssertionError("unstubbed command: %r" % (argv,))
        result = table[key]
        if isinstance(result, Exception):
            raise result
        return result

    return run


def _run_raises(exc):
    def run(argv, **kwargs):
        raise exc

    return run


def _forbidden_which(name):
    raise AssertionError("which(%r) should not have been called" % name)


def _no_binaries(name):
    return None


_IP_JSON_TWO_INTERFACES = """[
  {"ifname": "lo", "addr_info": [{"family": "inet", "local": "127.0.0.1"}]},
  {"ifname": "eth0", "addr_info": [{"family": "inet", "local": "192.168.1.50"}]}
]"""


# --- collect() never raises, whatever is missing -------------------------


def test_collect_reports_every_absent_subprocess_without_raising(monkeypatch, tmp_path):
    """git, claude and the interface-probe binary are all absent; each
    becomes ``None``/``"missing"``/an empty list rather than propagating."""
    monkeypatch.setattr(diagnostics, "_bundled_claude_path", lambda: None)
    run = _run_raises(FileNotFoundError(errno.ENOENT, "No such file or directory", "ip"))

    result = diagnostics.collect(tmp_path, run=run, which=_no_binaries)

    assert result["git"] is None
    assert result["claude_cli"] == {"path": None, "version": None, "source": "missing"}
    assert result["reachable"] == []


def test_collect_reports_every_present_subprocess_with_parsed_versions(monkeypatch, tmp_path):
    """git, claude (found on PATH) and the interface probe are all present
    and answer normally; each fact is parsed out of realistic banner text."""
    monkeypatch.setattr(diagnostics, "_bundled_claude_path", lambda: None)

    def which(name):
        return {"git": "/usr/bin/git", "claude": "/usr/local/bin/claude"}.get(name)

    run = _dispatch(
        {
            ("/usr/bin/git", "--version"): _Completed(stdout="git version 2.43.0\n"),
            ("/usr/local/bin/claude", "--version"): _Completed(stdout="2.1.0 (Claude Code)\n"),
            ("ip", "-json", "addr", "show"): _Completed(stdout=_IP_JSON_TWO_INTERFACES),
        }
    )

    result = diagnostics.collect(tmp_path, run=run, which=which)

    assert result["git"] == {"path": "/usr/bin/git", "version": "2.43.0"}
    assert result["claude_cli"] == {
        "path": "/usr/local/bin/claude",
        "version": "2.1.0",
        "source": "path",
    }
    assert result["reachable"] == [{"interface": "eth0", "address": "192.168.1.50"}]


def test_collect_carries_session_id_bind_and_port_through_unchanged(monkeypatch, tmp_path):
    """A caller with a running server already knows its own resolved bind
    and current session; collect must report those three verbatim rather
    than becoming a second place that interprets what they mean."""
    monkeypatch.setattr(diagnostics, "_bundled_claude_path", lambda: None)
    result = diagnostics.collect(
        tmp_path,
        session_id="sid-abc",
        bind="100.64.1.2",
        port=9001,
        run=_run_raises(FileNotFoundError("no ip binary")),
        which=_no_binaries,
    )
    assert result["session_id"] == "sid-abc"
    assert result["bind"] == "100.64.1.2"
    assert result["port"] == 9001


def test_collect_with_no_project_dir_reports_none_for_project_scoped_fields(monkeypatch):
    """A standalone ``doctor`` invocation with no project passes no
    project-scoped facts, and every one of them is ``None`` rather than a
    lookup against a directory that was never given."""
    monkeypatch.setattr(diagnostics, "_bundled_claude_path", lambda: None)
    result = diagnostics.collect(
        None, run=_run_raises(FileNotFoundError("no ip binary")), which=_no_binaries
    )
    assert result["project_root"] is None
    assert result["session_id"] is None
    assert result["bind"] is None
    assert result["port"] is None
    assert result["lock"] is None
    assert result["settings_files"]["project"] is None
    assert result["settings_files"]["project_present"] is False


def test_collect_payload_round_trips_through_json(monkeypatch, tmp_path):
    """``GET /settings`` ships this dict straight into an HTTP response
    body, so every value inside it has to survive ``json.dumps``/``loads``
    unchanged, not merely avoid raising."""
    monkeypatch.setattr(diagnostics, "_bundled_claude_path", lambda: None)
    mesh_dir = tmp_path / ".mesh"
    mesh_dir.mkdir()
    lock.lock_path(mesh_dir).write_bytes(
        json.dumps({"pid": os.getpid(), "port": 8765, "token": "tok"}).encode()
    )

    result = diagnostics.collect(
        tmp_path,
        session_id="sid-123",
        bind="127.0.0.1",
        port=8765,
        run=_run_raises(FileNotFoundError("no ip binary")),
        which=_no_binaries,
    )

    round_tripped = json.loads(json.dumps(result))
    assert round_tripped == result


# --- tool_version and parse_version: the primitive that must never raise --


def test_tool_version_reports_a_raising_subprocess_instead_of_raising():
    """A binary found on PATH may still fail to spawn (permissions, a
    stale symlink, an exec-format mismatch); the returned string names the
    exception rather than letting it propagate into doctor's caller."""

    def run(argv, **kwargs):
        raise PermissionError("not executable")

    version = diagnostics._tool_version("/usr/bin/git", run=run)
    assert version == "unknown (PermissionError: not executable)"


def test_tool_version_reports_a_timeout_instead_of_hanging():
    """A hung binary must not hang doctor with it; the timeout itself is
    injected rather than produced by an actually slow process."""

    def run(argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=diagnostics._VERSION_TIMEOUT_S)

    version = diagnostics._tool_version("/usr/bin/git", run=run)
    assert version == "unknown (timed out after %ss)" % diagnostics._VERSION_TIMEOUT_S


def test_tool_version_reports_a_nonzero_exit():
    def run(argv, **kwargs):
        return _Completed(stdout="", stderr="git: command not found\n", returncode=127)

    version = diagnostics._tool_version("/usr/bin/git", run=run)
    assert version == "unknown (exit 127: git: command not found)"


def test_parse_version_extracts_a_dotted_number_from_banner_text():
    assert diagnostics._parse_version("git version 2.43.0") == "2.43.0"


def test_parse_version_falls_back_to_the_first_nonblank_line():
    text = "\n  custom-tool build\nsecond line\n"
    assert diagnostics._parse_version(text) == "custom-tool build"


def test_parse_version_returns_none_for_empty_output():
    assert diagnostics._parse_version("") is None


# --- claude CLI source: bundled binary versus PATH fallback --------------


def test_claude_cli_reports_the_bundled_binary_when_the_wheel_carries_one(monkeypatch, tmp_path):
    """Most published wheels bundle a ``claude`` binary; when the installed
    package's own directory holds one, that is the binary the SDK's
    transport actually spawns, so doctor reports it without even
    consulting ``which``, rather than a copy on PATH that happens to also
    exist."""
    sdk_dir = tmp_path / "claude_agent_sdk"
    bundled_dir = sdk_dir / "_bundled"
    bundled_dir.mkdir(parents=True)
    cli_name = "claude.exe" if platform.system() == "Windows" else "claude"
    bundled_path = bundled_dir / cli_name
    bundled_path.write_bytes(b"")

    monkeypatch.setattr(
        diagnostics.importlib.util, "find_spec", lambda name: _FakeSpec([str(sdk_dir)])
    )

    calls = []

    def fake_run(argv, **kwargs):
        calls.append(tuple(argv))
        return _Completed(stdout="2.1.0\n")

    info = diagnostics._claude_cli_info(run=fake_run, which=_forbidden_which)

    assert info == {"path": str(bundled_path), "version": "2.1.0", "source": "bundled"}
    assert calls == [(str(bundled_path), "--version")]


def test_claude_cli_falls_back_to_path_when_the_sdist_carries_no_binary(monkeypatch, tmp_path):
    """A platform with no published wheel falls back to the SDK's
    dependency-free sdist (plan section 3.12), whose installed package
    directory holds no ``_bundled`` folder at all; ``claude`` then has to
    be found on PATH like any other tool."""
    sdk_dir = tmp_path / "claude_agent_sdk"
    sdk_dir.mkdir()

    monkeypatch.setattr(
        diagnostics.importlib.util, "find_spec", lambda name: _FakeSpec([str(sdk_dir)])
    )

    def fake_which(name):
        return "/usr/local/bin/claude" if name == "claude" else None

    def fake_run(argv, **kwargs):
        assert argv == ["/usr/local/bin/claude", "--version"]
        return _Completed(stdout="1.0.5\n")

    info = diagnostics._claude_cli_info(run=fake_run, which=fake_which)
    assert info == {"path": "/usr/local/bin/claude", "version": "1.0.5", "source": "path"}


def test_claude_cli_reports_missing_when_neither_bundled_nor_path_has_it(monkeypatch):
    monkeypatch.setattr(diagnostics.importlib.util, "find_spec", lambda name: None)
    info = diagnostics._claude_cli_info(
        run=_run_raises(AssertionError("run must not be called")), which=_no_binaries
    )
    assert info == {"path": None, "version": None, "source": "missing"}


# --- sandbox facts, from session.sdk, imported lazily ---------------------


def test_sandbox_info_reports_dependencies_and_missing_from_session_sdk(monkeypatch):
    """The dependency list and the missing subset both come from
    ``session.sdk``'s own view of the sandbox's requirements, not a second
    count kept here, so the two can never disagree with it."""
    from annealage_mesh.session import sdk

    monkeypatch.setattr(sdk, "missing_sandbox_dependencies", lambda: ("socat",))

    info = diagnostics._sandbox_info()
    assert info == {"dependencies": list(sdk.SANDBOX_DEPENDENCIES), "missing": ["socat"]}


def test_diagnostics_module_import_never_touches_claude_agent_sdk(monkeypatch):
    """``session.sdk`` is imported inside ``_sandbox_info``, not at this
    module's top level, so importing ``diagnostics.py`` on its own, the way
    a viewer-only ``doctor`` invocation would, never pays for the SDK's
    import. Proved by blocking any attempt to import ``claude_agent_sdk``
    and reloading this module fresh: if a future change moved the import to
    the top level, this test would fail on the blocked import rather than
    silently passing regardless of what the real environment has
    installed."""
    real_import = builtins.__import__

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name.split(".")[0] == "claude_agent_sdk":
            raise AssertionError("importing annealage_mesh.diagnostics pulled in claude_agent_sdk")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    monkeypatch.delitem(sys.modules, "annealage_mesh.diagnostics", raising=False)

    import annealage_mesh.diagnostics as fresh

    assert hasattr(fresh, "collect")


# --- the lock record, read without ever calling lock.acquire --------------


def test_lock_info_is_none_with_no_project_dir():
    assert diagnostics._lock_info(None) is None


def test_lock_info_is_none_when_no_lock_file_exists_and_creates_nothing(tmp_path):
    """``_lock_info`` must never call ``lock.acquire``, which would create a
    lock where none was held; an absent ``.mesh/lock`` has to stay absent
    after a diagnostics report has looked at it."""
    assert diagnostics._lock_info(tmp_path) is None
    assert not (tmp_path / ".mesh").exists()


def test_lock_info_reports_a_live_holder_without_surfacing_its_token(tmp_path):
    """This test's own pid is guaranteed live for its duration, so the
    liveness check needs no mocking. The token guards this project's live
    agent-mode instance, and a diagnostics report is not a channel for
    handing it back, whether it names this process or another one's."""
    mesh_dir = tmp_path / ".mesh"
    mesh_dir.mkdir()
    lock.lock_path(mesh_dir).write_bytes(
        json.dumps({"pid": os.getpid(), "port": 9001, "token": "secret-token"}).encode()
    )

    info = diagnostics._lock_info(tmp_path)
    assert info["corrupt"] is False
    assert info["pid"] == os.getpid()
    assert info["port"] == 9001
    assert info["alive"] is True
    assert "token" not in info


def test_lock_info_reports_a_stale_holder_as_not_alive(tmp_path, monkeypatch):
    """``os.kill`` is monkeypatched for one specific fake pid rather than
    finding a real dead process, so which pid is dead never depends on
    process-table timing; every other pid, including this test's own, still
    goes through the real syscall."""
    dead_pid = 999999
    real_kill = os.kill

    def fake_kill(pid, sig):
        if pid == dead_pid:
            raise OSError(errno.ESRCH, "No such process")
        return real_kill(pid, sig)

    monkeypatch.setattr(lock.os, "kill", fake_kill)

    mesh_dir = tmp_path / ".mesh"
    mesh_dir.mkdir()
    lock.lock_path(mesh_dir).write_bytes(
        json.dumps({"pid": dead_pid, "port": 1, "token": "stale"}).encode()
    )

    info = diagnostics._lock_info(tmp_path)
    assert info["alive"] is False
    assert info["pid"] == dead_pid


def test_lock_info_reports_a_corrupt_record_instead_of_raising(tmp_path):
    """A lock file that exists but will not parse is reported the same way
    ``lock.acquire`` itself refuses to guess at one: as a fact, not a
    crash."""
    mesh_dir = tmp_path / ".mesh"
    mesh_dir.mkdir()
    lock.lock_path(mesh_dir).write_bytes(b"not json at all")

    path = lock.lock_path(mesh_dir)
    info = diagnostics._lock_info(tmp_path)
    assert info["corrupt"] is True
    assert "path" in info
    assert "error" in info
    # Read-only: a diagnostics report never reclaims or rewrites a lock file
    # it cannot parse, even one this obviously broken.
    assert path.read_bytes() == b"not json at all"


# --- settings files: which two configs backed what the window shows ------


def test_settings_files_reports_user_presence_with_no_project_given():
    info = diagnostics._settings_files(None)
    assert info["user"] == str(settings.user_settings_path())
    assert info["user_present"] is False
    assert info["project"] is None
    assert info["project_present"] is False


def test_settings_files_reports_presence_once_the_files_exist(tmp_path):
    user_path = settings.user_settings_path()
    user_path.parent.mkdir(parents=True, exist_ok=True)
    user_path.write_text("# user settings\n", encoding="utf-8")

    project_path = settings.project_config_path(tmp_path)
    project_path.parent.mkdir(parents=True, exist_ok=True)
    project_path.write_text("# project settings\n", encoding="utf-8")

    info = diagnostics._settings_files(tmp_path)
    assert info == {
        "user": str(user_path),
        "user_present": True,
        "project": str(project_path),
        "project_present": True,
    }
