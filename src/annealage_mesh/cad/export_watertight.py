# /// script
# requires-python = ">=3.10"
# dependencies = ["cadquery>=2.4", "trimesh", "pymeshfix", "pymeshlab", "numpy"]
# ///
"""Watertight STL export that refuses to damage the shape, plus plate-layout
helpers.

The governing rule: ``clean=False`` booleans leave cosmetic coincident-face
seams that tessellate to a few open edges, and slicers repair those fine —
but aggressive repair tools (pymeshfix especially) will "close" a hollow
part by FILLING its interior, doubling the volume.  So a repair is accepted
ONLY if it both reduces open edges AND preserves volume (<2%); otherwise
the raw, geometrically-correct tessellation is kept.

Keep the STEP file as the exact master; this only affects print meshes.

Standalone usage (not common — usually called from model.py)::

    uv run cad/export_watertight.py  (no CLI; import and call from your model)
"""

from __future__ import annotations


def _open_edges(m):
    from trimesh.grouping import group_rows

    return len(group_rows(m.edges_sorted, require_count=1))


def _vol_ok(cand, v0):
    return cand is not None and (v0 == 0 or abs(abs(cand.volume) - v0) / v0 < 0.02)


def export_watertight(obj, path):
    """Tessellate each solid and write one STL, closing coincident-face seams
    without changing the shape.

    Repairs are volume-guarded: trimesh ``fill_holes`` first, then pymeshlab
    for non-manifold repair, then pymeshfix as a last resort — each accepted
    only if it reduces open edges AND preserves volume within 2%.
    """
    import cadquery as cq
    import numpy as np
    import trimesh

    shape = obj.val() if isinstance(obj, cq.Workplane) else obj
    solids = shape.Solids() or [shape]
    meshes = []
    for sol in solids:
        verts, tris = sol.tessellate(0.01, 0.1)
        v = np.ascontiguousarray([[p.x, p.y, p.z] for p in verts], dtype=np.float64)
        f = np.ascontiguousarray(tris, dtype=np.int32)
        mesh = trimesh.Trimesh(v, f, process=True)
        mesh.merge_vertices()
        if _open_edges(mesh):
            v0 = abs(mesh.volume)
            best = mesh
            # (1) non-destructive: triangulate the open seam loops
            try:
                cand = mesh.copy()
                trimesh.repair.fill_holes(cand)
                cand.merge_vertices()
                if _open_edges(cand) < _open_edges(best) and _vol_ok(cand, v0):
                    best = cand
            except Exception as e:
                print("  [fill_holes] %s" % e)
            # (2) pymeshlab: dedup + non-manifold repair + close holes (robust
            #     for self-intersecting thin shells where pymeshfix would
            #     collapse the walls).  Volume-guarded.
            if _open_edges(best):
                try:
                    import pymeshlab

                    ms = pymeshlab.MeshSet()
                    ms.add_mesh(
                        pymeshlab.Mesh(
                            np.ascontiguousarray(mesh.vertices, dtype=np.float64),
                            np.ascontiguousarray(mesh.faces, dtype=np.int32),
                        )
                    )
                    for fn in (
                        "meshing_remove_duplicate_vertices",
                        "meshing_remove_duplicate_faces",
                        "meshing_remove_null_faces",
                        "meshing_repair_non_manifold_edges",
                        "meshing_repair_non_manifold_vertices",
                        "meshing_close_holes",
                    ):
                        try:
                            ms.apply_filter(
                                fn,
                                **({"maxholesize": 100000} if fn.endswith("close_holes") else {}),
                            )
                        except Exception:
                            pass
                    mm = ms.current_mesh()
                    ml = trimesh.Trimesh(mm.vertex_matrix(), mm.face_matrix(), process=True)
                    if _open_edges(ml) < _open_edges(best) and _vol_ok(ml, v0):
                        best = ml
                except Exception as e:
                    print("  [pymeshlab] %s" % e)
            # (3) pymeshfix last resort — reject if it inflates/deletes
            if _open_edges(best):
                try:
                    import pymeshfix

                    vc, fc = pymeshfix.clean_from_arrays(
                        np.ascontiguousarray(mesh.vertices),
                        np.ascontiguousarray(mesh.faces, dtype=np.int32),
                        remove_smallest_components=False,
                    )
                    pm = trimesh.Trimesh(vc, fc, process=True)
                    if _open_edges(pm) < _open_edges(best) and _vol_ok(pm, v0):
                        best = pm
                except Exception as e:
                    print("  [pymeshfix] %s" % e)
            mesh = best
        meshes.append(mesh)
    import trimesh as _trimesh  # noqa: F811 — re-import for concatenate

    _trimesh.util.concatenate(meshes).export(str(path))
    oe = sum(_open_edges(m) for m in meshes)
    print("wrote %s: open=%d bodies=%d" % (path, oe, len(meshes)))


def center_drop(wp):
    """Centre a part in XY and drop its lowest point onto z=0 (ready to sit
    on the bed)."""
    b = wp.val().BoundingBox()
    return wp.translate((-(b.xmin + b.xmax) / 2, -(b.ymin + b.ymax) / 2, -b.zmin))


def build_split_plate(parts, gap=10.0):
    """Lay a list of already-oriented Workplanes side by side along X on one
    plate.

    Centre-drop each, space by *gap*, then recentre the whole group.  Returns
    a ``cq.Compound`` of N bodies (kept separate so the slicer sees distinct
    parts, not a fused blob).
    """
    import cadquery as cq

    placed, x = [], 0.0
    solids = []
    for wp in parts:
        p = center_drop(wp)
        w = p.val().BoundingBox().xlen
        placed.append(p.translate((x + w / 2, 0, 0)))
        x += w + gap
    for p in placed:
        solids.append(p.val())
    comp = cq.Compound.makeCompound(solids)
    b = comp.BoundingBox()
    return comp.translate((-(b.xmin + b.xmax) / 2, -(b.ymin + b.ymax) / 2, 0))
