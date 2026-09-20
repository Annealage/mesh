# Phase 7 correction: stop reconfiguring tools that were already correct

Date: 2026-09-20
Supersedes the credential-scheme parts of `20260920_phase7-progress.md`
(that document's description of the `integration` marker, per-backend test
shape, and adversarial-review discipline is still accurate; its
`MESH_LIVE_*` required-credential design and "no live credentials exist in
this environment" framing are not).

## What went wrong

Phase 7's original live-test design invented a `MESH_LIVE_*`-prefixed
required-credential scheme (`MESH_LIVE_ANTHROPIC_API_KEY`,
`MESH_LIVE_OPENAI_API_KEY`, a from-scratch `CODEX_HOME` isolated per test
with its own throwaway API-key login, `MESH_LIVE_OMP_BASE_URL`/
`MESH_LIVE_OMP_EXECUTABLE`/`MESH_LIVE_OMP_API_KEY`) without ever checking
whether this development host already had `claude`, `codex`, and `omp`
installed and authenticated. It did - `~/.claude/.credentials.json` (OAuth),
`~/.codex/auth.json` (OAuth), and a working `omp` on `PATH` were all
present and already configured for exactly the three models the user had
named (haiku, gpt-5.6-luna, titan-hosted qwen3.8-27b). The final summary
delivered at the end of that turn stated "No live credentials exist in this
environment" - a claim that was never verified, just assumed, and was
false.

The user corrected this directly, in three escalating messages: first
pointing out the tools were already configured, then explicitly stating no
API-key requirement had been requested, and finally - after the
orchestrator started inspecting billing/provider-config files to work out
how to plumb the invented scheme through anyway - stopping that
entirely: "If you've built the interfaces wrong to require re-configuring
of them in mesh rather than just using the tools as pre-configured on this
host, then you need to fix the interfaces."

That last framing was the actual finding. It was not only a test-harness
problem. `session/omp.py`'s `OmpSession.start()` hard-failed with
`ValueError("local_base_url is not configured...")` whenever
`local_base_url` was unset, and unconditionally synthesized its own
throwaway custom-provider config (a `models.yml`, a `PI_CODING_AGENT_DIR`
override, a `mesh-local/<id>` model prefix) even for a provider `omp`
already knew about by name. There was no way to tell `OmpSession` "just use
`omp --model titan/qwen3.8-27b` exactly as a human would type it" - the
production interface, not merely the test, forced reconfiguration.

`CodexSession` and `SdkSession`, by contrast, turned out to already be
correct: neither overrides `CODEX_HOME`/`env`/`ANTHROPIC_API_KEY` anywhere
in production code (confirmed by reading both files' `start()` methods in
full), so both already authenticate through whatever account the real
`claude`/`codex` CLI is logged into on the host, with zero mesh-side
reconfiguration. The bug in those two files' live tests was purely a wrong
assumption made while writing the tests, not a production defect.

## The fix

`session/omp.py`:
- `local_base_url` is now genuinely optional. Set: unchanged behavior -
  synthesizes the throwaway `mesh-local` custom-provider config, same as
  before this fix. Unset: no config synthesis, no `PI_CODING_AGENT_DIR`
  override (confirmed safe: `omp_rpc.RpcClient.start()` merges `env` onto
  `os.environ` rather than replacing it), `model` passed straight through
  to `omp`'s own `--model` flag exactly as given (e.g.
  `"titan/qwen3.8-27b"`), using whatever providers and credentials the
  host's `omp` is already configured with.
- `local_api_key` set without `local_base_url` now fails closed with a
  clear message before any subprocess is constructed: it only means
  something alongside a synthesized provider, and an already-known
  provider carries its own credentials.
- `set_model` now splits an incoming `"provider/model"` string for the
  no-`local_base_url` case, since `RpcClient.set_model(provider, model_id)`
  has no fuzzy/provider-omitted form the way the CLI `--model` flag does
  (confirmed by reading `omp_rpc/client.py`'s `_build_command` alongside
  `set_model` itself); a bare model id with no `local_base_url` raises a
  clear error rather than guessing which provider it belongs to.

`settings.py`/`cli.py`: the `backend`/`local_base_url`/`local_api_key`/
`model` setting descriptions and the `doctor` report's local-endpoint line
updated to describe `local_base_url` as optional - using omp's own
already-configured providers is the normal case, not an error state to
correct.

The three live test files were rewritten to match: no credential env var of
any kind is required or read; each skips only if its own CLI binary is
missing from `PATH` (`shutil.which`), and otherwise constructs the real
session class with zero reconfiguration - the omp test specifically
exercises the new `base_url=None` pass-through path with
`"titan/qwen3.8-27b"`. `.github/workflows/integration.yml` was simplified
to match: no `MESH_LIVE_*` secrets to map, just a documented expectation
that whatever runner this points at already has all three CLIs installed
and authenticated (a GitHub-hosted `ubuntu-latest` runner does not, so
every test skips cleanly there today - a real runner is a human
prerequisite this file does not fabricate).

`tests/test_omp_session.py`'s obsolete `test_missing_base_url_fails_without_launching_omp`
(which pinned the old hard-fail behavior) was replaced with five tests
covering both `start()` branches, the `api_key`-without-`base_url`
validation, and both `set_model` branches.

## What is verified now, for real

This is the first point in this rollout where a live pass/fail against a
real backend was actually obtained, not just designed for:

- **Claude/haiku: passed for real.** A real `claude` subprocess started,
  authenticated via this host's existing OAuth login, and replied "PONG"
  to the live prompt.
- **Codex/gpt-5.6-luna: real connectivity and protocol proven, blocked by
  a real account limit.** `CodexSession` connected to a real
  `codex app-server`, authenticated via this host's existing account,
  submitted a real turn, and received a real response - which was itself
  an error: `"You've hit your usage limit... try again at 2:17 PM."`
  (`code=usage_limit_reached`). This is exactly the fix working correctly
  - the whole point of the `local_base_url` interface fix and the
  reconfiguration-free test design was to prove the real plumbing, and it
  did: a real account state was surfaced through a real error event, not
  faked or worked around. That this session's independent `reviewer`
  subagent (also Codex-backed) hit the identical `usage_limit_reached`
  error minutes later, independently, corroborates that this is a real,
  current, account-wide constraint - not a mesh defect, and not something
  this fix should route around (e.g. by silently catching and hiding
  quota errors, which would defeat the entire purpose of this test tier).
- **omp/titan-qwen3.8-27b: not exercised, for an environmental reason
  outside this repository.** The `omp` binary was present, executable, and
  version-checkable (`omp/18.1.22`) earlier in this same session. By the
  time the live test ran, `~/.local/bin/omp`'s symlink target
  (`~/cc-pi-bridge/trial-omp/marketplace/plugins/claude-net-omp/bin/omp`)
  had been removed from the host entirely - confirmed by a direct retry
  showing the whole `bin/` directory gone, not merely one file. This is a
  host-environment change during the session, not a mesh code path this
  fix could have prevented; the test's own skip condition
  (`shutil.which("omp")`) fired correctly and reported the honest reason
  rather than erroring.

Full default suite: `pytest tests/ --ignore=tests/test_viewer_e2e.py -q` -
**1066 passed, 3 deselected** (up from 1062: net +4 from `session/omp.py`'s
new test coverage). `ruff check`/`ruff format --check` clean on every file
touched by this correction.

## Lesson, stated plainly

Two mistakes compounded: inventing a credential-plumbing requirement
nobody asked for, and asserting an environment fact ("no live credentials
exist here") without checking it. The second is the more important one to
name - a claim about the environment must be verified by actually looking,
not inferred from what would be convenient or cautious to assume. The
user's own three corrections trace the right escalation: first the factual
correction, then the scope correction ("you don't need to require api key
args"), then the root-cause correction ("fix the interfaces"). Each was
necessary because the prior one was answered too narrowly.
