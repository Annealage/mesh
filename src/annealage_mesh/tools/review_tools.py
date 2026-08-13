"""The six tools that read the human's comments, write the agent's own, and
write a record of the conversation out.

Two of these are the other half of the file contract the published skill
documents: ``mesh-comments.json`` is what the human's Submit writes and this
reads, and ``mesh-callouts.json`` is what an agent writes to put a cyan pin on
the geometry. Nothing here invents a new exchange format; ``add_callout`` and
``delete_callout`` write exactly the shape ``SKILL.md`` and the README
describe, so a project worked on through the chat pane and a project worked on
by a separately running agent leave the same files behind.

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
The write itself is atomic, so a reader never sees a half-written list, but
nothing here can merge two independent authors and it does not pretend to.
"""

import asyncio
import base64
import binascii
import functools
import json
import os
import time

from claude_agent_sdk import tool

from .. import paths
from ..session import events
from . import fail, ok

# Cap on how many callouts this tool will let the file grow to. Every one is a
# marker and a sprite in the viewer and a row in the side panel, so a model
# that pins a note per triangle makes the page unusable; the human can still
# hand-edit the file past this.
MAX_CALLOUTS = 200

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


def _read_annotations(serve_dir, name):
    """The annotation list in one exchange file, or None if it is unreadable.

    Accepts both shapes the viewer accepts: a bare JSON array, and an object
    with an ``annotations`` array. Anything else, including a file that does
    not parse, reads as None rather than as an empty list, so a caller can tell
    "there is nothing here" apart from "there is something here I could not
    read" and say so.
    """
    raw = paths.read_fixed_file(serve_dir, name)
    if raw is None:
        return []
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict) and isinstance(parsed.get("annotations"), list):
        return parsed["annotations"]
    return None


def _read_record(serve_dir, name):
    """The whole submission record, for ``list_comments``' own extra fields."""
    raw = paths.read_fixed_file(serve_dir, name)
    if raw is None:
        return {}
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _write_callouts(serve_dir, annotations):
    """Replace the callouts file with ``annotations``, atomically.

    Returns the path written, or None when the name is not something this
    package will write: the file name is fixed by this package but its
    directory entry is not, and a symlink or a hardlink left at that name would
    otherwise be written through.
    """
    target = paths.safe_fixed_file(serve_dir, paths.CALLOUTS_JSON_NAME)
    if target is None:
        return None
    payload = json.dumps({"annotations": annotations}, indent=2) + "\n"
    return paths.atomic_replace(target, payload.encode("utf-8"))


def _next_callout_id(annotations):
    """One past the highest id present, so an id is never reused.

    Reusing an id would silently reattach whatever the human had already said
    about the old callout, since the viewer keys its markers and its list rows
    by that number.
    """
    used = [a.get("id") for a in annotations if isinstance(a.get("id"), int)]
    return (max(used) + 1) if used else 1


def _add_callout(serve_dir, entry):
    annotations = _read_annotations(serve_dir, paths.CALLOUTS_JSON_NAME)
    if annotations is None:
        return (
            "%s exists but does not parse as JSON, so appending to it would "
            "discard whatever is in it; read it and fix it first" % paths.CALLOUTS_JSON_NAME
        )
    if len(annotations) >= MAX_CALLOUTS:
        return (
            "there are already %d callouts, which is this tool's limit; "
            "delete some with delete_callout before adding more" % len(annotations)
        )
    entry = dict(entry, id=_next_callout_id(annotations))
    written = _write_callouts(serve_dir, list(annotations) + [entry])
    if written is None:
        return (
            "refusing to write %s: it is not a plain, single-linked file" % paths.CALLOUTS_JSON_NAME
        )
    return {"added": entry, "count": len(annotations) + 1, "path": str(written)}


def _delete_callout(serve_dir, callout_id):
    annotations = _read_annotations(serve_dir, paths.CALLOUTS_JSON_NAME)
    if annotations is None:
        return (
            "%s does not parse as JSON, so nothing can be deleted from it"
            % paths.CALLOUTS_JSON_NAME
        )
    kept = [a for a in annotations if a.get("id") != callout_id]
    if len(kept) == len(annotations):
        present = ", ".join(str(a.get("id")) for a in annotations) or "none"
        return "no callout with id %d; the ids present are: %s" % (callout_id, present)
    written = _write_callouts(serve_dir, kept)
    if written is None:
        return (
            "refusing to write %s: it is not a plain, single-linked file" % paths.CALLOUTS_JSON_NAME
        )
    return {"deleted": callout_id, "count": len(kept), "path": str(written)}


def _write_snapshot(serve_dir, wanted, suffix, data):
    """Write ``data`` under ``images/``, choosing a free name near ``wanted``.

    The name is retried rather than overwritten, because every image here is
    evidence of what a part looked like at some moment and a silent overwrite
    loses one. ``paths.create_image_file`` raises ``FileExistsError`` for a
    taken name, which is the signal to try the next one.

    Returns None when the name or the ``images`` entry is not something this
    package will write, and re-raises ``FileExistsError`` once the suffixed
    names run out, so the caller can tell those two apart: one is a containment
    refusal and the other is a model that should pick a different name.
    """
    for attempt in range(1, _SNAPSHOT_NAME_ATTEMPTS + 1):
        name = wanted + suffix if attempt == 1 else "%s-%d%s" % (wanted, attempt, suffix)
        try:
            created = paths.create_image_file(serve_dir, name)
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


def build(bus, serve_dir, session_id=None):
    """Return the six review tools, bound to ``bus`` and ``serve_dir``.

    ``session_id`` names the conversation ``export_transcript`` writes out.
    ``None`` means this tool server belongs to no session, which is what a
    viewer-only run and a test bus both look like; the tool is still built,
    because the classification in ``registry.py`` refuses a server whose tools
    do not match it exactly, and refuses at call time instead.
    """

    @tool(
        "list_comments",
        "Read the human's submitted pin comments on the 3D model: each one's "
        "number, the part and face it is on, its point in model coordinates, "
        "and what they wrote. This is the feedback to act on. A pin the human "
        "has placed but not yet submitted is not here yet.",
        {},
    )
    async def list_comments(args):
        loop = asyncio.get_running_loop()
        record = await loop.run_in_executor(None, _read_record, serve_dir, paths.COMMENTS_JSON_NAME)
        annotations = record.get("annotations")
        if not isinstance(annotations, list):
            return ok(
                text="the human has not submitted any pin comments yet "
                "(%s does not exist, or holds no annotations)" % paths.COMMENTS_JSON_NAME
            )
        return ok(
            {
                "submitted_at": record.get("submitted_at"),
                "count": len(annotations),
                "annotations": annotations,
            }
        )

    @tool(
        "list_callouts",
        "Read the callouts currently pinned on the 3D model, the cyan markers "
        "the human sees, including any you added earlier and any a previous "
        "session left. Use it before add_callout to avoid repeating a note, "
        "and to find the id delete_callout takes.",
        {},
    )
    async def list_callouts(args):
        loop = asyncio.get_running_loop()
        annotations = await loop.run_in_executor(
            None, _read_annotations, serve_dir, paths.CALLOUTS_JSON_NAME
        )
        if annotations is None:
            return fail(
                "%s exists but does not parse as JSON; read the file to "
                "see what is in it" % paths.CALLOUTS_JSON_NAME
            )
        return ok({"count": len(annotations), "annotations": annotations})

    @tool(
        "add_callout",
        "Pin a note of your own at a point on the 3D model, which appears to "
        "the human as a numbered cyan marker in the viewer and a row in the "
        "review panel. This is how to point at a location instead of "
        "describing it: put the callout on the feature you are asking about or "
        "reporting on, and say what you mean in the comment.",
        {
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
    )
    async def add_callout(args):
        point = args.get("point")
        if not isinstance(point, (list, tuple)) or len(point) != 3:
            raise ValueError(
                "point must be an array of exactly three numbers, [x, y, z] in model coordinates"
            )
        try:
            point = [float(v) for v in point]
        except (TypeError, ValueError):
            raise ValueError("point must be three numbers, got %r" % (point,)) from None
        comment = args.get("comment")
        if not isinstance(comment, str) or not comment.strip():
            raise ValueError(
                "comment must say what you mean about that point; "
                "a callout with no comment is a marker the human "
                "cannot interpret"
            )
        entry = {"author": "agent", "point": point, "comment": comment.strip()}
        for key in ("part", "label"):
            value = args.get(key)
            if isinstance(value, str) and value.strip():
                entry[key] = value.strip()
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, _add_callout, serve_dir, entry)
        return fail(result) if isinstance(result, str) else ok(result)

    @tool(
        "delete_callout",
        "Remove one of the callouts pinned on the 3D model by its id, so a "
        "note you have finished with stops cluttering the viewer. Use "
        "list_callouts to see the ids.",
        {"id": int},
    )
    async def delete_callout(args):
        callout_id = args.get("id")
        if not isinstance(callout_id, int) or isinstance(callout_id, bool):
            raise ValueError("id must be a callout id, as reported by list_callouts")
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, _delete_callout, serve_dir, callout_id)
        return fail(result) if isinstance(result, str) else ok(result)

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
                % (wanted + suffix, _SNAPSHOT_NAME_ATTEMPTS - 1, paths.IMAGES_DIRNAME)
            )
        if target is None:
            return fail(
                "could not write the snapshot: %s/ must be a real "
                "directory (not a symlink) and the name must be a plain "
                "file name" % paths.IMAGES_DIRNAME
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
                "taken; the human will have to clear some out" % paths.REVIEW_DIRNAME
            )
        except OSError as exc:
            return fail(
                "could not write the transcript (%s); %s/ must be a real "
                "directory this process can write into, so tell the human "
                "rather than retrying" % (exc, paths.REVIEW_DIRNAME)
            )
        return ok(
            {
                "path": "%s/%s" % (paths.REVIEW_DIRNAME, target.name),
                "bytes": target.stat().st_size,
                "format": fmt,
                "include": include,
            }
        )

    return [list_comments, list_callouts, add_callout, delete_callout, snapshot, export_transcript]
