# /// script
# requires-python = ">=3.10"
# dependencies = ["cadquery>=2.4", "trimesh", "numpy", "matplotlib", "shapely"]
# ///
"""Agent-side self-verification helpers for CAD geometry.

Cross-sections, point-in-solid probes, and watertight/volume/body counts.
matplotlib is used deliberately — offscreen OpenSCAD segfaults in
headless/WSL environments, and 2D sections read interior features better
than a shaded 3D view.

Standalone usage (verify an STL)::

    uv run cad/section_probe.py verify models/part.stl
    uv run cad/section_probe.py section models/part.stl --plane 0 0 1 -o images/section-z.png
    uv run cad/section_probe.py probe models/part.stl 10 5 3  0 0 0  -5 -5 10
"""

from __future__ import annotations


def to_trimesh(cq_obj, tol=0.1):
    """Tessellate a CadQuery Workplane/Solid into a trimesh.

    ``process=False`` keeps raw geometry so open-edge counts are meaningful.
    Lower *tol* = finer mesh.
    """
    import numpy as np
    import trimesh

    shape = cq_obj.val() if hasattr(cq_obj, "val") else cq_obj
    solids = shape.Solids() or [shape]
    verts, faces, off = [], [], 0
    for s in solids:
        v, f = s.tessellate(tol)
        verts += [(p.x, p.y, p.z) for p in v]
        faces += [(a + off, b + off, c + off) for a, b, c in f]
        off = len(verts)
    return trimesh.Trimesh(np.array(verts), np.array(faces), process=False)


def load_stl(path):
    """Load an STL file directly into a trimesh (no CadQuery needed)."""
    import trimesh

    return trimesh.load(str(path), force="mesh", process=False)


def open_edges(mesh):
    """Count boundary (open) edges — 0 means watertight."""
    from trimesh.grouping import group_rows

    return int(group_rows(mesh.edges_sorted, require_count=1).shape[0])


def report(mesh, label="mesh"):
    """Print and return the three green-light numbers: open edges, volume,
    body count."""
    bodies = len(mesh.split(only_watertight=False))
    info = {
        "open_edges": open_edges(mesh),
        "volume": round(float(mesh.volume)),
        "bodies": bodies,
    }
    print(
        "%s: open=%d vol=%d bodies=%d" % (label, info["open_edges"], info["volume"], info["bodies"])
    )
    return info


def probe_line(mesh, points):
    """Return solid(True)/void(False) for each ``(x, y, z)``.

    Use to confirm a cavity is hollow or a corner was rounded away.
    """
    import numpy as np

    return list(mesh.contains(np.asarray(points, dtype=float)))


def section_png(
    meshes,
    path,
    plane_origin=(0, 0, 0),
    plane_normal=(0, 0, 1),
    overlays=None,
    xlim=None,
    ylim=None,
    title="",
):
    """Slice one or more ``(mesh, color, linewidth)`` tuples on a plane and
    save a 2D outline PNG.

    Axes: for a Z-normal slice the plot is X (horizontal) vs Y; for a
    Y-normal slice it's X vs Z; for X-normal it's Y vs Z.  *overlays* is
    a list of ``(xs, ys, style)`` polylines to draw on top.
    """
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    n = tuple(plane_normal)
    ai, bi = (0, 1) if n == (0, 0, 1) else (0, 2) if n == (0, 1, 0) else (1, 2)
    fig, ax = plt.subplots(figsize=(11, 7))
    for mesh, color, lw in meshes:
        sec = mesh.section(plane_origin=list(plane_origin), plane_normal=list(plane_normal))
        if sec is None:
            continue
        for ent in sec.entities:
            p = sec.vertices[ent.points]
            ax.plot(p[:, ai], p[:, bi], color=color, lw=lw)
    for xs, ys, style in overlays or []:
        ax.plot(xs, ys, style, lw=1.2)
    if xlim:
        ax.set_xlim(*xlim)
    if ylim:
        ax.set_ylim(*ylim)
    ax.set_aspect("equal")
    ax.grid(alpha=0.3)
    ax.set_title(title)
    fig.tight_layout()
    fig.savefig(path, dpi=95)
    plt.close(fig)
    print("wrote %s" % path)


if __name__ == "__main__":
    import json
    import sys

    def _usage():
        print("Usage:")
        print("  uv run section_probe.py verify <stl>")
        print(
            "  uv run section_probe.py section <stl> [--plane nx ny nz] [--offset F] [-o out.png]"
        )
        print("  uv run section_probe.py probe <stl> x1 y1 z1 [x2 y2 z2 ...]")
        sys.exit(1)

    if len(sys.argv) < 3:
        _usage()

    cmd, stl_path = sys.argv[1], sys.argv[2]

    if cmd == "verify":
        mesh = load_stl(stl_path)
        info = report(mesh, stl_path)
        print(json.dumps(info, indent=2))

    elif cmd == "section":
        import argparse

        ap = argparse.ArgumentParser()
        ap.add_argument("stl")
        ap.add_argument("--plane", nargs=3, type=float, default=[0, 0, 1])
        ap.add_argument("--offset", type=float, default=0.0)
        ap.add_argument("-o", "--output", default="section.png")
        ap.add_argument("--title", default="")
        args = ap.parse_args(sys.argv[2:])
        mesh = load_stl(args.stl)
        nx, ny, nz = args.plane
        ox = nx * args.offset
        oy = ny * args.offset
        oz = nz * args.offset
        section_png(
            [(mesh, "steelblue", 1.5)],
            args.output,
            plane_origin=(ox, oy, oz),
            plane_normal=(nx, ny, nz),
            title=args.title or ("section z=%.1f" % args.offset if nz else "section"),
        )

    elif cmd == "probe":
        coords = [float(x) for x in sys.argv[3:]]
        if len(coords) % 3 != 0:
            print("probe needs triples of x y z coordinates")
            sys.exit(1)
        points = [coords[i : i + 3] for i in range(0, len(coords), 3)]
        mesh = load_stl(stl_path)
        results = probe_line(mesh, points)
        for pt, inside in zip(points, results, strict=True):
            tag = "SOLID" if inside else "VOID"
            print("  (%.1f, %.1f, %.1f) -> %s" % (pt[0], pt[1], pt[2], tag))

    else:
        _usage()
