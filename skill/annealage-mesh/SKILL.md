---
name: annealage-mesh
description: Use when building a 3D-printable part with an agent — the full loop from caliper measurements through parametric CadQuery modeling to print-ready STL. Also use when reviewing or iterating on an existing model (STL files) where the human points at specific faces for located feedback rather than describing them in words. Covers dimension management, parametric scaffold, robust CadQuery/OCCT modeling, agent self-verification (cross-sections, probes, watertight checks), print preparation (orientation, splitting, plate layout), and the bidirectional pin-comment review loop.
---

# Annealage Mesh — agentic parametric CAD for 3D printing

Annealage Mesh is a local 3D viewer + chat pane where an agent writes CAD scripts, regenerated parts appear live, and the human pins feedback on the geometry. The project's `CLAUDE.md` has the layout and helper-script reference; this skill has the workflow and the hard-won OCCT knowledge.

## The pipeline

Every project follows the same five stages. Each builds on the last; skipping one stores up pain for later.

1. **Evidence → dimensions** — measurements off the hardware into `dimensions.json`
2. **Scaffold** — `model.py` reading that file, reference hardware modelled first
3. **Robust solids** — CadQuery geometry with OCCT kernel safety
4. **Verify** — agent self-checks before showing the user
5. **Print prep** — orientation, splitting, watertight export

## Starting the viewer

```
annealage-mesh view <dir> --no-open &
```

Then tell the human the URL printed on startup. Useful flags: `--port PORT`, `--host HOST` (default `127.0.0.1`; pass `tailscale` for the tailnet address), `--no-open`.

## Stage 1: Evidence → dimensions.json

The number set is the foundation. If it's wrong, every downstream part inherits the error and you don't discover it until a print doesn't fit.

### Building the file

Work through the user's photos/measurements systematically:

1. **Record the raw reading** with a clear key and unit (default mm). Keep the exact number — don't pre-round.
2. **Note provenance** — which photo, which feature. A `_notes` block or `"//"` sibling-key convention lets a future reader re-verify without re-measuring.
3. **Separate measured from derived.** Measured values come off the calipers. Derived values (e.g. centre offset = half width + half gap) are computed in the model code, never baked into the JSON.

Group related values so the structure mirrors the object:

```json
{
  "radiator": {
    "ear_height_y": 78.38,
    "full_x": 159.71,
    "corner_cut": 12.0
  },
  "fan": { "nominal": 80.0, "bore_d": 76.5, "screw_pitch": 71.5 },
  "part": { "wall": 2.4, "fit": 0.4 },
  "_notes": {
    "radiator.ear_height_y": "caliper across the ears, photo IMG_0142",
    "radiator.full_x": "end-to-end, photo IMG_0139"
  },
  "_units": "mm"
}
```

### The rule that saves prints

**Never silently change a measured value.** Measured values are ground truth from the hardware. If geometry demands a different number, either the measurement needs re-checking against the photo, or the *derivation* is wrong, or a fit/tolerance value should change — not the measurement. Ask the user before overwriting.

When a measurement is missing, say so explicitly and ask for that specific caliper read rather than guessing.

## Stage 2: Scaffold

### Layout

```
project/
├── dimensions.json       # single source of truth
├── model.py              # PEP-723 CadQuery script → models/*.step + models/*.stl
├── cad/                  # helper scripts (robust_solids, section_probe, etc.)
├── models/               # generated STEP + STL (viewer indexes this)
├── images/               # uploads, sketches, cross-section PNGs
└── CLAUDE.md
```

### CadQuery is the build engine; use PEP-723 inline deps

The model script is self-running via `uv run model.py`. PEP-723 inline deps mean no separate environment setup:

```python
# /// script
# requires-python = ">=3.10"
# dependencies = ["cadquery>=2.4", "trimesh", "numpy"]
# ///
```

Load the number set once at the top:

```python
import json, pathlib

D = json.loads(pathlib.Path("dimensions.json").read_text())
R = D["radiator"]
EH = R["ear_height_y"] / 2  # DERIVED from a measured value — never a bare literal
```

### Pin down the coordinate system

Most "mystery offset" bugs are coordinate-convention mismatches. Decide and document:

- Where the **origin** is (a real datum on the hardware)
- What each **axis** means and its **positive direction**

State it once in a comment header, obey it everywhere.

### Model the reference hardware first

Before any printable part, build the *context* geometry — the heatsink, board, bracket, whatever the part must fit. Two reasons:

1. **Positioning.** You place the new part against real geometry, not against a blank void.
2. **Boolean fit.** `part.cut(reference)` guarantees clearance automatically.

Model the reference from measured values including real features (corner cut-outs, notches — they often open up space you can use). Render it translucent in reviews.

## Stage 3: Robust CadQuery/OCCT solids

OCCT is powerful but brittle in specific, repeatable ways. Import `cad/robust_solids.py`:

```python
from cad.robust_solids import safe_fillet, box_around, assert_valid
```

### Fillets fail silently — the #1 source of pain

A fillet that can't fit does **not** always raise. Sometimes it returns an *invalid* solid, and an invalid solid used as a cut tool silently **no-ops the cut** — leaving your part solid where you expected a cavity. Nothing errors; you just get wrong geometry.

Defenses:

1. **Fillet simple solids BEFORE complex booleans.** Round a plain box, then union/cut it. Rounding edges on a heavily-boolean'd solid is where OCCT chokes.

2. **Validity-check every fillet and fall back.**

   ```python
   part = safe_fillet(part, lambda w: w.edges("|Z"), [16, 12, 8, 5])
   ```

   `safe_fillet` tries each radius largest-first, keeps the first valid result. If all fail, returns the unfilleted solid (a sharp corner — usually also wrong, so include a radius you know fits).

3. **Radius is capped by the narrowest adjacent passage.** A fillet in a 7.6 mm-wide arm can't exceed ~7.6 mm. If you need a bigger turn, you need more room, not a bigger radius.

### Round inside corners by filleting the void

To smooth a concave chamber corner, fillet the **void tool's** reflex edge, then cut it. **Union all void boxes into one cavity before filleting** — independent voids with their own rounds leave hooks at junctions.

### String selectors can't do boolean logic — use BoxSelector

```python
part = part.edges("|Z").edges(box_around((x, y, z), (4, 4, 999))).fillet(r)
```

### clean=False on filleted thin tabs

`union`/`cut` run `clean()` internally. On filleted thin tabs this throws "Courbes non jointives". Pass `clean=False` on those booleans, then a single guarded `clean()` at the end. Exports are correct either way.

### Non-watertight = invalid solid

If an STL has open edges, the root cause is almost always a self-intersecting invalid solid upstream — usually a bad fillet. Fix the geometry:

```python
assert_valid(part, "shroud body")
```

## Stage 4: Verify before showing the user

### Agent-side self-verification

Run `uv run cad/section_probe.py verify models/part.stl` before asking for human review:

```
part.stl: open=0 vol=12340 bodies=1
```

Three checks:

1. **Cross-sections** — slice the mesh on a plane and save a 2D outline PNG. A horizontal (Z) slice is a top view; Y or X shows interior walls. Save to `images/`:

   ```
   uv run cad/section_probe.py section models/part.stl --plane 0 0 1 --offset 10 -o images/section-z10.png
   ```

2. **Point-in-solid probes** — confirm a cavity is hollow or a corner got rounded:

   ```
   uv run cad/section_probe.py probe models/part.stl 10 5 3  0 0 0
   ```

3. **Watertight / volume / body counts** — `open_edges == 0` and expected body count are the green light. A volume that jumped means a boolean silently failed.

Keep a **focused build** that regenerates only the part you're iterating on. The full assembly can take minutes; iterate on one part, verify, and only run the full build occasionally.

### User-side review (the viewer loop)

1. Regenerate the STL → the viewer picks it up automatically.
2. Tell the human what changed and what to look at.
3. Human switches to "Add pin" mode, clicks the model, types a comment, hits Submit.
4. Read `mesh-comments.json` for pin locations + comments.
5. Map pin coordinates from the **export frame** back to **model coordinates** before acting (see the coordinate bridge section below).
6. Revise the model and repeat.

## The coordinate bridge

A pin's coordinates are in the exported STL's frame, which is usually NOT the model frame — the export is centred and dropped onto Z=0, and a split part has halves rotated/offset on a plate. **Invert the export transform** before acting on a pin.

See `cad/pin_to_model.py`:

```python
from cad.pin_to_model import center_drop_shift, invert_top_half, invert_bottom_half, map_pin
```

The recipe:

1. Reproduce the exact transform your export applied (centre-drop, per-half rotation, plate offset).
2. Invert it for the pin's point.
3. Choose which half a pin belongs to from its plate-X sign.

Print the mapped model coordinate and sanity-check it against known feature locations before editing.

## Stage 5: Print prep

### Orient for least overhang

- Stand tall features Z-up when that turns ceilings into vertical walls.
- Put the face whose finish matters least on the bed.
- Round bores print best axis-vertical.

### Split hollow parts into open-face trays

A closed box has an internal roof needing supports. Split through a plane so each half prints as an open-face tray:

- **Register with mating teeth.** Pins on one half, sockets (+0.25 mm clearance) on the other.
- **Keep functional internal geometry** — split around it, don't delete it.
- Only split when the roof genuinely forces heavy supports.

### Lay pieces on one plate

```python
from cad.export_watertight import center_drop, build_split_plate

plate = build_split_plate([bottom_tray, top_tray], gap=10)
```

### Export watertight — never at the cost of the shape

```python
from cad.export_watertight import export_watertight

export_watertight(part, "models/part.stl")
```

Repairs are volume-guarded: trimesh `fill_holes` → pymeshlab close-holes → pymeshfix, each accepted only if it reduces open edges AND preserves volume within 2%. Keep the STEP file as the exact master.

If a part won't go watertight without a big volume change, the real bug is an invalid solid upstream.

## Reading human comments

The human's submitted pins (written on Submit) land in `<dir>/mesh-comments.json`:

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

Re-read this file after asking the human to submit feedback.

## Writing agent callouts

Point the human at a specific location by writing `<dir>/mesh-callouts.json`:

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

The server pushes callouts to the viewer live (cyan pins). Rewrite the whole file each time.

## Installing this skill

Copy `skill/annealage-mesh/` into `~/.claude/skills/` so Claude auto-loads it. The skill only needs `annealage-mesh` reachable on `PATH` or via `uvx annealage-mesh`.

## Notes

- The server watches the directory, so a regenerated STL appears in the open viewer automatically. No restart, no refresh.
- Coordinates are whatever units/axes the STL was authored in; there is no conversion.
- The viewer and the helpers are separate concerns. The viewer watches for STL output; the helpers are CadQuery-specific. If you use a different CAD toolchain, the viewer still works — you just lose the helper scripts.
