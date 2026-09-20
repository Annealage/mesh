"""CAD modeling helpers for Annealage Mesh projects.

These scripts are bundled with the package and scaffolded into new projects
by ``annealage-mesh init``.  They work two ways:

1. **Copied into a project** and imported locally by the agent's model
   script (``from cad.robust_solids import safe_fillet``).  PEP-723 inline
   dependency blocks make each script runnable standalone via ``uv run``.

2. **Imported from the installed package** (``from annealage_mesh.cad
   import robust_solids``) — useful for tests and for MCP tools that want
   to call helper functions directly.  All non-stdlib imports are lazy so
   the package is importable even when CadQuery / trimesh are not installed.
"""

from __future__ import annotations

from importlib.resources import files


def script_source(name: str) -> str:
    """Return the text of a bundled helper script, for scaffolding into a project.

    ``name`` is the filename within the ``cad`` package, e.g.
    ``"robust_solids.py"``.
    """
    return (files(__package__) / name).read_text(encoding="utf-8")


#: The helper scripts ``init`` copies into a new project's ``cad/``
#: directory.  Ordered so that the most-imported (``robust_solids``) comes
#: first — purely cosmetic, since the scaffold writes them all.
HELPER_SCRIPTS = (
    "robust_solids.py",
    "section_probe.py",
    "export_watertight.py",
    "pin_to_model.py",
)
