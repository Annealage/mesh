"""The two tools that read the model files themselves, off the event loop.

Both go through the same scan ``/manifest`` is answered from, so what the
model is told exists is exactly what the viewer can load and what the human
sees listed: dotdirs, symlinks and the scan caps all apply here without being
restated, and a ``rel`` reported here is one ``set_visibility`` will accept.

Geometry facts come from ``stl.py``, this package's own dependency-free
reader, rather than a mesh library, because header, bounding box and triangle
count are all the model needs and a second opinion about what counts as a
valid STL would eventually disagree with the one the viewer's loader and the
models watcher already use.
"""

import asyncio
import os

from claude_agent_sdk import tool

from .. import paths, stl
from . import fail, ok

def _list(serve_dir):
    models, truncated = paths.scan_models(serve_dir)
    listing = []
    for model in models:
        entry = {"rel": model["rel"], "label": model["label"]}
        try:
            st = os.stat(model["path"])
        except OSError:
            # Deleted between the scan and the stat. Reported as listed with no
            # size rather than dropped, because "it was there a moment ago" is
            # the truth and a model that has just regenerated a part sees this.
            entry["bytes"] = None
        else:
            entry["bytes"] = st.st_size
        listing.append(entry)
    return {"dir": str(paths.resolve_serve_dir(serve_dir)),
            "models": listing,
            "truncated": truncated}


def _facts(serve_dir, rel):
    """Geometry facts for one model, or a string naming what went wrong."""
    index = paths.build_model_index(serve_dir)
    target = index.by_rel(rel)
    if target is None:
        known = ", ".join(m["rel"] for m in index.manifest_models) or "none"
        return "no model at rel %r; the models here are: %s" % (rel, known)
    try:
        facts = stl.read_stl_facts(str(target))
    except stl.StlError as exc:
        return ("%s is not readable as STL: %s. If you have just written it, it "
                "may still be being written; otherwise check what generated it."
                % (rel, exc))
    except OSError as exc:
        return "could not read %s: %s" % (rel, exc)
    size = facts.get("bbox_min"), facts.get("bbox_max")
    extent = None
    if all(v is not None for v in size):
        extent = [round(hi - lo, 4) for lo, hi in zip(size[0], size[1])]
    return {"rel": rel, "format": facts["format"], "triangles": facts["triangles"],
            "bbox_min": facts["bbox_min"], "bbox_max": facts["bbox_max"],
            "extent": extent, "bytes": facts["size_bytes"],
            "header": facts["header"]}


def build(serve_dir):
    """Return the two model tools, bound to ``serve_dir``."""

    @tool(
        "list_models",
        "List the STL models in the project directory that the 3D viewer is "
        "showing, with the rel path every other mesh tool identifies a part "
        "by, its display label and its size in bytes. Use it to find out what "
        "parts exist before reading or changing one.",
        {},
    )
    async def list_models(args):
        loop = asyncio.get_running_loop()
        return ok(await loop.run_in_executor(None, _list, serve_dir))

    @tool(
        "model_info",
        "Read one STL model's geometry facts without loading a mesh library: "
        "triangle count, bounding box, overall extent in model units, file "
        "size and format. Use it to check that a part you have just generated "
        "is the size you intended, or to find its dimensions.",
        {"rel": str},
    )
    async def model_info(args):
        rel = args.get("rel")
        if not isinstance(rel, str) or not rel:
            raise ValueError("rel must be a model's rel path, as reported by "
                             "list_models")
        loop = asyncio.get_running_loop()
        facts = await loop.run_in_executor(None, _facts, serve_dir, rel)
        return fail(facts) if isinstance(facts, str) else ok(facts)

    return [list_models, model_info]
