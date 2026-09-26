"""The six tools that read the human's comments, write the agent's own, and
write a record of the conversation out.

Four of them are the agent layer's shared review tools
(``annealage_agent.review.tools``) over ``MeshFilesStore`` (``review.py``),
configured below so the model sees exactly the surface Mesh has always had:
``list_comments`` for the human's submitted pins and ``list_callouts`` for the
callouts, ``add_callout`` taking a ``comment`` and a face ``label``,
``delete_callout``, with Mesh's own descriptions, schemas and results. That
surface is the other half of the file contract the published skill documents:
``mesh-comments.json`` is what the human's Submit writes and ``list_comments``
reads, and ``mesh-callouts.json`` is what an agent writes to put a cyan pin on
the geometry. Nothing here invents a new exchange format, so a project worked
on through the chat pane and a project worked on by a separately running
agent leave the same files behind. There is no ``resolve_comment``: neither
file has a status to resolve.

Two properties of that are worth stating because they are easy to assume
wrongly.

**The human's pins reach the file only when they press Submit.** A pin placed
and commented but not submitted is not in ``mesh-comments.json`` and
``list_comments`` cannot see it, which is deliberate: Submit is the human's
handoff, and reading a comment mid-typing would be reading a draft. The
``measure`` tool does reach live pins, because it asks the browser rather than
the file.

**A callout write is read-modify-write, and the file may have another
writer.** The contract keeps working for a separately running agent, so if one
is also writing this file, one of the two writes can lose the other's callout.
Writers in this process are serialised (``annealage_agent.review.file_lock``)
and the write itself is atomic, so a reader never sees a half-written list,
but nothing here can merge two independent processes' authors and it does not
pretend to.

The other two, ``snapshot`` and ``export_transcript``, are Mesh's own.
"""

import asyncio
import base64
import binascii
import functools
import os
import time

from annealage_agent import files
from annealage_agent.review import tools as shared_review
from annealage_agent.session import events
from annealage_agent.tools import fail, ok
from claude_agent_sdk import tool

from .. import paths
from ..review import MeshFilesStore

# What the browser is allowed to hand back for a snapshot, decoded. The socket
# already bounds the frame that carried it (``http/ws.py``); this bounds what
# lands in the project directory as a git-tracked file.
MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024

# Extension per capture format, so the bytes written match the name they are
# written under. The browser chooses the format (it re-encodes when a PNG would
# not fit in one frame), so the model's requested name cannot decide this.
_SNAPSHOT_SUFFIX = {"png": ".png", "jpeg": ".jpg", "webp": ".webp"}

# How many suffixed names are tried before a snapshot gives up on one base
# name. Enough that a model reusing a name a few times still gets a file;
# small enough that it does not stat its way through hundreds of entries.
_SNAPSHOT_NAME_ATTEMPTS = 20


# -- what the review tools show the model: Mesh's results, as they have always
#    read, built from the store's records (each annotation verbatim) ----------


def _present_comments(store, listing):
    if not listing.meta.get("submitted"):
        return ok(
            text="the human has not submitted any pin comments yet "
            "(%s does not exist, or holds no annotations)" % paths.COMMENTS_JSON_NAME
        )
    return ok(
        {
            "submitted_at": listing.meta.get("submitted_at"),
            "count": len(listing.comments),
            "annotations": [c.shown() for c in listing.comments],
        }
    )


def _present_callouts(store, listing):
    return ok(
        {"count": len(listing.comments), "annotations": [c.shown() for c in listing.comments]}
    )


def _present_added(store, written):
    return ok({"added": written.comment.shown(), "count": written.count, "path": str(written.path)})


def _present_deleted(store, written):
    return ok({"deleted": written.comment.id, "count": written.count, "path": str(written.path)})


#: The review tools as the model sees them in Mesh: names, descriptions,
#: schemas and results are Mesh's published surface, unchanged from before the
#: agent layer owned the review.
MESH_REVIEW_TOOLS = (
    shared_review.ListComments(
        name="list_comments",
        description="Read the human's submitted pin comments on the 3D model: each one's "
        "number, the part and face it is on, its point in model coordinates, "
        "and what they wrote. This is the feedback to act on. A pin the human "
        "has placed but not yet submitted is not here yet.",
        author="human",
        schema={},
        present=_present_comments,
    ),
    shared_review.ListComments(
        name="list_callouts",
        description="Read the callouts currently pinned on the 3D model, the cyan markers "
        "the human sees, including any you added earlier and any a previous "
        "session left. Use it before add_callout to avoid repeating a note, "
        "and to find the id delete_callout takes.",
        author="model",
        schema={},
        present=_present_callouts,
    ),
    shared_review.AddCallout(
        name="add_callout",
        description="Pin a note of your own at a point on the 3D model, which appears to "
        "the human as a numbered cyan marker in the viewer and a row in the "
        "review panel. This is how to point at a location instead of "
        "describing it: put the callout on the feature you are asking about or "
        "reporting on, and say what you mean in the comment.",
        text_field="comment",
        ref_field=None,
        extra_fields={
            "label": {
                "type": "string",
                "description": 'the face direction, e.g. "+Z", if it helps',
            }
        },
        # Verbatim, property order included, because it is what the model has
        # always been shown; the agent layer checks it declares exactly the
        # fields the handler reads (the anchor's point and part, the comment,
        # the label).
        schema={
            "type": "object",
            "properties": {
                "point": {
                    "type": "array",
                    "items": {"type": "number"},
                    "minItems": 3,
                    "maxItems": 3,
                    "description": "where the marker goes, [x, y, z] in model "
                    "coordinates, the same space as a pin's point",
                },
                "comment": {
                    "type": "string",
                    "description": "what you are saying about that point",
                },
                "part": {
                    "type": "string",
                    "description": "which part it is on, for the panel's label; "
                    "a rel or a label from list_models",
                },
                "label": {
                    "type": "string",
                    "description": 'the face direction, e.g. "+Z", if it helps',
                },
            },
            "required": ["point", "comment"],
        },
        present=_present_added,
    ),
    shared_review.DeleteCallout(
        name="delete_callout",
        description="Remove one of the callouts pinned on the 3D model by its id, so a "
        "note you have finished with stops cluttering the viewer. Use "
        "list_callouts to see the ids.",
        schema={"id": int},
        present=_present_deleted,
    ),
)


def _write_snapshot(serve_dir, wanted, suffix, data):
    """Write ``data`` under ``images/``, choosing a free name near ``wanted``.

    The name is retried rather than overwritten, because every image here is
    evidence of what a part looked like at some moment and a silent overwrite
    loses one. ``files.create_image_file`` raises ``FileExistsError`` for a
    taken name, which is the signal to try the next one.

    Returns None when the name or the ``images`` entry is not something this
    package will write, and re-raises ``FileExistsError`` once the suffixed
    names run out, so the caller can tell those two apart: one is a containment
    refusal and the other is a model that should pick a different name.
    """
    for attempt in range(1, _SNAPSHOT_NAME_ATTEMPTS + 1):
        name = wanted + suffix if attempt == 1 else "%s-%d%s" % (wanted, attempt, suffix)
        try:
            created = files.create_image_file(serve_dir, name)
        except FileExistsError:
            continue
        if created is None:
            return None
        fd, target = created
        try:
            written = 0
            while written < len(data):
                written += os.write(fd, data[written:])
        finally:
            os.close(fd)
        return target
    raise FileExistsError(name)


def build(bus, serve_dir, session_id=None, *, store=None):
    """Return the six review tools, bound to ``bus`` and ``serve_dir``.

    ``store`` is the app's ``MeshFilesStore`` (``bus.review_store``), so a
    callout the model adds reaches the review watcher at once; ``None``
    builds one over ``serve_dir``, which is what a test bus gets.

    ``session_id`` names the conversation ``export_transcript`` writes out.
    ``None`` means this tool server belongs to no session, which is what a
    viewer-only run and a test bus both look like; the tool is still built,
    because the classification in ``registry.py`` refuses a server whose tools
    do not match it exactly, and refuses at call time instead.
    """
    if store is None:
        store = MeshFilesStore(serve_dir)
    shared = shared_review.review_tools(store, bus=bus, tools=MESH_REVIEW_TOOLS)

    @tool(
        "snapshot",
        "Save a screenshot of the 3D viewer into the project's images/ "
        "directory as a file, so it can be committed or referred to later. "
        "Use capture_view instead when you only want to look at the part "
        "yourself; this one is for keeping the picture.",
        {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "base file name, without a directory or an "
                    "extension; a timestamp is used if omitted",
                },
                "width": {
                    "type": "integer",
                    "minimum": 64,
                    "maximum": 1568,
                    "description": "pixel width to render at; leave out for the canvas's own size",
                },
            },
        },
    )
    async def snapshot(args):
        params = {}
        width = args.get("width")
        if width is not None:
            if not isinstance(width, int) or isinstance(width, bool):
                raise ValueError("width must be an integer number of pixels")
            params["width"] = width
        wanted = args.get("name")
        if wanted is None:
            wanted = time.strftime("snapshot-%Y%m%d-%H%M%S")
        if not isinstance(wanted, str) or not wanted.strip():
            raise ValueError("name must be a base file name, or left out")
        wanted = os.path.splitext(wanted.strip())[0]

        result = await bus.call("viewer.capture_view", params)
        image = (result or {}).get("image") or ""
        _prefix, _, encoded = image.partition(",")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            data = b""
        if not data:
            return fail(
                "the viewer returned a capture this build could not read, so nothing was written"
            )
        if len(data) > MAX_SNAPSHOT_BYTES:
            return fail(
                "the capture is %d bytes, over this tool's %d byte limit; "
                "ask for a smaller width" % (len(data), MAX_SNAPSHOT_BYTES)
            )
        suffix = _SNAPSHOT_SUFFIX.get(result.get("format"), ".png")

        loop = asyncio.get_running_loop()
        try:
            target = await loop.run_in_executor(
                None, _write_snapshot, serve_dir, wanted, suffix, data
            )
        except FileExistsError:
            return fail(
                "%r and the next %d names after it are all taken in %s/; "
                "pass a different name"
                % (wanted + suffix, _SNAPSHOT_NAME_ATTEMPTS - 1, files.IMAGES_DIRNAME)
            )
        if target is None:
            return fail(
                "could not write the snapshot: %s/ must be a real "
                "directory (not a symlink) and the name must be a plain "
                "file name" % files.IMAGES_DIRNAME
            )
        return ok(
            {
                "path": str(target),
                "bytes": len(data),
                "width": result.get("width"),
                "height": result.get("height"),
                "url": "/asset/%s" % target.name,
            }
        )

    @tool(
        "export_transcript",
        "Write this conversation out as a file in the project's review/ "
        "directory, so it can be committed, attached to a review or read "
        "later without the server running. Use it when the human asks for a "
        "record of what was decided, or before finishing a piece of work that "
        "someone else will pick up. Choose markdown for something a person "
        "reads and jsonl for the raw event records.",
        {
            "type": "object",
            "properties": {
                "format": {
                    "type": "string",
                    "enum": list(events.TRANSCRIPT_FORMATS),
                    "description": "markdown for prose, jsonl for the raw "
                    "event records; markdown if omitted",
                },
                "include": {
                    "type": "string",
                    "enum": list(events.TRANSCRIPT_INCLUDE),
                    "description": "text for the conversation alone, full to "
                    "add tool inputs, tool results, permission "
                    "decisions and per-turn cost; text if "
                    "omitted",
                },
            },
        },
    )
    async def export_transcript(args):
        if session_id is None:
            return fail(
                "there is no session to export: this viewer is running "
                "without a conversation attached, so there is no "
                "transcript. Tell the human rather than retrying."
            )
        fmt = args.get("format", events.TRANSCRIPT_FORMATS[0])
        include = args.get("include", events.TRANSCRIPT_INCLUDE[0])
        if fmt not in events.TRANSCRIPT_FORMATS:
            raise ValueError("format must be one of: %s" % ", ".join(events.TRANSCRIPT_FORMATS))
        if include not in events.TRANSCRIPT_INCLUDE:
            raise ValueError("include must be one of: %s" % ", ".join(events.TRANSCRIPT_INCLUDE))

        loop = asyncio.get_running_loop()
        try:
            target = await loop.run_in_executor(
                None,
                functools.partial(
                    events.export_transcript, serve_dir, session_id, fmt=fmt, include=include
                ),
            )
        except FileExistsError:
            return fail(
                "every name this export would use in %s/ is already "
                "taken; the human will have to clear some out" % files.REVIEW_DIRNAME
            )
        except OSError as exc:
            return fail(
                "could not write the transcript (%s); %s/ must be a real "
                "directory this process can write into, so tell the human "
                "rather than retrying" % (exc, files.REVIEW_DIRNAME)
            )
        return ok(
            {
                "path": "%s/%s" % (files.REVIEW_DIRNAME, target.name),
                "bytes": target.stat().st_size,
                "format": fmt,
                "include": include,
            }
        )

    return shared + [snapshot, export_transcript]
