"""Templates for the CAD project scaffold.

``project.py`` calls these to generate the files ``annealage-mesh init``
writes into a new project alongside the existing ``models/``, ``images/``,
``.gitignore`` and ``CLAUDE.md``.
"""

from __future__ import annotations


def dimensions_json_body():
    """The generated ``dimensions.json`` template.

    Starts with placeholder values so ``model.py`` runs without edits.
    The user replaces them with their own measured values.
    """
    import json

    template = {
        "width": 40,
        "depth": 30,
        "height": 20,
        "_notes": {
            "width": "placeholder — replace with your measured value",
            "depth": "placeholder — replace with your measured value",
            "height": "placeholder — replace with your measured value",
        },
        "_units": "mm",
    }
    return json.dumps(template, indent=2) + "\n"


def model_py_body():
    r"""The generated ``model.py`` scaffold.

    A working CadQuery script with PEP-723 inline dependencies that reads
    ``dimensions.json`` and exports STEP + STL to ``models/``.  Run it
    with ``uv run model.py``.
    """
    return '''\
# /// script
# requires-python = ">=3.10"
# dependencies = ["cadquery>=2.4"]
# ///
"""Parametric CAD model.

Run:   uv run model.py
Out:   models/part.step (B-rep master) + models/part.stl (print mesh)

Edit dimensions.json for measurements, this script for geometry.
Import helpers from cad/ for robust fillets and verification.
"""
import json
import pathlib

import cadquery as cq

# ── Dimensions (single source of truth) ──────────────────────────
D = json.loads(pathlib.Path("dimensions.json").read_text())

# ── Coordinate system ────────────────────────────────────────────
# Document your coordinate system here:
#   Origin: centre of the part footprint, Z up
#   X: width  ·  Y: depth  ·  Z: height

# ── Reference hardware (optional) ───────────────────────────────
# Model the thing your part fits against, so boolean cuts guarantee
# clearance.  Render translucent in reviews.
# reference = cq.Workplane("XY").box(...)

# ── Your part ────────────────────────────────────────────────────
part = (
    cq.Workplane("XY")
    .box(D["width"], D["depth"], D["height"])
)

# ── Export ────────────────────────────────────────────────────────
out = pathlib.Path("models")
out.mkdir(exist_ok=True)

name = "part"
cq.exporters.export(part, str(out / (name + ".step")))
cq.exporters.export(part, str(out / (name + ".stl")))
print("Wrote " + str(out / (name + ".step")) + " and " + str(out / (name + ".stl")))
'''


def claude_md_cad_section():
    """The CAD-workflow section appended to the generated ``CLAUDE.md``.

    This is what teaches the agent the pipeline without relying on the
    skill being installed.
    """
    return """\
## CAD workflow

This project uses CadQuery for parametric 3D modeling.  The five-stage \
pipeline:

1. **Dimensions** — `dimensions.json` is the single source of truth for \
all measured values.  Every number the model uses comes from here; never \
hardcode a dimension in the script.  Record provenance (which photo, which \
feature) so a future reader can re-verify without re-measuring.

2. **Model** — `model.py` is a PEP-723 CadQuery script.  Run it with \
`uv run model.py` to regenerate STEP and STL files in `models/`.  The \
viewer picks up changed STLs automatically.  Model the *reference hardware* \
first (the thing your part fits against), then boolean-cut it from your \
part so clearance is guaranteed.

3. **Robust solids** — Import `cad/robust_solids.py` for OCCT kernel \
safety.  `safe_fillet` validity-checks every fillet and falls back; \
`box_around` targets edges by location; `assert_valid` catches invalid \
solids before they silently no-op a downstream cut.  Fillet simple solids \
BEFORE complex booleans.

4. **Verify before showing** — Run `uv run cad/section_probe.py verify \
models/part.stl` to check watertight status, volume, and body count.  Use \
`section` and `probe` subcommands for cross-sections and point-in-solid \
tests.  Save section PNGs to `images/` for evidence.  Check \
`.val().isValid()` after every fillet and boolean.

5. **Print prep** — Orient for least overhang.  Split hollow parts into \
open-face trays with mating teeth.  Export watertight with \
`cad/export_watertight.py` (volume-guarded repair).  Lay pieces on one \
plate with `build_split_plate`.  STEP is the exact master; STL is the \
print mesh.

## Helpers in `cad/`

- `robust_solids.py` — `safe_fillet(wp, selector_fn, radii)`, \
`box_around(center, half)`, `assert_valid(wp, label)`
- `section_probe.py` — `verify`, `section`, `probe` CLI; or import \
`to_trimesh`, `open_edges`, `report`, `probe_line`, `section_png`
- `export_watertight.py` — `export_watertight(obj, path)`, \
`center_drop(wp)`, `build_split_plate(parts, gap)`
- `pin_to_model.py` — `center_drop_shift`, `invert_top_half`, \
`invert_bottom_half`, `map_pin` for viewer↔model coordinate bridge"""
