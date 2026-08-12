---
name: annealage-mesh
description: Use when reviewing or iterating on a 3D-print or CAD model (STL files) and you want the human to point at a specific face/location and give located feedback rather than describing it in words - e.g. "let's review this print", "get feedback on the model", "which face did you mean". Also use when you (the agent) want to call out a design concern at a specific 3D location for the human to see and confirm.
---

# Annealage Mesh — located pin-comment review for STL models

`annealage-mesh` serves a local three.js viewer over a directory of STL files. The human clicks points on the model to drop pin-comments; you (the agent) can also drop "callouts" at specific coordinates that show up as pins the human sees live, in the same viewer. Comments and callouts are exchanged as JSON files in the served directory, so no realtime channel between you and the browser is needed.

## Starting the server

Run it in the background against the directory holding the STL(s) you want reviewed (e.g. a `build/` output directory):

```
uvx --from git+https://github.com/Annealage/mesh annealage-mesh <dir> --no-open &
```

or, if the package is already installed (or once it's on PyPI, `uvx annealage-mesh <dir>`):

```
annealage-mesh <dir> --no-open &
```

Useful flags: `--port PORT` (default 8765), `--host HOST` (default `0.0.0.0`), `--no-open` (skip auto-opening a browser — usually correct when launched from an agent session). Then tell the human the URL printed on startup (e.g. `http://localhost:8765/`) and what to do: switch to "Add pin" mode, click points on the model, type comments, hit Submit.

## Reading human comments

The human's submitted pins are written to `<dir>/mesh-comments.json` (and appended, one line per submission, to `<dir>/mesh-comments.log`):

```json
{
  "submitted_at": "2026-07-23T10:15:00+00:00",
  "count": 1,
  "annotations": [
    {
      "id": 1,
      "part": "shroud",
      "label": "+Z",
      "point": [12.4, -3.1, 8.0],
      "normal": [0.0, 0.0, 1.0],
      "faceIndex": 1523,
      "comment": "this wall looks too thin"
    }
  ]
}
```

- `part` — name of the STL (from the auto-discovered manifest) the pin landed on.
- `label` — dominant axis of the face normal at the pin (`+X`/`-X`/`+Y`/`-Y`/`+Z`/`-Z`).
- `point` — pin location in model coordinates (same units the STL was authored in), so you can map it straight back to the CAD/script that made it.
- `normal` — face normal at the pin.
- `comment` — free text from the human.

Re-read this file after asking the human to submit feedback.

## Writing agent callouts

To point the human at a specific location (e.g. after you change geometry, or to ask "does this fillet look right?"), write `<dir>/mesh-callouts.json`:

```json
{
  "annotations": [
    {
      "id": 1,
      "author": "agent",
      "part": "shroud",
      "label": "+Z",
      "point": [12.4, -3.1, 8.0],
      "comment": "widened this wall 1.2mm -> 2mm, please confirm clearance"
    }
  ]
}
```

The viewer polls this file (~1.5s) and renders these as cyan pins, distinct from the human's orange pins, without a page reload. `point` and `comment` are the load-bearing fields; the rest are display niceties. Rewrite the whole file each time — the server doesn't merge/append it.

## Installing this skill

Copy `skill/annealage-mesh/` into `~/.claude/skills/` (or a plugin's skills directory) so Claude auto-loads it. The skill only needs `annealage-mesh` reachable on `PATH` or via `uvx annealage-mesh`.

## Notes

- The server auto-discovers `*.stl` in `<dir>` on each `/manifest` request, so you don't need to restart it after regenerating STLs — just tell the human to refresh.
- Coordinates are whatever units/axes the STL was authored in; there is no conversion. If the model is Y-up, tell the human to use the viewer's "Z-up / Y-up" toggle.
- One pure-Python runtime dependency (microdot), so `uvx annealage-mesh` starts near-instantly.
