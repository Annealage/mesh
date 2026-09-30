"""Route handlers for the packaged viewer and the STL/callout file contract.

Registers, against one served directory:

    GET  /, /index.html          the packaged viewer shell, resolved through
                                  the static asset index like any other file
                                  under this package's static/ directory
    GET  /static/<path:rel>       any file in the package's static/ tree,
                                  resolved through the static asset index
                                  (the agent layer's own front end is served
                                  separately, under /agent/static/)
    GET  /style/<path:rel>        the shared brand styling (tokens, theme,
                                  fonts, icons, marks), resolved through its
                                  own static asset index over STYLE_DIR
    GET  /manifest                model listing, rescanned at most once per
                                  INDEX_CACHE_TTL (annealage_agent/http/static.py) seconds
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
    GET  /callouts, /callouts.json   agent-authored callouts (read-only here)
    POST /submit                  human pin-comment submissions

Files this module writes:
    mesh-comments.json   overwritten on every /submit
    mesh-comments.log    appended on every /submit

Files this module reads but never writes:
    mesh-callouts.json   written by an agent directly; served verbatim

Deliberately absent: any route that serves a file under the served
directory that the manifest scan did not list. The one route that does serve
unindexed files, ``/asset`` for the images/ subtree, is the agent layer's
(``annealage_agent/http/routes_chat.py``), because uploads and captures write there.
The viewer's own assets come only from this package's static/ directory,
resolved through the static asset index, never from the served directory.
"""

import asyncio
import datetime
import json
import os
import sys
from pathlib import Path
from urllib.parse import unquote

from annealage_agent import files
from annealage_agent.http import CHUNK_SIZE, Response
from annealage_agent.http.static import make_index_cache, serve_indexed, static_tree
from annealage_agent.http.ws import _origin_is_allowed, _token_is_allowed, refusal

from .. import paths

# Mode for a submission file this process creates. An existing file's own
# mode is preserved instead; see _write_comments.
_RECORD_FILE_MODE = 0o644

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

# The shared brand styling, the lib/style submodule. A wheel carries the files
# it ships as annealage_mesh/style (hatch_build.py); a checkout has no such
# directory and serves the submodule where it sits, so an edit there shows up
# on the next request without a copy step in between.
_PACKAGED_STYLE_DIR = Path(__file__).resolve().parent.parent / "style"
STYLE_DIR = (
    _PACKAGED_STYLE_DIR
    if _PACKAGED_STYLE_DIR.is_dir()
    else Path(__file__).resolve().parents[3] / "lib" / "style"
)
# Exists for the CLI's own startup check, so a missing packaged viewer.html
# is reported before a socket is even opened. It is not a route input: the
# index route resolves "viewer.html" through the static asset index like
# every other packaged asset, so the shell is served under the same
# containment, identity-pinning and content-type rules as the modules it
# loads.
VIEWER_HTML = STATIC_DIR / "viewer.html"


def register_routes(app, serve_dir, *, token=None, allowed_origins=(), require_token=False):
    """Register the viewer routes on ``app`` for one served directory.

    A fresh set of closures per call, so independent apps (independent
    served directories, as in the test suite) never share route state or
    either index cache: the model index is naturally per-``serve_dir``, and
    the static index, though its target directory is the same for every
    app, still gets its own cache per call rather than a module-level one
    shared across them.

    ``token`` and ``allowed_origins`` gate ``POST /submit``, which is the one
    route here that writes into the human's project. The two are applied
    differently and deliberately.

    The ``Origin`` check applies in **every** mode. ``mesh-comments.json`` is
    the documented human-to-agent channel and ``SKILL.md`` tells an agent to
    re-read it, so text written there reaches an agent holding a shell; a page
    the human happens to have open in another tab must not be able to put text
    in it. A POST carrying a plain body is a CORS-simple request, so no
    preflight stands in front of it and the browser's refusal to let the page
    read the reply is no protection at all when the write itself is the payload.
    A browser attaches ``Origin`` to a cross-origin POST, including a form
    submission, and a page cannot suppress it.

    ``require_token`` is set for agent mode alone (plan section 3.10). Viewer-only
    mode leaves the token off this route because the published skill flow tells
    the human to open the bare ``http://localhost:8765/`` and press Submit, and
    breaking that is not worth what the token adds on top of the ``Origin``
    check for a run with no agent attached to it.
    """
    serve_dir = files.resolve_serve_dir(serve_dir)
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
                    files.open_fixed_file_for_append,
                    serve_dir,
                    paths.COMMENTS_LOG_NAME,
                    _RECORD_FILE_MODE,
                )
            return log_state["fd"]

    get_index, invalidate_index = make_index_cache(paths.build_model_index, serve_dir)
    # Published so the models watcher can drop the cached scan at the moment it
    # decides the directory changed. Without that, the watcher's push races the
    # cache: the browser refetches ``/manifest`` immediately, is served the scan
    # from before the change, and since one event has already been sent there is
    # nothing left to prompt another fetch. A part added during a session would
    # then stay invisible until something else happened to change.
    app.mesh_invalidate_model_index = invalidate_index
    serve_static = static_tree(STATIC_DIR)
    serve_style = static_tree(STYLE_DIR)

    @app.get("/")
    @app.get("/index.html")
    async def index(req):
        # Resolved through the static index like any other packaged asset,
        # rather than a hardcoded path, so the shell gets the same identity
        # pinning (a rebuild during development that replaces viewer.html
        # with a new inode is served after one rescan, not 404ed) and the
        # same content-type lookup as everything under /static.
        return await serve_static(req, "viewer.html")

    @app.get("/static/<path:rel>")
    async def static_asset(req, rel):
        return await serve_static(req, unquote(rel), rel)

    @app.get("/style/<path:rel>")
    async def style_asset(req, rel):
        return await serve_style(req, unquote(rel), rel)

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
        return await serve_indexed(
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
        return await serve_indexed(
            get_index,
            invalidate_index,
            req,
            name,
            lambda idx: (idx.by_file(key), idx.identity_of_file(key)),
            lambda target: paths.CONTENT_TYPES[".stl"],
        )

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
            None, files.read_fixed_file, serve_dir, paths.CALLOUTS_JSON_NAME
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
        # Before the body is read, and in the same order /upload uses, so
        # neither check tells an unauthenticated caller which one it failed.
        if require_token and not _token_is_allowed(req, token):
            return refusal()
        if not _origin_is_allowed(req, allowed_origins):
            return refusal()
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
        safe_json = files.safe_fixed_file(serve_dir, paths.COMMENTS_JSON_NAME)
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


def _write_comments(record, comments_json, log_fd):
    """Write the submission record to disk.

    Blocking filesystem calls; run through an executor by the caller so a
    submit does not stall the event loop's other connections for the
    duration of the write.

    The JSON record goes through ``files.atomic_replace``, so two submissions
    arriving together leave one complete record rather than one record's bytes
    overlaid on another's, and a reader never observes a half-written file.

    ``log_fd`` is a descriptor the caller opened and validated once, and
    holds for the process's lifetime; see
    ``files.open_fixed_file_for_append`` for why the name is not resolved per
    write. The record needs no such treatment: it goes to a fresh temporary
    file and is moved into place with ``os.replace``, which acts on the
    directory entry and writes through neither a symlink nor a hardlink
    sitting at the destination.
    """
    payload = json.dumps(record, indent=2) + "\n"
    files.atomic_replace(comments_json, payload.encode("utf-8"), _RECORD_FILE_MODE)

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
