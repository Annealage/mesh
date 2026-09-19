# Planning folder index

Operating instructions for this folder. Written so a fresh agent - a new
session, a different machine, a plain workflow subagent with no access to
the `phased-roadmap` skill that produced this folder - can action the
roadmap without re-deriving how it works.

## Document map

Reading order for a fresh agent: this index, then
`20260919_multi-backend-research.md` (the SDK/architecture research the
roadmap rests on), then `roadmap.md` (current phase and its tickets), then
the current phase's tickets under `tickets/`.

- `00_index.md` - this file.
- `20260919_multi-backend-research.md` - research report: what was ruled out
  and why, the `openai-codex` and `omp-rpc` SDK internals the plan depends
  on, source citations. Timestamped and HEAD-stamped.
- `roadmap.md` - the phased roadmap: current state, open-questions table
  (Qk), settled design decisions, six phases each with goal/work
  items/targets/tests/exit criteria/workflow shape, risk register, progress
  tracking. Updated in place as phases complete; never forked.
- `tickets/` - one self-contained file per substantive work item, written
  upfront at peak research knowledge. Each carries file:line anchors, design
  constraints and acceptance criteria so an implementer picks it up and acts,
  rather than re-researching what this folder's research report already
  covers.

## Conventions

- Every document in this folder is stamped with its date and the HEAD short
  SHA (`git rev-parse --short=10 HEAD`) it was written against.
- Tickets carry an immutable `Written:` stamp and an append-only
  `Revalidated:` history - never overwrite the `Written:` line.
- The open-questions table in `roadmap.md` uses `Qk` numbering. A closed
  question gets a dated `DECIDED` note and a pointer to whatever design note
  resolved it; rows are never deleted.
- `roadmap.md` is updated in place, never forked, as phases complete or new
  direction arrives.
- Each executed phase writes its own `YYYYMMDD_<topic>.md` progress report
  back to this folder.

## Phase-entry procedure (do this before authoring a phase's Workflow script)

For each ticket the phase is about to use:

1. **Code drift**: `git log --oneline <ticket's Written SHA>..HEAD` for what
   landed since, and `git diff <ticket's Written SHA>..HEAD -- <the
   ticket's anchored files>` for exactly how its anchors moved. Re-resolve
   every file:line reference in the ticket against the current tree.
2. **Knowledge drift**: read any planning-folder documents dated after the
   ticket's `Written:` stamp, plus any `Qk` rows in `roadmap.md` closed or
   reopened since, plus prior phases' progress reports.
3. **Update the ticket in place**: refresh anchors, adjust scope/approach/
   tests to match reality, resolve or escalate anything the drift
   invalidated, then append a `Revalidated:` stamp line (new date, new HEAD
   SHA, one line on what changed or "no drift"). If the drift is large
   enough to change the ticket's shape entirely, that is a finding for
   `roadmap.md` too - update the phase there, do not silently rewrite the
   ticket underneath it.

Only a ticket whose latest `Revalidated:` SHA equals current HEAD has
actually been revalidated. A ticket that has not been revalidated at the
current HEAD does not feed a workflow.

## Execution model

Each phase's revalidated tickets are the input to a dynamic multi-agent
`Workflow`. Default coding-task model tiering (from the operator's own
`~/.claude/CLAUDE.md`, "Dynamic Workflow" section):

- Implementation (writing/editing code and tests) -> **sonnet**.
- Automated testing (compile, run tests against the implementation) ->
  **haiku**.
- Review, both standard and adversarial -> **opus**.
- Loop: opus findings -> sonnet fixes -> haiku re-test -> opus re-review ->
  repeat until reviews are clean and tests pass.

A ticket's own `## Workflow shape` section overrides this default when
present (e.g. a low-risk additive phase may skip the adversarial pass; see
`roadmap.md`'s Phase 2/5/6 for examples of an explicitly lighter shape).

## Ticket template

```
# <component>: <imperative one-line title>

Phase: N
Depends on: <other tickets / phases / external prerequisites>
Written: <YYYY-MM-DD> at HEAD <short SHA>
Revalidated: <YYYY-MM-DD> at HEAD <short SHA> - <one line: what changed / "no drift">
  (append one line per revalidation; never overwrite the Written stamp)

## Context
## Scope
## Files and anchors
## Design constraints
## Approach sketch
## Acceptance criteria and tests
## Workflow shape
## Open questions
```

See any file under `tickets/` for a filled-in example.
