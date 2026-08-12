"""Route handlers for the packaged viewer and the STL/callout file contract.

Registers, against one served directory:

    GET  /, /index.html          the packaged three.js viewer
    GET  /manifest                model listing, rescanned at most once per
                                  INDEX_CACHE_TTL seconds
    GET  /model/<path:rel>        model bytes, resolved through the manifest index
    GET  /<name>.stl              compatibility alias into the manifest index,
                                  by bare filename (case-insensitive
                                  extension), for the shipped viewer's
                                  ``loader.load(p.file)`` calls
    GET  /asset/<path:rel>        image bytes, restricted to an images/ subtree
    GET  /callouts, /callouts.json   agent-authored callouts (read-only here)
    POST /submit                  human pin-comment submissions

Files this module writes:
    mesh-comments.json   overwritten on every /submit
    mesh-comments.log    appended on every /submit

Files this module reads but never writes:
    mesh-callouts.json   written by an agent directly; served verbatim

Deliberately absent: any route that serves a file under the served
directory that the manifest scan did not list and that is not under
images/. The viewer's own assets come only from this package's static/
directory, never from the served directory.
"""

import asyncio
import datetime
import errno
import json
import os
import stat
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import unquote

from .. import paths
from . import Response, file_response

# Mode for a submission file this process creates. An existing file's own
# mode is preserved instead; see _write_comments.
_RECORD_FILE_MODE = 0o644

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
VIEWER_HTML = STATIC_DIR / "viewer.html"

# How long a scanned ModelIndex is reused before the next request triggers a
# fresh scan. The scan lists the served directory plus a symlink resolve per
# candidate file, and one page load fetches the manifest and then one
# model per visible part; without this, that single page load would pay for
# one full walk per part instead of one walk shared across all of them. The
# trade this makes: a model added, removed or changed on disk can take up to
# this long to be reflected, rather than appearing on the very next request.
INDEX_CACHE_TTL = 1.0


def register_routes(app, serve_dir):
    """Register the viewer routes on ``app`` for one served directory.

    A fresh set of closures per call, so independent apps (independent
    served directories, as in the test suite) never share route state or a
    model-index cache.
    """
    serve_dir = paths.resolve_serve_dir(serve_dir)
    comments_json = paths.comments_path(serve_dir)

    index_cache = {"index": None, "at": 0.0, "pending": None}
    # Opened on the first submission and then held, so the log's name is
    # resolved once rather than on every write. The file therefore does not
    # exist until something is actually submitted.
    log_state = {"fd": None, "lock": asyncio.Lock()}

    async def get_log_fd():
        fd = log_state["fd"]
        if fd is not None:
            # An inode with no names left still accepts writes that nobody can
            # ever read, so a log deleted or replaced under a running server
            # would report every submission as stored while discarding it.
            # Dropping the descriptor here makes the next call reopen by name.
            try:
                if os.fstat(fd).st_nlink == 0:
                    os.close(fd)
                    log_state["fd"] = None
                    fd = None
            except OSError:
                log_state["fd"] = None
                fd = None
        if fd is not None:
            return fd
        async with log_state["lock"]:
            if log_state["fd"] is None:
                loop = asyncio.get_running_loop()
                log_state["fd"] = await loop.run_in_executor(
                    None, paths.open_fixed_file_for_append,
                    serve_dir, paths.COMMENTS_LOG_NAME, _RECORD_FILE_MODE)
            return log_state["fd"]

    async def get_index():
        """Return a ``ModelIndex`` for ``serve_dir``, scanning off the event
        loop and reusing the result for up to ``INDEX_CACHE_TTL`` seconds.

        Only one scan is ever in flight at a time. A cache miss stores the
        scan's ``Task`` in ``index_cache["pending"]`` before awaiting it, so
        every request that arrives while that scan is still running awaits
        the same task instead of launching its own; without this, a page
        load's simultaneous manifest-plus-per-part requests would each pay
        for a full scan during every cold window, and a burst of
        concurrent requests during one cold window would each launch a
        separate walk, all competing for the same default executor that
        file reads and ``/submit`` writes also depend on.
        """
        now = time.monotonic()
        cached = index_cache["index"]
        if cached is not None and now - index_cache["at"] < INDEX_CACHE_TTL:
            return cached
        pending = index_cache["pending"]
        if pending is not None:
            return await pending
        loop = asyncio.get_running_loop()
        task = loop.create_task(_scan_and_cache())
        index_cache["pending"] = task
        try:
            return await task
        finally:
            index_cache["pending"] = None

    def invalidate_index():
        """Drop the cached scan so the next request rescans.

        Called when a model's bytes turn out not to be the ones the scan
        validated, which is what a regenerated file looks like: a CAD script
        writing a new file and renaming it over the old one leaves a different
        inode behind. Rescanning immediately keeps the documented workflow of
        regenerating a model and reloading the page, rather than making the
        page wait out the cache window on a stale identity.
        """
        index_cache["index"] = None
        index_cache["at"] = 0.0

    async def _scan_and_cache():
        loop = asyncio.get_running_loop()
        idx = await loop.run_in_executor(None, paths.build_model_index, serve_dir)
        index_cache["index"] = idx
        index_cache["at"] = time.monotonic()
        return idx

    @app.get("/")
    @app.get("/index.html")
    async def index(req):
        if not VIEWER_HTML.exists():
            return "viewer.html not found in package static dir", 500
        return await file_response(VIEWER_HTML, paths.CONTENT_TYPES[".html"], req.method)

    @app.get("/manifest")
    async def manifest(req):
        midx = await get_index()
        # Every scanned model is listed and every one is fetchable, both by
        # the bare-filename alias and by /model/<rel>, because the scan is
        # flat and a directory cannot hold two entries with one name.
        return {
            "dir": str(serve_dir),
            "models": midx.manifest_models,
            "truncated": midx.truncated,
        }

    @app.get("/model/<path:rel>")
    async def model(req, rel):
        midx = await get_index()
        key = unquote(rel)
        return await _serve_indexed(
            req, rel, lambda idx: (idx.by_rel(key), idx.identity_of(key)),
            lambda target: paths.CONTENT_TYPES.get(
                target.suffix.lower(), "application/octet-stream"))

    # microdot's URLPattern.compile() splits the raw pattern text on '/'
    # before it ever looks at regex syntax, so a custom "re:" segment cannot
    # contain a literal slash character even inside a character class.
    # \x2f stands in for '/' so this still matches "not a directory
    # separator", which is what confines the alias to one path segment. The
    # extension is matched letter-by-letter case-insensitively because the
    # manifest scan indexes ".STL"/".Stl" alongside ".stl" (suffix matching
    # is lower()'d there), and a file listed in the manifest must be
    # reachable through this alias regardless of the case a CAD tool
    # exported it with.
    @app.get(r"/<re:[^\x2f]+\.[sS][tT][lL]:name>")
    async def model_alias(req, name):
        midx = await get_index()
        key = unquote(name)
        return await _serve_indexed(
            req, name, lambda idx: (idx.by_file(key), idx.identity_of_file(key)),
            lambda target: paths.CONTENT_TYPES[".stl"])

    async def _serve_indexed(req, request_key, lookup, content_type_for):
        """Serve model bytes through the index, rescanning once on a mismatch.

        The scan pins each model's inode and the open refuses anything else,
        which is what stops a name being relinked to a file outside the served
        directory between the two. A regenerated model is indistinguishable
        from that at the moment of the open, so one retry after a fresh scan
        separates them: a real regeneration resolves to the new file, while a
        relinked outside file fails the scan's own containment and link checks
        and stays unreachable.
        """
        for attempt in (0, 1):
            idx = await get_index()
            target, identity = lookup(idx)
            if target is None:
                if attempt == 0:
                    invalidate_index()
                    continue
                return "not found: %s" % request_key, 404
            res = await file_response(
                target, content_type_for(target), req.method, identity)
            if res.status_code != 404:
                return res
            if attempt == 0:
                invalidate_index()
        return "not found: %s" % request_key, 404

    @app.get("/asset/<path:rel>")
    async def asset(req, rel):
        found = paths.resolve_asset(serve_dir, unquote(rel))
        if found is None:
            return "not found: %s" % rel, 404
        target, identity = found
        ctype = paths.ASSET_CONTENT_TYPES.get(target.suffix.lower(), "application/octet-stream")
        return await _file_or_same_404(target, ctype, req.method, rel, identity)

    @app.get("/callouts")
    @app.get("/callouts.json")
    async def callouts(req):
        # Agent-authored callouts. The agent writes this file directly; return
        # an empty record (not 404) until it exists so the viewer's poll loop
        # stays quiet rather than logging errors before the first callout.
        # The name is fixed but its directory entry is not, so it goes through
        # the same containment check as any client-supplied path: a symlink
        # left here by a reviewed bundle would otherwise be followed and its
        # target served.
        loop = asyncio.get_running_loop()
        raw = await loop.run_in_executor(
            None, paths.read_fixed_file, serve_dir, paths.CALLOUTS_JSON_NAME)
        if raw is None:
            return {"annotations": []}
        return Response(body=raw, headers={
            "Content-Type": paths.CONTENT_TYPES[".json"],
            "Cache-Control": "no-store",
        })

    @app.post("/submit")
    async def submit(req):
        raw = req.body or b""
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            return {"ok": False, "error": "invalid JSON: %s" % exc}, 400
        if not isinstance(data, list):
            return {"ok": False, "error": "body must be a JSON array"}, 400
        # Every element must be an object: the record writer stores the list
        # verbatim and the console summary indexes into each element with
        # .get(...), so a non-object element would surface as an unhandled
        # exception after the write to disk had already succeeded, rather
        # than as a clean rejection before anything is written.
        if not all(isinstance(item, dict) for item in data):
            return {"ok": False, "error": "every array element must be an object"}, 400

        ts = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
        record = {"submitted_at": ts, "count": len(data), "annotations": data}

        # The record destination is re-checked per request, because os.replace
        # acts on the directory entry and so cannot be redirected; only a
        # non-regular entry needs refusing. The log is a descriptor opened and
        # validated once, because resolving that name per write is a race an
        # attacker with write access to this directory wins.
        safe_json = paths.safe_fixed_file(serve_dir, paths.COMMENTS_JSON_NAME)
        if safe_json is None:
            return {"ok": False,
                    "error": "refusing to write: %s is not a plain, single-linked file"
                             % paths.COMMENTS_JSON_NAME}, 500
        log_fd = await get_log_fd()
        if log_fd is None:
            return {"ok": False,
                    "error": "refusing to write: %s is not a plain, single-linked file"
                             % paths.COMMENTS_LOG_NAME}, 500

        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, _write_comments, record, safe_json, log_fd)
        except OSError as exc:
            return {"ok": False, "error": "write failed: %s" % exc}, 500

        # The write has already succeeded at this point, so a problem
        # printing the console summary must not turn a successful submit
        # into a reported failure; it is logged and swallowed instead.
        try:
            await loop.run_in_executor(None, _print_summary, record, comments_json)
        except Exception as exc:
            sys.stderr.write("warning: could not print submit summary: %s\n" % exc)

        return {"ok": True, "count": len(data), "path": str(comments_json)}


async def _file_or_same_404(target, ctype, method, request_key, expect_identity=None):
    """Stream ``target``, or fall back to the caller's own miss message.

    The caller has already confirmed ``target`` exists as a file; the only
    way ``file_response`` still 404s from here is an open failure (a
    permission error, or the file vanishing in the race between that check
    and this call). That failure must read exactly like the caller's own
    "the path never resolved to anything" 404 for ``request_key`` - not
    file_response's own generic body - so a client cannot use a difference
    in wording to tell "does not exist" apart from "exists but is
    unreadable" for a path it does not have permission to see the real
    answer to.
    """
    res = await file_response(target, ctype, method, expect_identity)
    if res.status_code == 404:
        return "not found: %s" % request_key, 404
    return res


def _write_comments(record, comments_json, log_fd):
    """Write the submission record to disk.

    Blocking filesystem calls; run through an executor by the caller so a
    submit does not stall the event loop's other connections for the
    duration of the write.

    The JSON record is written to a temporary file in the same directory and
    moved into place with ``os.replace``, which is atomic on POSIX and on
    Windows. Two submissions arriving together therefore leave one complete
    record rather than one record's bytes overlaid on another's, and a
    reader never observes a half-written file. The temporary name is
    dot-prefixed so the model scan skips it if a crash leaves one behind.

    ``log_fd`` is a descriptor the caller opened and validated once, and
    holds for the process's lifetime; see
    ``paths.open_fixed_file_for_append`` for why the name is not resolved per
    write. The record needs no such treatment: it goes to a fresh ``mkstemp``
    file and is moved into place with ``os.replace``, which acts on the
    directory entry and writes through neither a symlink nor a hardlink
    sitting at the destination.
    """
    payload = json.dumps(record, indent=2) + "\n"

    fd, tmp_name = tempfile.mkstemp(
        dir=str(comments_json.parent), prefix=".mesh-comments-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        # mkstemp creates 0600 and os.replace carries the mode across, so
        # without this the record would silently narrow on every submit. An
        # existing record keeps whatever mode it already has, so a deliberate
        # choice by the operator survives; a new one gets the default.
        try:
            os.chmod(tmp_name, stat.S_IMODE(
                os.stat(comments_json, follow_symlinks=False).st_mode))
        except OSError:
            os.chmod(tmp_name, _RECORD_FILE_MODE)
        os.replace(tmp_name, comments_json)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise

    # One O_APPEND write per line. O_APPEND makes the seek and the write one
    # operation, so concurrent submissions interleave between lines and never
    # within one. os.write may still write fewer bytes than asked, which would
    # leave a truncated line and make the file unparseable from that point, so
    # the remainder is written until none is left.
    payload_line = (json.dumps(record) + "\n").encode("utf-8")
    written = 0
    while written < len(payload_line):
        written += os.write(log_fd, payload_line[written:])


def _print_summary(record, comments_path):
    """Write a human-readable summary of one submission to stdout.

    A blocking write; run through an executor by the caller. If stdout is a
    pipe with a stalled reader, a summary written directly on the event loop
    would block every other connection this process is serving for as long
    as the pipe stays full. Called only after the write to disk has already
    succeeded; the caller swallows any exception this raises so a
    formatting surprise here cannot turn a successful submit into a
    reported failure.
    """
    line = "=" * 64
    out = sys.stdout
    out.write("\n%s\n" % line)
    out.write("ANNEALAGE MESH COMMENTS SUBMITTED  %s  (%d pins)\n"
              % (record["submitted_at"], record["count"]))
    out.write("wrote: %s\n" % comments_path)
    out.write("%s\n" % line)
    for a in record["annotations"]:
        n = a.get("id", "?")
        part = a.get("part", "?")
        label = a.get("label", "?")
        p = a.get("point", [None, None, None])
        try:
            loc = "[% .1f, % .1f, % .1f]" % (p[0], p[1], p[2])
        except (TypeError, IndexError):
            loc = str(p)
        comment = (a.get("comment") or "").strip() or "(no comment)"
        out.write("#%s  %s  %s  @ %s mm\n" % (n, part, label, loc))
        out.write("     %s\n" % comment)
    out.write("%s\n\n" % line)
    out.flush()
