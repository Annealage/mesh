"""The two CAD-pipeline tools: STL mesh verification and dimension management.

``mesh_verify`` gives the agent a one-call quality gate for any model in
the served directory — watertight status, open edges, volume, body count —
without needing to run a separate script.  Enhanced analysis (the trimesh
numbers) is available when ``trimesh`` and ``numpy`` are installed;
otherwise the tool returns basic geometry facts from this package's own
dependency-free STL reader and tells the agent how to get the full check.

``mesh_dimensions`` is a CRUD interface to the project's
``dimensions.json`` — the single source of truth for all measured values.
It is pure-stdlib and works on every install.
"""

import asyncio
import json

from claude_agent_sdk import tool

from .. import paths, stl
from . import fail, ok

# ── Dimensions helpers (pure stdlib) ─────────────────────────────


def _dims_path(serve_dir):
    return paths.resolve_serve_dir(serve_dir) / "dimensions.json"


def _read_dims(serve_dir):
    p = _dims_path(serve_dir)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _write_dims(serve_dir, dims):
    p = _dims_path(serve_dir)
    paths.atomic_replace(p, (json.dumps(dims, indent=2) + "\n").encode("utf-8"))
    return str(p)


def _get_path(d, key_path):
    """Walk a dotted key path like 'radiator.ear_height_y'."""
    parts = key_path.split(".")
    val = d
    for part in parts:
        if not isinstance(val, dict) or part not in val:
            return None, False
        val = val[part]
    return val, True


def _set_path(d, key_path, value):
    """Set a value at a dotted key path, creating intermediate dicts."""
    parts = key_path.split(".")
    target = d
    for part in parts[:-1]:
        if part not in target or not isinstance(target[part], dict):
            target[part] = {}
        target = target[part]
    target[parts[-1]] = value


def _list_leaves(d, prefix=""):
    """Yield (dotted_key, value) for every non-dict, non-meta leaf."""
    for k, v in d.items():
        if k.startswith(("_", "//")):
            continue
        path = "%s.%s" % (prefix, k) if prefix else k
        if isinstance(v, dict):
            yield from _list_leaves(v, path)
        else:
            yield path, v


# ── STL verify helpers ───────────────────────────────────────────


def _basic_facts(stl_path):
    """Geometry facts from the package's own dependency-free STL reader."""
    facts = stl.read_stl_facts(str(stl_path))
    result = {
        "triangles": facts.get("triangle_count", 0),
        "bbox_min": facts.get("bbox_min"),
        "bbox_max": facts.get("bbox_max"),
    }
    bmin, bmax = result["bbox_min"], result["bbox_max"]
    if bmin is not None and bmax is not None:
        result["extent"] = [round(hi - lo, 4) for lo, hi in zip(bmin, bmax, strict=True)]
    return result


def _enhanced_analysis(stl_path):
    """Trimesh-based analysis: open edges, volume, body count, watertight."""
    try:
        import numpy as np  # noqa: F401 — trimesh needs it
        import trimesh
        from trimesh.grouping import group_rows
    except ImportError:
        return None
    try:
        mesh = trimesh.load(str(stl_path), force="mesh", process=False)
        oe = int(group_rows(mesh.edges_sorted, require_count=1).shape[0])
        bodies = len(mesh.split(only_watertight=False))
        return {
            "open_edges": oe,
            "volume": round(float(mesh.volume), 2),
            "bodies": bodies,
            "watertight": oe == 0,
        }
    except Exception as exc:
        return {"error": "trimesh analysis failed: %s" % exc}


# ── Tool builders ────────────────────────────────────────────────


def build(serve_dir):
    """Return the two CAD tools, bound to ``serve_dir``."""

    @tool(
        "mesh_verify",
        "Check an STL model's mesh quality: triangle count, bounding box, "
        "and (if trimesh is installed) watertight status, open-edge count, "
        "volume, and body count.  Call this after regenerating a model and "
        "before asking the human to review it.",
        {"rel": str},
    )
    async def mesh_verify(args):
        rel = args.get("rel")
        if not isinstance(rel, str) or not rel:
            raise ValueError("rel must be a model's rel path, as reported by list_models")
        index = paths.build_model_index(serve_dir)
        target = index.by_rel(rel)
        if target is None:
            known = ", ".join(m["rel"] for m in index.manifest_models) or "none"
            return fail("no model at rel %r; the models here are: %s" % (rel, known))

        def _run():
            result = _basic_facts(target)
            enhanced = _enhanced_analysis(target)
            if enhanced is not None:
                result.update(enhanced)
            else:
                result["trimesh_available"] = False
                result["note"] = (
                    "Install trimesh for watertight/volume analysis: "
                    "pip install trimesh numpy.  Or run: "
                    "uv run cad/section_probe.py verify %s" % rel
                )
            return result

        result = await asyncio.get_event_loop().run_in_executor(None, _run)
        return ok(result)

    @tool(
        "mesh_dimensions",
        "Read, write, or list entries in the project's dimensions.json — "
        "the single source of truth for all measured values that the model "
        "script reads.  Actions: 'read' (full file), 'get' (one key by "
        "dotted path), 'set' (one key with value and optional note), "
        "'list' (all leaf keys with values).",
        {"action": str, "key": str, "value": str, "note": str},
    )
    async def mesh_dimensions(args):
        action = args.get("action", "")
        key = args.get("key", "")
        value = args.get("value", "")
        note = args.get("note", "")

        if action == "read":
            dims = _read_dims(serve_dir)
            if dims is None:
                return fail(
                    "no dimensions.json found; create one with "
                    "'annealage-mesh init' or write it by hand"
                )
            return ok(dims)

        if action == "list":
            dims = _read_dims(serve_dir)
            if dims is None:
                return fail("no dimensions.json found")
            leaves = [{"key": k, "value": v} for k, v in _list_leaves(dims)]
            return ok({"count": len(leaves), "dimensions": leaves})

        if action == "get":
            if not key:
                return fail("'get' needs a key (dotted path, e.g. 'radiator.ear_height_y')")
            dims = _read_dims(serve_dir)
            if dims is None:
                return fail("no dimensions.json found")
            val, found = _get_path(dims, key)
            if not found:
                return fail("key %r not found in dimensions.json" % key)
            return ok({"key": key, "value": val})

        if action == "set":
            if not key:
                return fail("'set' needs a key (dotted path)")
            if not value:
                return fail("'set' needs a value (JSON-encoded, e.g. '78.38')")
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                return fail("value must be valid JSON (e.g. 78.38, '\"text\"', '[1,2]')")
            dims = _read_dims(serve_dir) or {}
            _set_path(dims, key, parsed)
            if note:
                notes = dims.setdefault("_notes", {})
                notes[key] = note
            written = _write_dims(serve_dir, dims)
            return ok({"key": key, "value": parsed, "written": written})

        return fail("unknown action %r; use 'read', 'get', 'set', or 'list'" % action)

    return [mesh_verify, mesh_dimensions]
