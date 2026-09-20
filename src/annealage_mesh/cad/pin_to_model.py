# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Map annotation pins from the EXPORTED STL frame back to MODEL coordinates.

A pin the user drops in Annealage Mesh is in the coordinates of the STL
they're looking at — which is almost never the model frame.  Exports are
typically centred in XY and dropped onto Z=0, and a split part has each
half rotated/offset on a plate.  Acting on a raw pin therefore fixes the
wrong feature.  Invert the export transform first.

This module gives the building blocks; you MUST reproduce the exact
transform YOUR export applied.  The layout below matches the common
"split into top/bottom trays on one plate" case — adapt the offsets and
rotations to your own ``main()``.

Recipe:
  1. Rebuild the per-part transform the export used (center-drop,
     per-half rotation, plate offset).
  2. Compute each part's total shift ``T`` and inverse-rotate the pin
     about the same axis.
  3. Choose which part a pin belongs to from its plate-position
     (e.g. sign of plate-X).
"""

from __future__ import annotations


def center_drop_shift(bbox):
    """Shift that centres a part in XY and drops its lowest point to Z=0.

    *bbox*: object with ``xmin``/``xmax``/``ymin``/``ymax``/``zmin``
    (e.g. a CadQuery Solid ``BoundingBox()``).
    """
    return (
        -(bbox.xmin + bbox.xmax) / 2,
        -(bbox.ymin + bbox.ymax) / 2,
        -bbox.zmin,
    )


def invert_top_half(pin, shift):
    """Inverse of: centre-drop, then rotate 180 deg about X (the common
    top-tray transform), then translate by *shift*.

    Returns the model-frame coordinate::

        rot = pin - shift ; model = (rot.x, -rot.y, -rot.z)
    """
    rx = pin[0] - shift[0]
    ry = pin[1] - shift[1]
    rz = pin[2] - shift[2]
    return (rx, -ry, -rz)


def invert_bottom_half(pin, shift):
    """Inverse of a plain centre-drop + translate (no rotation).

    ``model = pin - shift``.
    """
    return (pin[0] - shift[0], pin[1] - shift[1], pin[2] - shift[2])


def build_split_transforms(part_builder, gap=10.0):
    """Reproduce the split-plate layout so pins can be inverted.

    *part_builder(which)* must return the CadQuery Workplane for *which* in
    ``{"bottom", "top"}``, built the SAME way your export does (e.g. the
    top half rotated 180 deg about X).

    Returns ``(Tsh, Bsh)``: the total plate-frame shift for the top and
    bottom halves, to feed ``invert_top_half`` / ``invert_bottom_half``.

    Adapt this if your plate layout differs — the point is to mirror the
    export math exactly.
    """
    import cadquery as cq

    bottom = part_builder("bottom")
    top = part_builder("top")  # already rotated 180 deg about X by the builder
    bs = center_drop_shift(bottom.val().BoundingBox())
    ts = center_drop_shift(top.val().BoundingBox())
    b = bottom.translate(bs)
    t = top.translate(ts)
    bw = b.val().BoundingBox().xlen
    tw = t.val().BoundingBox().xlen
    b = b.translate((-(bw / 2 + gap / 2), 0, 0))
    t = t.translate((tw / 2 + gap / 2, 0, 0))
    comp = cq.Compound.makeCompound([b.val(), t.val()])
    sb = comp.BoundingBox()
    fx = -(sb.xmin + sb.xmax) / 2
    fy = -(sb.ymin + sb.ymax) / 2
    Tsh = (ts[0] + (tw / 2 + gap / 2) + fx, ts[1] + fy, ts[2])
    Bsh = (bs[0] - (bw / 2 + gap / 2) + fx, bs[1] + fy, bs[2])
    return Tsh, Bsh


def map_pin(pin, Tsh, Bsh):
    """Map one plate-frame pin to model coords, choosing the half by
    plate-X sign."""
    if pin[0] > 0:  # top half sits on +X in this layout
        return invert_top_half(pin, Tsh)
    return invert_bottom_half(pin, Bsh)
