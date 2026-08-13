"""Route handlers for the packaged viewer and the STL/callout file contract.

Registers, against one served directory:

    GET  /, /index.html          the packaged viewer shell, resolved through
                                  the static asset index like any other file
                                  under this package's static/ directory
    GET  /static/<path:rel>       any file in the package's static/ tree,
                                  resolved through the static asset index
    GET  /manifest                model listing, rescanned at most once per
                                  INDEX_CACHE_TTL seconds
    GET  /model/<path:rel>        model bytes, resolved through the manifest
                                  index; the key the recursive scan makes
                                  unique, so this is the only route every
                                  indexed model is guaranteed reachable by
    GET  /<name>.stl              compatibility alias into the manifest
                                  index, by bare filename (case-insensitive
                                  extension); 404s for a name that is not in
                                  the index or that two or more indexed
                                  models share, since a recursive scan can
                                  index two models of the same bare filename
                                  in different directories
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
directory, resolved through the static asset index, never from the served
directory.
"""

import asyncio
import datetime
import json
import os
import stat
import sys
import time
from pathlib import Path
from urllib.parse import unquote

from .. import paths
from . import CHUNK_SIZE, Response, file_response, not_modified

# Mode for a submission file this process creates. An existing file's own
# mode is preserved instead; see _write_comments.
_RECORD_FILE_MODE = 0o644

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
# Exists for the CLI's own startup check, so a missing packaged viewer.html
# is reported before a socket is even opened. It is not a route input: the
# index route resolves "viewer.html" through the static asset index like
# every other packaged asset, so the shell is served under the same
# containment, identity-pinning and content-type rules as the modules it
# loads.
VIEWER_HTML = STATIC_DIR / "viewer.html"

# How long a scanned index (the model index for the served directory, or the
# static index for this package's own static/ tree) is reused before the
# next request triggers a fresh scan. One page load fetches the manifest and
# then one model per visible part, plus the shell, the stylesheet and every
# script the shell imports; without this, that single page load would pay
# for one full walk per file instead of one walk shared across all of them.
# The trade this makes: a model added, removed or changed on disk, or a
# static file edited during development, can take up to this long to be
# reflected, rather than appearing on the very next request.
INDEX_CACHE_TTL = 1.0


def _make_index_cache(build, target):
    """Return a ``(get, invalidate)`` pair caching one ``build(target)`` scan.

    ``build`` is ``paths.build_model_index`` or ``paths.build_static_index``,
    called with ``target`` (a served directory or the package's static
    directory) off the event loop, its result reused for up to
    ``INDEX_CACHE_TTL`` seconds. This is the one caching implementation both
    ``/model``, the ``.stl`` alias, ``/static`` and the packaged shell route
    through, each with its own call to this factory rather than a second copy
    of the logic, since the shape (scan off-loop, cache with a TTL, share one
    in-flight scan, allow an explicit invalidate) is identical for a served
    project's models and this package's own static files; only what gets
    scanned differs.

    Only one scan is ever in flight at a time. A cache miss stores the
    scan's ``Task`` before awaiting it, so every request that arrives while
    that scan is still running awaits the same task instead of launching its
    own; without this, a page load's simultaneous manifest-plus-per-part (or
    shell-plus-every-script) requests would each pay for a full scan during
    every cold window, and a burst of concurrent requests during one cold
    window would each launch a separate walk, all competing for the same
    default executor that file reads and ``/submit`` writes also depend on.
    """
    cache = {"index": None, "at": 0.0, "pending": None}

    async def _scan_and_cache():
        loop = asyncio.get_running_loop()
        idx = await loop.run_in_executor(None, build, target)
        cache["index"] = idx
        cache["at"] = time.monotonic()
        return idx

    async def get():
        now = time.monotonic()
        cached = cache["index"]
        if cached is not None and now - cache["at"] < INDEX_CACHE_TTL:
            return cached
        pending = cache["pending"]
        if pending is not None:
            return await pending
        loop = asyncio.get_running_loop()
        task = loop.create_task(_scan_and_cache())
        cache["pending"] = task
        try:
            return await task
        finally:
            cache["pending"] = None

    def invalidate():
        """Drop the cached scan so the next request rescans.

        Called when a file's bytes turn out not to be the ones the scan
        validated, which is what a regenerated file looks like: a build step
        or a CAD script writing a new file and renaming it over the old one
        leaves a different inode behind. Rescanning immediately keeps the
        documented workflow of regenerating a model, or editing a script
        during development, and reloading the page, rather than making the
        page wait out the cache window on a stale identity.
        """
        cache["index"] = None
        cache["at"] = 0.0

    return get, invalidate


def _lstat_or_none(path):
    """``os.lstat`` returning None instead of raising, for a file that may have
    gone between an index scan and now."""
    try:
        return os.lstat(path)
    except OSError:
        return None


async def _maybe_not_modified(req, target):
    """Return a 304 if the client's ``If-None-Match`` matches ``target``, else None.

    The tag is computed from a fresh ``lstat`` of the indexed path rather than
    from whatever the cached scan recorded, so a file edited within the index
    cache window is never affirmed as unchanged. That costs one stat per
    conditional request, which is what a megabyte of vendored JavaScript is
    worth avoiding on a phone over a tailnet.

    ``lstat`` rather than ``stat``: a symlink appearing at an indexed name is
    refused here for the same reason the open path refuses it, so this cannot
    become a way to have a link's target validated. A tag mismatch simply
    falls through to the ordinary serve path, which applies the full open-time
    identity check, so a wrong or stale client tag costs a transfer and never
    a wrong answer.
    """
    header = req.headers.get("If-None-Match")
    if not header:
        return None
    loop = asyncio.get_running_loop()
    st = await loop.run_in_executor(None, _lstat_or_none, target)
    if st is None or not stat.S_ISREG(st.st_mode):
        return None
    tags = [t.strip() for t in header.split(",")]
    # "*" asks for a 304 if any representation exists at all, which one
    # confirmed here does.
    if "*" in tags or paths.file_validator(st) in tags:
        return not_modified(st)
    return None


async def _serve_indexed(
    get_idx, invalidate, req, request_key, lookup, content_type_for, revalidatable=False
):
    """Serve one file through a cached index, rescanning once on a mismatch.

    ``get_idx`` and ``invalidate`` come from ``_make_index_cache``, so this
    function has no idea whether it is serving a model or a packaged static
    asset; ``lookup(idx)`` returns the ``(target, identity)`` pair for
    whichever index it was given, and everything below is the retry policy
    the two cases share.

    The scan pins each file's inode and the open refuses anything else,
    which is what stops a name being relinked to a file outside the served
    directory between the two. A regenerated file is indistinguishable from
    that at the moment of the open, so one retry after a fresh scan
    separates them: a real regeneration resolves to the new file, while a
    relinked outside file fails the scan's own containment and link checks
    and stays unreachable.

    ``revalidatable`` is set by the routes serving this package's own assets,
    which are large, change only when the package does, and are fetched again
    on every page load. It is not set for model bytes; see ``file_response``.
    """
    for attempt in (0, 1):
        idx = await get_idx()
        target, identity = lookup(idx)
        if target is None:
            if attempt == 0:
                invalidate()
                continue
            return "not found: %s" % request_key, 404
        if revalidatable:
            unchanged = await _maybe_not_modified(req, target)
            if unchanged is not None:
                return unchanged
        res = await file_response(
            target, content_type_for(target), req.method, identity, revalidatable
        )
        if res.status_code != 404:
            return res
        if attempt == 0:
            invalidate()
    return "not found: %s" % request_key, 404


def register_routes(app, serve_dir):
    """Register the viewer routes on ``app`` for one served directory.

    A fresh set of closures per call, so independent apps (independent
    served directories, as in the test suite) never share route state or
    either index cache: the model index is naturally per-``serve_dir``, and
    the static index, though its target directory is the same for every
    app, still gets its own cache per call rather than a module-level one
    shared across them.
    """
    serve_dir = paths.resolve_serve_dir(serve_dir)
    comments_json = paths.comments_path(serve_dir)

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
                    None,
                    paths.open_fixed_file_for_append,
                    serve_dir,
                    paths.COMMENTS_LOG_NAME,
                    _RECORD_FILE_MODE,
                )
            return log_state["fd"]

    get_index, invalidate_index = _make_index_cache(paths.build_model_index, serve_dir)
    # Published so the models watcher can drop the cached scan at the moment it
    # decides the directory changed. Without that, the watcher's push races the
    # cache: the browser refetches ``/manifest`` immediately, is served the scan
    # from before the change, and since one event has already been sent there is
    # nothing left to prompt another fetch. A part added during a session would
    # then stay invisible until something else happened to change.
    app.mesh_invalidate_model_index = invalidate_index
    get_static_index, invalidate_static_index = _make_index_cache(
        paths.build_static_index, STATIC_DIR
    )

    @app.get("/")
    @app.get("/index.html")
    async def index(req):
        # Resolved through the static index like any other packaged asset,
        # rather than a hardcoded path, so the shell gets the same identity
        # pinning (a rebuild during development that replaces viewer.html
        # with a new inode is served after one rescan, not 404ed) and the
        # same content-type lookup as everything under /static.
        return await _serve_indexed(
            get_static_index,
            invalidate_static_index,
            req,
            "viewer.html",
            lambda idx: (idx.by_rel("viewer.html"), idx.identity_of("viewer.html")),
            lambda target: paths.CONTENT_TYPES[".html"],
            revalidatable=True,
        )

    @app.get("/static/<path:rel>")
    async def static_asset(req, rel):
        key = unquote(rel)
        return await _serve_indexed(
            get_static_index,
            invalidate_static_index,
            req,
            rel,
            lambda idx: (idx.by_rel(key), idx.identity_of(key)),
            lambda target: paths.StaticIndex.content_type_of(key),
            revalidatable=True,
        )

    @app.get("/manifest")
    async def manifest(req):
        midx = await get_index()
        # Every scanned model is listed and always fetchable by /model/<rel>,
        # because rel is the one field the scan guarantees unique. The
        # bare-filename alias is narrower: it works only for a model whose
        # filename no other indexed model shares, which ModelIndex.by_file
        # already enforces, so nothing further is needed here.
        return {
            "dir": str(serve_dir),
            "models": midx.manifest_models,
            "truncated": midx.truncated,
        }

    @app.get("/model/<path:rel>")
    async def model(req, rel):
        key = unquote(rel)
        return await _serve_indexed(
            get_index,
            invalidate_index,
            req,
            rel,
            lambda idx: (idx.by_rel(key), idx.identity_of(key)),
            lambda target: paths.CONTENT_TYPES.get(
                target.suffix.lower(), "application/octet-stream"
            ),
        )

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
        key = unquote(name)
        return await _serve_indexed(
            get_index,
            invalidate_index,
            req,
            name,
            lambda idx: (idx.by_file(key), idx.identity_of_file(key)),
            lambda target: paths.CONTENT_TYPES[".stl"],
        )

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
            None, paths.read_fixed_file, serve_dir, paths.CALLOUTS_JSON_NAME
        )
        if raw is None:
            return {"annotations": []}
        return Response(
            body=raw,
            headers={
                "Content-Type": paths.CONTENT_TYPES[".json"],
                "Cache-Control": "no-store",
            },
        )

    @app.post("/submit")
    async def submit(req):
        # The body is never buffered (app.py sets max_body_length to 0), so
        # it is read off req.stream here rather than through req.body. The
        # length check comes first and unconditionally: reading the stream
        # with no declared length would, on a real connection, read
        # forever, since req.stream is then the raw client reader and
        # nothing ever makes such a read return.
        content_length = req.content_length
        if content_length <= 0:
            return {
                "ok": False,
                "error": "Content-Length is required and must be greater than zero",
            }, 411
        chunks = []
        total = 0
        while total < content_length:
            chunk = await req.stream.read(min(CHUNK_SIZE, content_length - total))
            if not chunk:
                return {
                    "ok": False,
                    "error": "body ended after %d of %d bytes" % (total, content_length),
                }, 400
            chunks.append(chunk)
            total += len(chunk)
        raw = b"".join(chunks)
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
            return {
                "ok": False,
                "error": "refusing to write: %s is not a plain, single-linked file"
                % paths.COMMENTS_JSON_NAME,
            }, 500
        log_fd = await get_log_fd()
        if log_fd is None:
            return {
                "ok": False,
                "error": "refusing to write: %s is not a plain, single-linked file"
                % paths.COMMENTS_LOG_NAME,
            }, 500

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

    The JSON record goes through ``paths.atomic_replace``, so two submissions
    arriving together leave one complete record rather than one record's bytes
    overlaid on another's, and a reader never observes a half-written file.

    ``log_fd`` is a descriptor the caller opened and validated once, and
    holds for the process's lifetime; see
    ``paths.open_fixed_file_for_append`` for why the name is not resolved per
    write. The record needs no such treatment: it goes to a fresh temporary
    file and is moved into place with ``os.replace``, which acts on the
    directory entry and writes through neither a symlink nor a hardlink
    sitting at the destination.
    """
    payload = json.dumps(record, indent=2) + "\n"
    paths.atomic_replace(comments_json, payload.encode("utf-8"), _RECORD_FILE_MODE)

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
    out.write(
        "ANNEALAGE MESH COMMENTS SUBMITTED  %s  (%d pins)\n"
        % (record["submitted_at"], record["count"])
    )
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
