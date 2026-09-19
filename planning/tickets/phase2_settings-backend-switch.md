# settings.py / cli.py: add the backend switch

Phase: 2
Depends on: none (independent of Phase 1; can land in parallel)
Written: 2026-09-19 at HEAD 58f78db34e

## Context

Every backend selection in this project (Claude/Codex/local) needs one
switch, validated and CLI-flaggable the same way every other agent setting
already is. This ticket ships only the switch and the branch point in
`build_session` - the `codex`/`local` branches stay stubs until Phase 3/4
land, so this ticket has no new runtime capability to verify beyond "the
existing `claude` path is bit-for-bit unchanged."

See `planning/roadmap.md`'s Phase 2 section and
`planning/20260919_multi-backend-research.md`'s "Settled architecture" table.

## Scope

In scope: `backend` setting key, `local_base_url`/`local_api_key` keys,
`--backend` CLI flag and its `agent_only` validation, `build_session`'s
branch structure (stub non-`claude` branches), the Claude-only sandbox
preflight gate, `model` key's description text, `settings.js`'s rendering.

Out of scope: any actual `CodexSession`/`OmpSession` implementation (Phase
3/4); resolving Q4 (exact `local_api_key` wire shape) beyond storing it as an
opaque string - Phase 4 decides how it is actually used.

## Files and anchors

- `settings.py:87-149` - the `Key` dataclass. New keys follow this exact
  shape; no changes to the dataclass itself needed.
- `settings.py:152-244` - `SETTING_KEYS` tuple. Insert `backend` near
  `model`/`effort`/`permission_mode` (lines 183-224), not at the end -
  `settings_report()` and the settings window render in tuple order.
- `settings.py:183-194` - the existing `model` key. Its `description` string
  currently reads "The Claude model the agent session uses. Unset falls back
  to the CLI's own default." - change to backend-generic wording; keep
  `type_name`, `layers=(USER, PROJECT)`, `effect="restart"`, `nullable=True`
  unchanged.
- `cli.py:65-177` - `build_parser()`. Add `--backend` next to the existing
  `--model`/`--effort`/`--permission-mode` flags (their exact argparse
  wiring is in the unread middle of this function - read it before adding,
  do not guess the `add_argument` call shape from other flags' conventions
  alone).
- `cli.py:550-561` - `main()`'s `agent_only` list, the tuple of
  `(flag_name, given_bool)` pairs checked against `viewer_only`. Add
  `("--backend", args.backend is not None)` alongside the existing four.
- `cli.py:622-637` - the `bwrap`/`socat` sandbox preflight, inside `if mode
  == "agent":`. Wrap in `if resolved_settings["backend"] == "claude":` -
  Codex uses its own `Sandbox` enum (Phase 3) and omp has its own posture
  (Phase 4), neither needs `bwrap`/`socat` on the host.
- `cli.py:724-773` - `build_session`. Currently unconditionally imports and
  constructs `SdkSession`. Restructure as a branch on
  `resolved_settings["backend"]`; the `codex`/`local` branches for this
  ticket only need to exist and raise a clear "not yet implemented" (not an
  ambiguous import error) - do not stub in a fake session object that would
  make `describe_agent_posture` (`cli.py:420-467`) or `on_ready` render
  something misleading.
- `static/js/settings.js:32-36` - `SECTIONS`. Add `"backend"` to the
  `"Agent"` section's key list, alongside `"model"`, `"effort"`,
  `"permission_mode"`.
- `static/js/settings.js:50-54` - `CHOICES`. Add
  `backend: ["claude", "codex", "local"]`.

## Design constraints

- `permission_mode` stays `layers=(PROJECT,)`-only per its existing
  docstring reasoning ("Project-scoped only, so a personal default cannot
  silently loosen prompting in every project") - do not widen it while
  touching this area of the file for an unrelated reason.
- `backend` and the two `local_*` keys use `(USER, PROJECT)` layers, matching
  `model`/`effort` - a personal default is fine here since backend choice is
  not a security-loosening axis the way `permission_mode` is.
- Every new/changed `Key` needs a `description` written in the same voice as
  the existing ones (a full sentence, states the effect and any fallback,
  no telegraphic phrasing) - `settings_report()`
  (`cli.py:286-308`) and the settings window both render it verbatim.

## Approach sketch

```python
Key(
    name="backend",
    type_name='"claude" or "codex" or "local"',
    default="claude",
    layers=(USER, PROJECT),
    effect="restart",
    description=(
        "Which agent backend this session uses: claude (claude-agent-sdk, "
        "Claude subscription or API billing), codex (OpenAI's openai-codex "
        "SDK, ChatGPT subscription or API billing), or local (an arbitrary "
        "OpenAI-compatible endpoint via local_base_url)."
    ),
    py_type=str,
    choices=("claude", "codex", "local"),
),
Key(
    name="local_base_url",
    type_name="str or null",
    default=None,
    layers=(USER, PROJECT),
    effect="restart",
    description=(
        "The OpenAI-compatible base URL the local backend talks to. Only "
        "used when backend is local."
    ),
    py_type=str,
    nullable=True,
),
Key(
    name="local_api_key",
    type_name="str or null",
    default=None,
    layers=(USER, PROJECT),
    effect="restart",
    description=(
        "The API key sent to local_base_url, if the endpoint requires one. "
        "Only used when backend is local."
    ),
    py_type=str,
    nullable=True,
),
```

`build_session`:

```python
backend = resolved_settings["backend"]
if backend == "claude":
    from .session.sdk import SdkSession
    session = SdkSession(...)  # unchanged construction
elif backend == "codex":
    raise NotImplementedError("backend=codex is not yet implemented")  # Phase 3
elif backend == "local":
    raise NotImplementedError("backend=local is not yet implemented")  # Phase 4
else:
    raise AssertionError("unreachable: settings.py validates backend's choices")
```

(Exact error surfacing - exception vs. a stderr message plus a clean exit
code, matching this file's existing error-reporting conventions elsewhere in
`main()` - should follow whatever pattern `cli.py`'s other `sys.stderr.write(...);
return 2` sites use, not a raw traceback.)

## Acceptance criteria and tests

- `annealage-mesh --backend claude ...` behaves identically to today's
  default in every existing test.
- `annealage-mesh --backend codex ...` / `--backend local ...` fail with a
  clear, testable message, not a traceback or a silent fallback to Claude.
- `annealage-mesh view --backend codex` (or any `--no-agent`/`view`
  combination) is refused with the same wording pattern the existing
  `agent_only` check already uses for `--model`.
- `settings.py`'s validation test suite covers `backend`'s choices
  (an invalid value like `"gpt4"` is rejected with `SettingsError`).
- Settings window (manual or a `test_routes_viewer.py`/`test_settings_routes.py`-style
  test) shows a `backend` dropdown with exactly the three choices.

## Workflow shape

Mechanical, low-risk, additive: single sonnet implementation pass, haiku test
run, one opus review (not adversarial). No loop expected unless review finds
a real defect.

## Open questions

None new. Q4 (`local_api_key` wire shape) is Phase 4's concern, not this
ticket's - here it is only stored, not consumed.
