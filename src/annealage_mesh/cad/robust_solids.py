# /// script
# requires-python = ">=3.10"
# dependencies = ["cadquery>=2.4"]
# ///
"""Drop-in helpers for robust CadQuery/OCCT geometry.

Import next to your model.py::

    from cad.robust_solids import safe_fillet, box_around, assert_valid

These encode the fixes described in the Annealage Mesh skill.  The overriding
idea is that OCCT fails *silently* — an invalid fillet doesn't always raise,
it returns a broken solid that then no-ops a downstream cut and leaves your
part wrong.  So every fragile op is validity-checked.
"""

from __future__ import annotations


def safe_fillet(wp, selector_fn, r):
    """Fillet edges chosen by *selector_fn(wp)* and keep the result only if
    it stays a VALID solid; otherwise return *wp* unchanged.

    *r* may be a single radius or a **descending** list of radii — the
    largest one that yields a valid solid is used.  This lets a corner take
    the biggest clean round its geometry allows instead of silently getting
    none (or getting an invalid solid that no-ops a later cut).

    If every radius in the list is too large you get NO fillet (a sharp
    corner) — usually also wrong.  Always include a radius you know fits.
    """
    radii = r if isinstance(r, (list, tuple)) else [r]
    for rr in radii:
        try:
            cand = selector_fn(wp).fillet(rr)
            if cand.val().isValid():
                return cand
        except Exception:
            pass  # OCCT "command not done" etc. — try the next-smaller radius
    return wp


def box_around(center, half):
    """Return a ``BoxSelector`` centred on *center* ``(x, y, z)`` with
    per-axis half-extents *half* (scalar or ``(hx, hy, hz)``).

    Use to target one specific edge by location when string selectors
    can't express it — string selectors support NO and/or/not keywords::

        part.edges("|Z").edges(box_around((x, y, z), (4, 4, 999))).fillet(r)
    """
    from cadquery.selectors import BoxSelector

    hx, hy, hz = (half, half, half) if isinstance(half, (int, float)) else half
    cx, cy, cz = center
    return BoxSelector((cx - hx, cy - hy, cz - hz), (cx + hx, cy + hy, cz + hz))


def assert_valid(wp, label="solid"):
    """Raise early if the solid is invalid.

    A self-intersecting/invalid solid is the usual root cause of a
    non-watertight STL, and it's far cheaper to catch it here than after
    export.
    """
    if not wp.val().isValid():
        raise ValueError("%s is not a valid solid — a fillet/boolean upstream went wrong" % label)
    return wp
