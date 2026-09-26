"""Mesh's review store: the two published files behind the shared review model.

The agent layer's review model (``annealage_agent.review``) is what Mesh's
review tools, ``GET /review`` and the ``review_changed`` push are built on.
Mesh does not keep that model in the agent layer's own file format, because it
already has one that is published: the README and the MIT skill document two
files in the served directory, and a separately running agent reads and writes
them directly. So this store is an adapter over exactly those files, and they
stay the only source of truth:

``mesh-comments.json``
    The human's pins, written whole by the viewer's Submit (``POST /submit``,
    ``http/routes_viewer.py``), never by this store. A pin placed but not
    submitted is a draft in the page and is not here, which is deliberate:
    Submit is the human's handoff. Read leniently, as Mesh always has: a file
    that is absent, does not parse, or holds no ``annotations`` list reads as
    "nothing submitted yet".
``mesh-callouts.json``
    The model's callouts, written by ``add_callout`` and ``delete_callout``
    through this store and, under the published contract, by any agent
    directly. Either shape the viewer accepts is read (a bare array, or an
    object with an ``annotations`` array), and a write always leaves the
    object shape. A file that exists but does not parse is refused, never
    overwritten.

How the two map onto ``Comment``, both ways:

- every entry of ``mesh-comments.json`` is ``author="human"``, every entry of
  ``mesh-callouts.json`` is ``author="model"``, whatever its own ``author``
  field says (the viewer draws every entry of the callouts file as a callout);
  a callout this store writes carries ``"author": "agent"``, the published
  value;
- ``comment`` is the text; ``part``, ``point``, ``normal`` and ``faceIndex``
  are the anchor (``MeshAnchorSpace``); ``id`` is the id; every other key
  (``label``, the face direction, and anything an agent added) is the
  comment's ``extra``, and the whole entry, key order included, is its
  ``record``, which is what the list tools show the model;
- nothing has a status: capabilities say so, and no resolve tool exists;
- ids are per file. The page numbers the human's pins and this store numbers
  callouts, one past the highest present, so a callout's id is never reused
  while the file lasts; a human pin and a callout can share a number.
"""

import json

from annealage_agent import files
from annealage_agent.review import (
    HUMAN,
    MODEL,
    AnchorSpace,
    Capabilities,
    Comment,
    Listing,
    ReviewError,
    ReviewStore,
    Written,
    bytes_state,
    file_lock,
)

from . import paths

# Cap on how many callouts the model may pin. Every one is a marker and a
# sprite in the viewer and a row in the side panel, so a model that pins a
# note per triangle makes the page unusable; the human can still hand-edit the
# file past this. Counted over every entry in the callouts file, since the
# viewer draws every one.
MAX_CALLOUTS = 200

# The keys of a Mesh annotation this adapter interprets; every other key is
# the comment's ``extra``.
_ANCHOR_KEYS = ("part", "point", "normal", "faceIndex")
_OWN_KEYS = frozenset(_ANCHOR_KEYS + ("id", "comment", "author"))

_POINT_SHAPE_MESSAGE = (
    "point must be an array of exactly three numbers, [x, y, z] in model coordinates"
)


class MeshAnchorSpace(AnchorSpace):
    """A point on a part of the 3D model: ``point`` ``[x, y, z]`` in model
    coordinates (the one space every part is drawn in), the ``part`` it is
    on, and, for a pin the page placed, the face's ``normal`` and its
    ``faceIndex`` in that part's triangles.

    The tool input offers ``point`` and ``part`` only, as ``add_callout``
    always has; ``normal`` and ``faceIndex`` come from a raycast in the page,
    which the model has no way to perform.
    """

    name = "mesh-point"
    schema = {
        "properties": {
            "point": {
                "type": "array",
                "items": {"type": "number"},
                "minItems": 3,
                "maxItems": 3,
                "description": "where the marker goes, [x, y, z] in model "
                "coordinates, the same space as a pin's point",
            },
            "part": {
                "type": "string",
                "description": "which part it is on, for the panel's label; "
                "a rel or a label from list_models",
            },
        },
        "required": ["point"],
    }

    def validate(self, anchor):
        point = anchor.get("point")
        if not isinstance(point, (list, tuple)) or len(point) != 3:
            raise ReviewError(_POINT_SHAPE_MESSAGE)
        try:
            normalised = {"point": [float(v) for v in point]}
        except (TypeError, ValueError):
            raise ReviewError("point must be three numbers, got %r" % (point,)) from None
        # A part that is not a non-empty name is left out rather than
        # refused, as add_callout always has: it only labels the panel row.
        part = anchor.get("part")
        if isinstance(part, str) and part.strip():
            normalised["part"] = part.strip()
        normal = anchor.get("normal")
        if normal is not None:
            if not isinstance(normal, (list, tuple)) or len(normal) != 3:
                raise ReviewError("normal must be an array of three numbers, or left out")
            try:
                normalised["normal"] = [float(v) for v in normal]
            except (TypeError, ValueError):
                raise ReviewError("normal must be three numbers, got %r" % (normal,)) from None
        face = anchor.get("faceIndex")
        if face is not None:
            if not isinstance(face, int) or isinstance(face, bool) or face < 0:
                raise ReviewError("faceIndex must be a triangle's index, or left out")
            normalised["faceIndex"] = face
        return normalised


class MeshFilesStore(ReviewStore):
    """The review over ``serve_dir``'s ``mesh-comments.json`` and
    ``mesh-callouts.json``; see this module's docstring for the mapping."""

    def __init__(self, serve_dir):
        super().__init__()
        self.serve_dir = files.resolve_serve_dir(serve_dir)
        self.anchor_space = MeshAnchorSpace()
        # Read when the store is built, so a test that lowers MAX_CALLOUTS
        # before building one sees its own limit.
        self.capabilities = Capabilities(
            can_resolve=False,
            can_delete_own=True,
            human_adds_via_api=False,
            max_open_model_callouts=MAX_CALLOUTS,
        )

    # -- the two files ----------------------------------------------------------

    def _read_callouts(self):
        """The callouts file's entries, ``[]`` when there is nothing to read,
        or ``None`` when it exists but is not a list of annotations.

        A symlink, a hard link or a FIFO at the name reads as absent, as on
        the ``/callouts`` route, and is refused on write.
        """
        raw = files.read_fixed_file(self.serve_dir, paths.CALLOUTS_JSON_NAME)
        if raw is None:
            return []
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except ValueError:
            return None
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict) and isinstance(parsed.get("annotations"), list):
            return parsed["annotations"]
        return None

    def _read_submission(self):
        """The human's whole submission record, ``{}`` when it is absent or
        cannot be read as one."""
        raw = files.read_fixed_file(self.serve_dir, paths.COMMENTS_JSON_NAME)
        if raw is None:
            return {}
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _write_callouts(self, annotations):
        """Replace the callouts file with ``annotations``, atomically; the
        path written, or ``None`` when the name is not a plain, single-linked
        file this store will write through."""
        target = files.safe_fixed_file(self.serve_dir, paths.CALLOUTS_JSON_NAME)
        if target is None:
            return None
        payload = json.dumps({"annotations": annotations}, indent=2) + "\n"
        return files.atomic_replace(target, payload.encode("utf-8"))

    def _callouts_path(self):
        return self.serve_dir / paths.CALLOUTS_JSON_NAME

    # -- the operations -------------------------------------------------------

    def list_comments(self, author=None, status=None):
        comments = []
        meta = {}
        if author in (None, HUMAN):
            record = self._read_submission()
            annotations = record.get("annotations")
            meta["submitted"] = isinstance(annotations, list)
            meta["submitted_at"] = record.get("submitted_at")
            comments += _comments(annotations if isinstance(annotations, list) else [], HUMAN)
        if author in (None, MODEL):
            # Under the file's lock, like the native store's reads: an
            # in-process write replacing the file mid-read would otherwise make
            # the guarded read refuse it, and a healthy file read as empty.
            with file_lock(self._callouts_path()):
                annotations = self._read_callouts()
            if annotations is None:
                raise ReviewError(
                    "%s exists but does not parse as JSON; read the file to see what "
                    "is in it" % paths.CALLOUTS_JSON_NAME
                )
            comments += _comments(annotations, MODEL)
        if status is not None:
            comments = [c for c in comments if c.status == status]
        return Listing(tuple(comments), meta)

    def add_comment(self, *, anchor, text, author, ref=None, extra=None):
        if author != MODEL:
            raise ReviewError(
                "the human's comments reach %s through Submit in the viewer, not "
                "through the review store" % paths.COMMENTS_JSON_NAME
            )
        anchor = self.anchor_space.validate(anchor)
        if not isinstance(text, str) or not text.strip():
            raise ReviewError("a callout needs a comment saying what it is about")
        extra = dict(extra or {})
        clashing = sorted(set(extra) & _OWN_KEYS)
        if clashing:
            raise ReviewError("%s cannot be extra fields of a callout" % ", ".join(clashing))
        with file_lock(self._callouts_path()):
            annotations = self._read_callouts()
            if annotations is None:
                raise ReviewError(
                    "%s exists but does not parse as JSON, so appending to it would "
                    "discard whatever is in it; read it and fix it first" % paths.CALLOUTS_JSON_NAME
                )
            if len(annotations) >= self.capabilities.max_open_model_callouts:
                raise ReviewError(
                    "there are already %d callouts, which is this tool's limit; "
                    "delete some with delete_callout before adding more" % len(annotations)
                )
            # The published shape, in the order add_callout has always
            # written it: author, point, comment, then what is optional, then
            # the id.
            entry = {"author": "agent", "point": anchor["point"], "comment": text.strip()}
            for key in ("part", "normal", "faceIndex"):
                if key in anchor:
                    entry[key] = anchor[key]
            entry.update(extra)
            entry["id"] = _next_callout_id(annotations)
            written = self._write_callouts(list(annotations) + [entry])
            if written is None:
                raise ReviewError(
                    "refusing to write %s: it is not a plain, single-linked file"
                    % paths.CALLOUTS_JSON_NAME
                )
        self._changed()
        return Written(_comment(entry, MODEL), len(annotations) + 1, written)

    def delete_callout(self, comment_id):
        with file_lock(self._callouts_path()):
            annotations = self._read_callouts()
            if annotations is None:
                raise ReviewError(
                    "%s does not parse as JSON, so nothing can be deleted from it"
                    % paths.CALLOUTS_JSON_NAME
                )
            removed = [a for a in annotations if _id_of(a) == comment_id]
            if not removed:
                present = ", ".join(str(_id_of(a)) for a in annotations) or "none"
                raise ReviewError(
                    "no callout with id %d; the ids present are: %s" % (comment_id, present)
                )
            kept = [a for a in annotations if _id_of(a) != comment_id]
            written = self._write_callouts(kept)
            if written is None:
                raise ReviewError(
                    "refusing to write %s: it is not a plain, single-linked file"
                    % paths.CALLOUTS_JSON_NAME
                )
        self._changed()
        return Written(_comment(removed[0], MODEL), len(kept), written)

    def state(self):
        # The digest covers both files: a Submit changes the review as much as
        # a callout does, and a page that shows submitted comments needs
        # telling. Whether the change has settled is judged by the callouts
        # file alone. It is the one an outside agent writes, possibly not
        # atomically, so a half-written one must be waited on; the comments
        # file is only ever replaced atomically by /submit, and one that does
        # not parse reads as "nothing submitted", so it must not hold back
        # every callout push for the full deferral while it stays that way.
        with file_lock(self._callouts_path()):
            callouts = files.read_fixed_file(self.serve_dir, paths.CALLOUTS_JSON_NAME)
        comments = files.read_fixed_file(self.serve_dir, paths.COMMENTS_JSON_NAME)
        digest, _settled = bytes_state(callouts, comments)
        return digest, bytes_state(callouts)[1]


def _id_of(entry):
    return entry.get("id") if isinstance(entry, dict) else None


def _next_callout_id(annotations):
    """One past the highest id present, so an id is never reused.

    Reusing an id would silently reattach whatever the human had already said
    about the old callout, since the viewer keys its markers and its list rows
    by that number.
    """
    used = [
        i
        for i in (_id_of(a) for a in annotations)
        if isinstance(i, int) and not isinstance(i, bool)
    ]
    return (max(used) + 1) if used else 1


def _comment(entry, author):
    """One annotation as a ``Comment``. Read leniently: the file is written by
    hand and by other agents, and its readers (the viewer, the list tools)
    have always shown whatever it holds, so nothing here is validated."""
    comment_id = entry.get("id")
    if not isinstance(comment_id, int) or isinstance(comment_id, bool):
        comment_id = None
    text = entry.get("comment")
    return Comment(
        id=comment_id,
        anchor={key: entry[key] for key in _ANCHOR_KEYS if key in entry},
        text=text if isinstance(text, str) else "",
        author=author,
        extra={key: value for key, value in entry.items() if key not in _OWN_KEYS},
        record=entry,
    )


def _comments(annotations, author):
    # An entry that is not an object cannot be a comment. Mesh itself never
    # writes one, and a write here keeps any it finds in the file untouched.
    return [_comment(entry, author) for entry in annotations if isinstance(entry, dict)]
