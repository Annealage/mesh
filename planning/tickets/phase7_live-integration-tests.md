# tests/test_{sdk,codex,omp}_session_live.py: real on-demand integration tests

Phase: 7
Depends on: Phase 3 (`CodexSession`), Phase 4 (`OmpSession`), Phase 5 (live
model selection) - all landed and complete.
Written: 2026-09-20 at HEAD e28f609

## Context

Phase 3's and Phase 4's own tickets each named a "manual integration pass
against a real account/endpoint" as an acceptance criterion. Neither phase
actually performed one before being marked complete - every test added in
this rollout (1062 passing) drives `CodexSession`/`OmpSession`/`SdkSession`
through a hand-written fake (`FakeCodexClient`, `FakeRpcClient`,
`test_sdk_session.py`'s `FakeTransport`) that stands in for the real SDK
object one level below the public wrapper. That is deliberate and correct
for what it proves (protocol-shape and approval-gating correctness against
a scriptable double), but it proves nothing about whether the real
`openai_codex`/`omp_rpc`/`claude_agent_sdk` packages, given mesh's actual
generated config, actually start, authenticate, and complete a turn. This
ticket closes that gap with a real, automated, on-demand ("live") test
tier - not a one-off manual pass that leaves no regression coverage behind.

**Explicit constraint carried over from this whole rollout's own working
rules**: never point a test at the shared harness's own `~/.codex` or
`~/.omp` credentials/installations. Every live test below isolates its own
credential/config directory; see each backend's section.

## Scope

In scope: a new `integration` pytest marker (opt-in, excluded from every
default run); three new test files, one per backend, each constructing the
*real* driver class with no `client_factory`/`transport` override and
submitting one trivial, cheap, text-only prompt; a `workflow_dispatch`-only
CI workflow that can run them for real given repo secrets.

Out of scope: re-testing approval/tool-call gating logic live (already
exhaustively covered by the fake-transport suites); live model-switching to
a second real model (covered by fakes; this tier only proves the *starting*
model produces a real reply); provisioning real credentials or a real
"titan"-hosted endpoint (external prerequisite - this ticket documents
exactly what a human must set, it does not and cannot create it).

## Files and anchors

- `pyproject.toml`'s new `[tool.pytest.ini_options]` (already added ahead
  of this ticket's implementation): `addopts = "-m 'not integration'"`,
  `markers = ["integration: ..."]`. Already verified: existing 1062 tests
  unaffected.
- `session/sdk.py`'s `SdkSession.__init__` (`model=`, `sandbox=`,
  `transport=` - leave `transport` unset for a real subprocess) and
  `submit_turn`.
- `session/codex.py`'s `CodexSession.__init__` (`client_factory=` defaults
  to the real `CodexClient` when omitted - leave it unset) and `.start()`
  (imports `CodexConfig`, `CodexClient` from `openai_codex.client`;
  `ApiKeyLoginAccountParams`, `LoginAccountParams` from
  `openai_codex.generated.v2_all`).
- `session/omp.py`'s `OmpSession.__init__` (`client_factory=` defaults to
  the real `omp_rpc.RpcClient`; `model=`, `base_url=`, `api_key=`) and
  `.start()`, which hardcodes `executable="omp"` with no override
  parameter - PATH is the injection seam (see the omp section below), the
  same seam `tests/test_diagnostics.py` already uses for `_omp_info`.
- `session/permissions.py`'s `PermissionBroker` - construct a real one in
  every live test, the same way `tests/test_codex_session.py`'s
  `_started_session`/`"__default__"` helper does
  (`PermissionBroker(recorder, timeout=2.0, no_viewer_grace=0.05)`),
  never `broker=None`. A trivial text-only prompt never triggers an
  approval, but a live test must never be the one place in this codebase
  that runs a real backend with no broker wired - see
  `planning/20260919_codex-approval-handler-finding.md` for why a nulled
  `approval_handler` is a silent full bypass on the Codex side
  specifically.
- `session/base.py`: `TextDelta(text, viewer=None)`, `TurnEnd(...)`,
  `AgentError(stderr, remediation, viewer=None)`, `AGENT_READY`.
- `.github/workflows/test.yml` - structural reference for a matrix-less,
  single-Python job; the new `integration.yml` workflow does not need the
  3.10/3.11/3.12/3.13 matrix (real backend behavior does not vary by
  interpreter version), one leg on the newest supported Python is enough.

## Env vars (all backends)

Every variable below is read directly by the test file via `os environ`;
none of them are settings.py keys, and this ticket does not add any -
these tests construct the driver classes directly, exactly like the
existing fake-transport tests do, not through `cli.py`'s `build_session`.
`MESH_LIVE_` prefixed rather than reusing bare `ANTHROPIC_API_KEY`/
`OPENAI_API_KEY` deliberately: a developer's shell commonly has those set
for unrelated purposes (this very harness, other tools), and the whole
point of this tier is that enabling it is a deliberate, single-purpose
decision - a bare env var already present for another reason must never
silently turn on live spending. Every test skips (`pytest.mark.skipif` at
collection time, not an error inside the test body) when its own required
variable(s) are unset - the tier stays usable per-backend (a human testing
only Claude locally sets only the Claude variables).

| Backend | Required | Optional (default) |
|---|---|---|
| Claude | `MESH_LIVE_ANTHROPIC_API_KEY` | `MESH_LIVE_CLAUDE_MODEL` (`haiku`) |
| Codex | `MESH_LIVE_OPENAI_API_KEY` | `MESH_LIVE_CODEX_MODEL` (`gpt-5.6-luna`) |
| omp | `MESH_LIVE_OMP_BASE_URL`, `MESH_LIVE_OMP_EXECUTABLE` | `MESH_LIVE_OMP_API_KEY` (unset = keyless endpoint), `MESH_LIVE_OMP_MODEL` (`qwen3.8-27b`) |

**omp model naming - read this before wiring `MESH_LIVE_OMP_MODEL`.**
`OmpSession.start()` (`session/omp.py`) always constructs
`model="%s/%s" % (_PROVIDER_ID, model_id)` where `_PROVIDER_ID` is the
hardcoded string mesh's own custom-provider config uses internally
(confirmed against `tests/test_omp_session.py`:
`fake.kwargs["model"] == "mesh-local/llama3.1:8b"` for
`OmpSession(model="llama3.1:8b", ...)`). The `model=` constructor argument
is therefore always the *bare* model id the endpoint itself understands
(what `MESH_LIVE_OMP_BASE_URL` serves), never a `<provider>/<model>`
string - passing `"titan/qwen3.8-27b"` there would double-prefix into
`"mesh-local/titan/qwen3.8-27b"`, which no real endpoint answers to.
"titan" names which real host serves the model, i.e. it is
`MESH_LIVE_OMP_BASE_URL`'s concern, not `MESH_LIVE_OMP_MODEL`'s - default
`MESH_LIVE_OMP_MODEL` to the bare `"qwen3.8-27b"`.

## Per-backend design

### `tests/test_sdk_session_live.py` (Claude / haiku)

Simplest of the three - `claude_agent_sdk`'s bundled CLI reads
`ANTHROPIC_API_KEY` from its subprocess environment directly for API-key
auth, no separate login step. `tests/conftest.py`'s `isolated_user_config`
autouse fixture already points `XDG_CONFIG_HOME` at a fresh per-test
directory, which is what keeps the workspace-trust store away from the
shared harness's real one; nothing extra is needed for isolation here.

```python
@pytest.mark.integration
@pytest.mark.skipif(
    "MESH_LIVE_ANTHROPIC_API_KEY" not in os.environ,
    reason="set MESH_LIVE_ANTHROPIC_API_KEY to run the live Claude test",
)
@pytest.mark.asyncio
async def test_real_claude_backend_completes_a_turn(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", os.environ["MESH_LIVE_ANTHROPIC_API_KEY"])
    model = os.environ.get("MESH_LIVE_CLAUDE_MODEL", "haiku")
    events = []
    session = SdkSession(
        events.append,
        cwd=str(tmp_path),
        session_id="live-claude",
        broker=PermissionBroker(events.append, timeout=30.0, no_viewer_grace=0.05),
        model=model,
        sandbox=False,  # no bash/tool use in this prompt; do not require
                         # bwrap/socat just to prove a live turn works
    )
    session.on_viewer_presence(1)
    try:
        await session.start()
        assert session.agent_status() == AGENT_READY, _diagnose(events)
        await session.submit_turn([{"type": "text", "text": LIVE_PROMPT}])
        await _wait_for_turn_end(events, timeout=60.0)
    finally:
        await session.close()
    assert "pong" in _reply_text(events).lower(), _diagnose(events)
```

Before writing this file, confirm (read `claude_agent_sdk`'s installed
`_internal/transport/subprocess_cli.py` or equivalent, do not assume) that
the real subprocess transport inherits `os.environ` by default when no
custom env is supplied to `ClaudeAgentOptions`/the transport - if it
instead requires an explicit passthrough, adjust accordingly (this file's
own `set_model`/Q3 finding pattern: verify against source, not the
sketch above).

### `tests/test_codex_session_live.py` (Codex / gpt-5.6-luna)

Needs a real login into an *isolated* `CODEX_HOME` before `CodexSession`
can reach `AGENT_READY` - `CodexSession.start()` calls `account_read()`
and immediately fails closed (`_fail_not_authenticated()`) if no account
is configured; there is no in-band way to log in mid-`start()`. Perform
the login once, as test setup, with a throwaway raw `CodexClient` built
exactly the way `CodexSession.start()` builds its own (same
`CodexConfig(cwd=..., client_name=..., client_title=...)` shape, no MCP
overrides needed since this prompt uses no tools) - **not** by shelling
out to the bundled `codex` binary's `login --with-api-key` (mesh's own
Python-level login call, `account_login_start`, is the supported
programmatic path and stays entirely inside the test process).

`CodexConfig.env` merges onto `os.environ.copy()` (confirmed by reading
`openai_codex/client.py`'s `_build_popen_kwargs`-equivalent: `env =
os.environ.copy(); if self.config.env: env.update(self.config.env)`), and
`CodexSession.start()` constructs `CodexConfig` with no `env=` at all, so
setting `CODEX_HOME` via `monkeypatch.setenv` on the *test process* before
either the throwaway login client or the real `CodexSession` starts is
sufficient - both inherit it through that merge, no `CodexSession`
constructor change needed.

```python
@pytest.mark.integration
@pytest.mark.skipif(
    "MESH_LIVE_OPENAI_API_KEY" not in os.environ,
    reason="set MESH_LIVE_OPENAI_API_KEY to run the live Codex test",
)
@pytest.mark.asyncio
async def test_real_codex_backend_completes_a_turn(tmp_path, monkeypatch):
    codex_home = tmp_path / "codex-home"  # isolated from ~/.codex
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    api_key = os.environ["MESH_LIVE_OPENAI_API_KEY"]
    model = os.environ.get("MESH_LIVE_CODEX_MODEL", "gpt-5.6-luna")

    login_client = CodexClient(
        config=CodexConfig(cwd=str(tmp_path), client_name="annealage_mesh_live_test"),
        approval_handler=None,  # no tool use in a login call; nothing to approve
    )
    await asyncio.get_running_loop().run_in_executor(None, login_client.start)
    try:
        await asyncio.get_running_loop().run_in_executor(None, login_client.initialize)
        response = await asyncio.get_running_loop().run_in_executor(
            None,
            login_client.account_login_start,
            LoginAccountParams(root=ApiKeyLoginAccountParams(type="apiKey", api_key=api_key)),
        )
        assert isinstance(response.root, ApiKeyLoginAccountResponse), response
    finally:
        await asyncio.get_running_loop().run_in_executor(None, login_client.close)

    events = []
    session = CodexSession(
        events.append,
        cwd=str(tmp_path),
        session_id="live-codex",
        broker=PermissionBroker(events.append, timeout=30.0, no_viewer_grace=0.05),
        model=model,
    )
    session.on_viewer_presence(1)
    try:
        await session.start()
        assert session.agent_status() == AGENT_READY, _diagnose(events)
        await session.submit_turn([{"type": "text", "text": LIVE_PROMPT}])
        await _wait_for_turn_end(events, timeout=60.0)
    finally:
        await session.close()
    assert "pong" in _reply_text(events).lower(), _diagnose(events)
```

Confirm `CodexClient`'s real constructor/method names against
`session/codex.py`'s own imports before writing this (`start`,
`initialize`, `account_login_start`, `close` are already used identically
by `CodexSession` itself - mirror it exactly rather than re-deriving).

### `tests/test_omp_session_live.py` (omp / titan-hosted qwen3.8-27b)

`OmpSession.start()` hardcodes `executable="omp"` (no constructor
override), resolved via the launched subprocess's `PATH` - the same seam
`tests/test_diagnostics.py`'s `_omp_info` tests already use (see that
file's own comment on why PATH, not a monkeypatched function, is the real
seam here). Do not add an `executable=` parameter to `OmpSession` for
this; prepend a scratch directory holding a symlink named exactly `omp`
pointing at `MESH_LIVE_OMP_EXECUTABLE`'s target onto `PATH` instead -
zero production-code change, and it keeps this test provably using
whichever binary a human explicitly named, never whatever `omp` a bare
PATH lookup would have found ambiently (this workstation has one at
`~/.local/bin/omp` from the shared harness itself - `MESH_LIVE_OMP_EXECUTABLE`
must be explicit, there is deliberately no bare-`"omp"` fallback here).

`OmpSession` already isolates its own agent/config directory per launch
via `PI_CODING_AGENT_DIR` (Phase 4's design - see `session/omp.py`'s
module docstring) and launches with `--auto-approve --no-tools
--no-extensions`, so no extra isolation work is needed beyond the
executable-resolution seam above.

```python
@pytest.mark.integration
@pytest.mark.skipif(
    "MESH_LIVE_OMP_BASE_URL" not in os.environ or "MESH_LIVE_OMP_EXECUTABLE" not in os.environ,
    reason="set MESH_LIVE_OMP_BASE_URL and MESH_LIVE_OMP_EXECUTABLE to run the live omp test",
)
@pytest.mark.asyncio
async def test_real_omp_backend_completes_a_turn(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "omp").symlink_to(os.environ["MESH_LIVE_OMP_EXECUTABLE"])
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])
    model = os.environ.get("MESH_LIVE_OMP_MODEL", "qwen3.8-27b")  # bare model id -
        # see the "omp model naming" note above; never the "titan/..." form

    events = []
    session = OmpSession(
        events.append,
        cwd=str(tmp_path),
        session_id="live-omp",
        broker=PermissionBroker(events.append, timeout=30.0, no_viewer_grace=0.05),
        model=model,
        base_url=os.environ["MESH_LIVE_OMP_BASE_URL"],
        api_key=os.environ.get("MESH_LIVE_OMP_API_KEY"),
    )
    session.on_viewer_presence(1)
    try:
        await session.start()
        assert session.agent_status() == AGENT_READY, _diagnose(events)
        await session.submit_turn([{"type": "text", "text": LIVE_PROMPT}])
        await _wait_for_turn_end(events, timeout=60.0)
    finally:
        await session.close()
    assert "pong" in _reply_text(events).lower(), _diagnose(events)
```

### Shared helpers (put in a small local module or duplicated 3 ways per
this project's existing convention of not sharing fixtures across
`test_*_session.py` files - check which the fakes already do and match it)

- `LIVE_PROMPT = "Reply with exactly the single word PONG and nothing else, no punctuation."`
- `_wait_for_turn_end(events, timeout)`: poll (short sleep loop or an
  `asyncio.Event` set by a wrapper around `on_event`) until a `TurnEnd` or
  `AgentError` appears in `events`; on `AgentError`, fail immediately with
  its `stderr`/`remediation` rather than waiting out the full timeout.
- `_reply_text(events)`: `"".join(e.text for e in events if isinstance(e, TextDelta))`.
- `_diagnose(events)`: a short string dump of every event's class name and
  key fields, attached to assertion failures so a real failure (bad model
  name, expired key, unreachable endpoint) is diagnosable from CI output
  alone.

## Acceptance criteria and tests

- `pytest -q` (bare, matching CI's default invocation) collects and runs
  exactly the same 1062 tests as before this ticket - the new files are
  present but excluded by `addopts`.
- `pytest -m integration -q`, run in this environment with **no**
  `MESH_LIVE_*` variables set, collects the three new tests and reports
  all three **skipped** (not errored, not silently absent) with their
  stated reasons. This is the one thing this ticket's implementation can
  actually be verified against in this environment - there are no real
  credentials or a real "titan" endpoint available here. Do not attempt to
  fabricate a passing live run.
- A human with real credentials exporting the relevant `MESH_LIVE_*`
  variables and running `pytest -m integration -q` gets a real pass/fail
  against the real backend - documented, not executed, in this ticket's
  own progress report.
- `.github/workflows/integration.yml`: `workflow_dispatch`-only trigger (no
  `push`/`pull_request`/`workflow_call` - this tier must never run
  unattended), one job, installs `--extra codex` and `omp-rpc` the same
  way `test.yml` does, maps repo/environment secrets to the `MESH_LIVE_*`
  variables, runs `uv run --extra dev --extra codex pytest -m integration -q`.
  Provisioning the real secrets and a reachable omp binary/endpoint in
  GitHub's own environment is a manual prerequisite this ticket documents,
  not performs.

## Workflow shape

Implementation on sonnet (the three test files can be built in parallel -
independent files, no shared state beyond the already-landed
`pyproject.toml` marker config), automated verification on haiku/direct
bash (the skip-path run above, plus confirming the existing 1062 are
unaffected), adversarial review on opus specifically checking: the
`integration` marker is genuinely excluded from every default run path
(not just the one this ticket's implementer happened to test); no test
constructs a real driver with `broker=None`; the omp test never falls back
to a bare `"omp"` PATH lookup; the Codex login flow does not leak the API
key into any assertion-failure string or log line; `CODEX_HOME`/`PATH`
isolation actually prevents touching `~/.codex`/`~/.omp` even if the
variables happen to be unset (i.e. the *skip* path, which is what actually
runs in review, must not accidentally touch either). Loop until clean.
