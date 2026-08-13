"""Route handler for chat-originated image uploads.

Registers, against one served directory:

    POST /upload    accepts one raw image (PNG, JPEG or WEBP) into images/,
                     named by this process alone

Refuses, in the order checked: an absent or wrong token, or ``t`` given more
than once (the same ``ws.refusal()`` response ``/ws`` returns, so an
unauthenticated caller cannot tell this route apart from any other); a
disallowed ``Origin``, checked here as well as on ``/ws`` because this is the
one route in the process that writes into the human's project and a POST
carrying a raw body is a CORS simple request, so no preflight stands in front
of it; an unknown query key, ``kind`` given twice, or ``kind`` outside its
whitelist; a missing or zero Content-Length; a Content-Length over
``paths.MAX_IMAGE_BYTES``; a body whose bytes are not a PNG, JPEG or WEBP;
and, once past every check above, whatever ``paths.create_unique_image_file``
itself refuses (an ``images/`` entry that cannot be written into, or every
generated name already taken), plus a write that fails partway or a body
shorter than its declared length.

Nothing is left on disk by a refusal, including one decided after the file
exists and including a caller that simply disappears: every exit past the
open goes through ``_abandon``.

The body is raw bytes, not multipart: microdot ships no multipart parser, and
the one thing a client chooses about an upload, its kind (``upload`` or
``sketch``), fits in a query parameter. The written file's name, including
its extension, never comes from the client; see
``paths.create_unique_image_file`` and ``paths.sniff_image``.
"""

import asyncio
import os

from . import CHUNK_SIZE
from .ws import _origin_is_allowed, _token_is_allowed, refusal
from .. import paths


class _Refused(Exception):
    """A refusal decided after the file exists, carrying its own response.

    Raised rather than returned so that one handler, the one that also closes
    the descriptor and removes the file, decides every exit past the open.
    """

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message

# The only values the "kind" query parameter may take. Absent means the
# first entry. Held here, not in paths.py, because it is this route's own
# query parameter that is being validated, not a property of a file under
# images/.
UPLOAD_KINDS = ("upload", "sketch")

_ALLOWED_QUERY_KEYS = frozenset(("t", "kind"))


def _query_values(req, key):
    """Every value ``key`` was given in ``req``'s query string, in order.

    ``req.args`` is a plain ``{}`` when the request had no query string at
    all, and a ``MultiDict`` (which has ``getlist``) whenever it did, even if
    the string was empty. The same two-shape handling ``ws.py``'s own token
    check already does for ``t``.
    """
    if hasattr(req.args, "getlist"):
        return req.args.getlist(key)
    return [req.args[key]] if key in req.args else []


def _upload_kind(req):
    """Return ``(kind, None)`` or ``(None, error)`` for this request's query string.

    Only ``t`` and ``kind`` may appear, ``kind`` at most once and only from
    ``UPLOAD_KINDS``; anything else is refused without reading any of the
    body. Called only once the token has already passed, so this whitelist
    is never a way to probe the route unauthenticated.
    """
    keys = set(req.args.keys()) if hasattr(req.args, "keys") else set()
    unknown = sorted(keys - _ALLOWED_QUERY_KEYS)
    if unknown:
        return None, "unknown query parameter: %s" % ", ".join(unknown)
    kind_values = _query_values(req, "kind")
    if len(kind_values) > 1:
        return None, "kind must not be given more than once"
    kind = kind_values[0] if kind_values else UPLOAD_KINDS[0]
    if kind not in UPLOAD_KINDS:
        return None, "kind must be one of: %s" % ", ".join(UPLOAD_KINDS)
    return kind, None


def _write_all(fd, data):
    """Write every byte of ``data`` to ``fd``, looping past a short write.

    ``os.write`` is not guaranteed to write its whole argument in one call.
    Run through an executor by the caller, like every other blocking call
    this route makes.
    """
    written = 0
    while written < len(data):
        written += os.write(fd, data[written:])


def _finish(fd):
    """Flush ``fd`` to disk and close it, once every byte has been written."""
    os.fsync(fd)
    os.close(fd)


def _abandon(fd, target):
    """Close ``fd`` and remove ``target``, best-effort, after a failed upload.

    Called once the file already exists and something after that point
    refused the upload, so a rejected request leaves nothing on disk for
    ``/asset`` to serve. Both steps are best-effort: a failure closing or
    removing a file this process just created and is walking away from is
    not a new error worth surfacing over whatever refusal is already being
    returned to the client.
    """
    try:
        os.close(fd)
    except OSError:
        pass
    try:
        os.unlink(target)
    except OSError:
        pass


async def _receive(req, loop, fd, first, content_length):
    """Stream the rest of the body onto ``fd`` and return the total written.

    Raises ``_Refused`` for a body that stops short of its declared length or
    a write that fails, and propagates anything the stream itself raises. The
    caller owns ``fd``, so nothing here closes it: a peer that vanishes
    mid-upload makes ``read`` raise, and that has to reach the one place that
    knows there is a file to remove.
    """
    total = len(first)
    try:
        await loop.run_in_executor(None, _write_all, fd, first)
    except OSError as exc:
        raise _Refused(500, "write failed: %s" % exc)

    while total < content_length:
        chunk = await req.stream.read(min(CHUNK_SIZE, content_length - total))
        if not chunk:
            raise _Refused(400, "body ended after %d of %d bytes"
                                % (total, content_length))
        try:
            await loop.run_in_executor(None, _write_all, fd, chunk)
        except OSError as exc:
            raise _Refused(500, "write failed: %s" % exc)
        total += len(chunk)

    try:
        await loop.run_in_executor(None, _finish, fd)
    except OSError as exc:
        raise _Refused(500, "write failed: %s" % exc)
    return total


def register_chat_routes(app, serve_dir, *, token, allowed_origins=()):
    """Register ``POST /upload`` on ``app`` for one served directory."""
    serve_dir = paths.resolve_serve_dir(serve_dir)

    @app.post("/upload")
    async def upload(req):
        # Token then Origin, the same order and the same opaque refusal /ws
        # uses, so neither check tells an unauthenticated caller which one it
        # failed.
        if not _token_is_allowed(req, token):
            return refusal()
        if not _origin_is_allowed(req, allowed_origins):
            return refusal()

        kind, error = _upload_kind(req)
        if error is not None:
            return {"ok": False, "error": error}, 400

        content_length = req.content_length
        if content_length <= 0:
            return {"ok": False, "error": "Content-Length is required and "
                    "must be greater than zero"}, 411
        if content_length > paths.MAX_IMAGE_BYTES:
            return {"ok": False, "error": "body is %d bytes, over the %d "
                    "byte image limit" % (content_length, paths.MAX_IMAGE_BYTES)}, 413

        loop = asyncio.get_running_loop()

        # Read only as far as a verdict needs, looping rather than taking one
        # read as the answer because a real socket's read(n) can return far
        # fewer than n bytes even mid-transfer, and treating a short first read
        # as the whole head would refuse a perfectly good, merely slow, upload.
        # The loop stops at paths.SNIFF_MIN_BYTES because the sniffer looks no
        # further: reading a declared 8 MiB to the end before refusing its
        # first twelve bytes would let any holder of the token make this
        # process buffer the whole cap per connection, and nothing here limits
        # concurrent connections or times out a slow read.
        want = min(paths.SNIFF_MIN_BYTES, content_length)
        first = b""
        while len(first) < want:
            chunk = await req.stream.read(min(CHUNK_SIZE, content_length - len(first)))
            if not chunk:
                break
            first += chunk
        sniff = paths.sniff_image(first)
        if sniff is None:
            return {"ok": False, "error": "not a recognised image; only "
                    "PNG, JPEG and WEBP are accepted"}, 415
        media_type, suffix = sniff

        try:
            created = await loop.run_in_executor(
                None, paths.create_unique_image_file, serve_dir, kind, suffix)
        except FileExistsError:
            return {"ok": False, "error": "could not find a free name in "
                    "images/ after %d attempts" % paths.UNIQUE_IMAGE_NAME_ATTEMPTS}, 500
        except OSError as exc:
            # A directory this process cannot write into, a full filesystem, a
            # quota. Answered in the same shape as every other refusal here,
            # because a caller parsing JSON gets nothing it can show a human
            # out of microdot's plain-text 500.
            return {"ok": False, "error": "could not create the file: %s" % exc}, 500
        if created is None:
            return {"ok": False, "error": "refusing to write: %s/ must be a "
                    "real directory this process can create a file in"
                    % paths.IMAGES_DIRNAME}, 500
        fd, target = created

        # One exit for everything past the open. The stream reads inside
        # _receive raise ConnectionResetError when a peer disappears
        # mid-upload, which a closed tab, an aborted fetch or a dropped link
        # all produce routinely, and a cancelled task raises too. Either one
        # escaping would leave the descriptor open until the process hits
        # EMFILE and no upload ever succeeds again, and would leave a
        # truncated image in a git-tracked directory that /asset serves and
        # the model can attach later.
        try:
            total = await _receive(req, loop, fd, first, content_length)
        except BaseException as exc:
            await loop.run_in_executor(None, _abandon, fd, target)
            if isinstance(exc, _Refused):
                return {"ok": False, "error": exc.message}, exc.status
            raise

        return {"ok": True, "path": "images/%s" % target.name,
                "url": "/asset/%s" % target.name, "bytes": total,
                "media_type": media_type}, 200
