"""Builds the microdot application and owns its asyncio startup and shutdown.

``create_app`` wires one served directory's routes onto a fresh ``Microdot``
instance; a fresh instance per call means independent served directories
never share route state, which matters for tests. ``run`` binds the socket,
hands control back to the caller once it is actually listening, then serves
until interrupted and closes the listener before returning.
"""

import asyncio
import hashlib
import inspect
import json
import os
import pathlib
import sys
import time

from microdot import Microdot, Request

from . import __version__, net, paths, protocol, sessions, stl
from .http.routes_viewer import register_routes
from .http.ws import host_is_allowed, ping_forever, refusal, register_ws
from .session.base import CalloutsChanged, ModelsChanged
from .session.events import EventLog
from .viewers import ViewerRegistry

# The port used when a caller does not name one. Kept here rather than only in
# argparse so an app built directly, as the tests do, computes the same
# Origin and Host allowlists the command line would.
DEFAULT_PORT = 8765

# Upper bound on how long shutdown waits for in-flight requests to drain
# once the listening socket has stopped accepting new connections. Past
# this, the process exits with those requests abandoned rather than blocking
# until they finish: an interrupt during a large in-progress model transfer
# must return control in a couple of seconds, not stall until that transfer
# completes.
SHUTDOWN_DRAIN_TIMEOUT = 2.0

# microdot's request body limits are class attributes on ``Request`` and
# therefore process-global: raising them (done in configure_request_limits,
# called from create_app) affects every route this process ever serves, not
# only /submit, and every other microdot app or test sharing this
# interpreter. 8 MiB comfortably covers a full-page pin review (microdot's
# 16 KiB default caps a submission at roughly 68 pins of the {id, part,
# label, point, normal, faceIndex, comment} shape the viewer sends): a
# 400-pin submission measures about 84 KB, so 8 MiB is far more headroom
# than the real payload needs. The actual exposure of that headroom is per
# in-flight request, not aggregate: microdot buffers a request's whole body
# before its handler runs and imposes no cap on concurrent connections and
# no read timeout, so N slow or stalled clients each declaring a large
# Content-Length can together hold N times this many bytes for as long as
# they keep their sockets open. That aggregate exposure is unbounded; capping
# it needs a connection limit or a read timeout, and this module has
# neither. Both attributes move together:
# max_body_length gates whether microdot buffers the body into req.body at
# all, and /submit reads req.body directly rather than streaming req.stream.
MAX_REQUEST_BODY = 8 * 1024 * 1024


# How often the callouts file is checked, and how long a file that keeps
# changing without ever parsing is waited on before an event is pushed
# anyway. One stat-and-read of a small JSON file every quarter second is
# cheaper than an inotify dependency and behaves the same on every platform.
CALLOUTS_POLL_INTERVAL = 0.25
CALLOUTS_MAX_DEFER = 5.0


def configure_request_limits():
    """Raise microdot's process-global request body limits to MAX_REQUEST_BODY.

    Called from create_app rather than run at import time, so the global
    mutation happens as a deliberate step tied to building an app, not as a
    side effect of merely importing this module.
    """
    Request.max_content_length = MAX_REQUEST_BODY
    Request.max_body_length = MAX_REQUEST_BODY


def create_app(serve_dir, *, token=None, host=net.DEFAULT_HOST, port=DEFAULT_PORT,
               extra_origins=(), mesh_session_id=None, build_session=None):
    """Build a Microdot app serving ``serve_dir``, routes registered, not started.

    ``host`` is an address already resolved (``net.resolve_bind`` does the
    resolving, in ``cli.py``), and together with ``port`` and
    ``extra_origins`` it decides the exact ``Origin`` and ``Host`` values this
    app accepts. Computing those here, from the bind, is what lets a remote
    viewer work at all: an allowlist hardcoded to localhost would refuse
    every tailnet client.

    ``token`` of ``None`` means no token was configured, and ``/ws`` then
    refuses every request. The read-only routes stay open either way, because
    gating them buys nothing: their exposure was closed by deleting the
    serve-anything fallback, not by a secret.

    ``mesh_session_id`` is the id ``cli.py`` resolved for this run (fresh or
    resumed, per plan section 3.4); it is only ever reported in the
    ``hello`` frame's ``session`` object here, never used to construct an
    agent, since no ``AgentSession`` is built by this function.
    """
    configure_request_limits()
    bind = net.bind_from_address(host)
    allowed_origins = net.allowed_origins(bind, port, extra_origins)
    allowed_hosts = net.allowed_hosts(bind, port)

    app = Microdot()
    app.mesh_token = token
    app.mesh_bind = bind
    app.mesh_port = port

    @app.before_request
    async def _check_host(req):
        # Every route, not only /ws: a rebound DNS name can read /manifest
        # and write through /submit as readily as it can open a socket. A
        # before_request handler that returns a value short-circuits the
        # route entirely, so a refused request never reaches a handler.
        if not host_is_allowed(req, allowed_hosts):
            return refusal()

    register_routes(app, serve_dir)

    # Set after the session exists, since the broker it belongs to is built by
    # the session factory below; a list with one slot rather than a nonlocal so
    # the closure reads the current value instead of capturing None.
    presence_listener = []

    def _presence(count):
        for listen in presence_listener:
            listen(count)

    # Given a path in agent mode, so the conversation survives the process. The
    # 500-event ring alone covers a browser reconnecting; it is this file that
    # lets `-c` resume a session and `-r` report what a session cost, both of
    # which read it back off disk. Viewer-only mode has no session and no
    # conversation, so it gets a ring and nothing on disk.
    event_log = EventLog(
        str(sessions.events_path(serve_dir, mesh_session_id))
        if mesh_session_id is not None else None)
    registry = ViewerRegistry(event_log=event_log, on_presence=_presence)
    session_info = {
        "id": mesh_session_id if mesh_session_id is not None else "viewer-only",
        "sdk_session_id": None,
        "cwd": str(serve_dir),
        "agent": "unavailable",
    }
    app.mesh_registry = registry
    app.mesh_event_log = event_log

    # ``build_session`` is called with the callback a session must use to
    # publish an event, and returns the session or None for viewer-only. It is
    # a factory rather than a constructed object so that the event callback,
    # which needs the registry and the log built above, exists before the
    # session that will call it, without either module importing the other.
    session = None
    if build_session is not None:
        session = build_session(_event_publisher(registry, event_log))
        presence_listener.append(session.on_viewer_presence)
    app.mesh_session = session
    if session is not None:
        # The hello frame publishes whatever the session currently knows, so a
        # tab that connects later sees a ready agent rather than the
        # connecting state this dict was built with.
        session_info["agent"] = session.agent_status()

    register_ws(app, token=token, allowed_origins=allowed_origins,
                allowed_hosts=allowed_hosts, registry=registry,
                event_log=event_log, session_info=session_info, session=session)

    @app.errorhandler(413)
    async def _payload_too_large(req):
        # microdot's own 413 (request body over Request.max_content_length)
        # is a bare text/plain response; every other failure on /submit
        # returns {"ok": false, "error": ...}, so this keeps that contract
        # for the one failure mode the route handler itself never sees.
        return {"ok": False, "error": "request body exceeds the %d byte limit"
                % Request.max_content_length}, 413

    async def _access_log(req, res):
        # One line per request to stderr, independent of stdout (used for
        # the startup banner and the /submit summary), so a server reachable
        # from a remote or Tailscale-bound address gives visible evidence
        # that traffic is arriving even when the human never opens a
        # browser tab locally. ``req`` is None when the request line itself
        # could not be parsed, in which case there is nothing to report but
        # the failure.
        if req is None:
            sys.stderr.write("  ? - \"?\" %s -\n" % res.status_code)
            return res
        addr = req.client_addr[0] if req.client_addr else "-"
        sys.stderr.write("  %s - \"%s %s HTTP/%s\" %s -\n" % (
            addr, req.method, req.path, req.http_version, res.status_code))
        return res

    async def _no_store(req, res):
        # Every response here is either live data (manifest, callouts) or a
        # file that may change between requests (a model regenerated on
        # disk); nothing served should ever be cached by the browser. This
        # runs as both after_request and after_error_request, since microdot
        # only routes a response through the first of those two lists, never
        # both, depending on whether the route raised.
        if "Cache-Control" not in res.headers:
            res.headers["Cache-Control"] = "no-store"
        if "Server" not in res.headers:
            res.headers["Server"] = "annealage-mesh/%s" % __version__
        # Without this, a browser may sniff a mislabelled asset's bytes and
        # render it as HTML or SVG regardless of the Content-Type this
        # process sent, which is exactly the content-type restriction the
        # /asset route relies on to keep uploaded images from executing as
        # script on this origin.
        if "X-Content-Type-Options" not in res.headers:
            res.headers["X-Content-Type-Options"] = "nosniff"
        return res

    app.after_request(_access_log)
    app.after_error_request(_access_log)
    app.after_request(_no_store)
    app.after_error_request(_no_store)

    return app


def _event_publisher(registry, event_log):
    """Return the ``on_event`` callback a session publishes through.

    Appending to the log and broadcasting are one action, not two, and the
    order matters: the seq comes from the log, so it has to be assigned before
    the frame carrying it can be built. A session calls this synchronously from
    its message pump, which is not a place that can await, so the broadcast is
    scheduled as a task rather than awaited here.

    A broadcast that fails must not stop the log from having recorded the
    event: the log is what a reconnecting browser replays from, so an event
    that reached the log but no live socket is recoverable, while the reverse
    is a hole in the history.
    """
    def publish(event):
        seq = event_log.append(event)
        frame = protocol.build_event(seq, event.to_wire())
        asyncio.ensure_future(registry.broadcast(frame))
    return publish


def _callouts_state(serve_dir):
    """Return ``(digest, parses)`` for the callouts file as it is right now.

    The change signal is a digest of the bytes actually read, not the file's
    size and modification time. A digest cannot miss a rewrite that happens to
    preserve both, and it removes the gap between deciding a file changed and
    reading what it changed to, because there is only one read. Reading goes
    through ``paths.read_fixed_file``, so a symlink, a hard link or a FIFO left
    at that name is refused here exactly as it is on the ``/callouts`` route.

    ``parses`` is the readiness test. The agent writes this file directly and
    the published skill does not require it to do so atomically, so a change
    can be observed mid-write. Stability of a stat signature across two
    samples does not prove a write finished: a same-size rewrite, or a stall
    inside one ``write`` call, produces two identical samples of an
    incomplete file. Whether the bytes parse as JSON does prove it, for every
    incomplete write that is not itself coincidentally valid JSON.
    """
    raw = paths.read_fixed_file(serve_dir, paths.CALLOUTS_JSON_NAME)
    if raw is None:
        return None, True
    digest = hashlib.sha256(raw).hexdigest()
    try:
        json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return digest, False
    return digest, True


class CalloutsWatcher:
    """Pushes ``callouts_changed`` when the callouts file's contents change.

    This is what replaces the browser's own 1.5 s poll. The event carries no
    payload and the browser refetches ``/callouts`` for itself, because pushing
    the content would give that state two writers in the page, which the
    front-end store contract exists to prevent.

    The state machine is ``tick``, which takes the current time as a parameter
    rather than reading a clock, and the sleeping loop is ``run``. Splitting
    them is what lets the deferral rule below be tested by calling ``tick``
    with the times a test chooses, instead of by sleeping and hoping.
    """

    def __init__(self, serve_dir, registry, event_log,
                 interval=CALLOUTS_POLL_INTERVAL, max_defer=CALLOUTS_MAX_DEFER):
        self._serve_dir = serve_dir
        self._registry = registry
        self._event_log = event_log
        self._interval = interval
        self._max_defer = max_defer
        # The first tick records what it finds and announces nothing: this
        # watcher reports changes since it started, and the state it starts in
        # is already covered by the page's own fetch on load. Without that,
        # every start with a callouts file already present would push an event
        # telling the browser to refetch what it had just fetched, and every
        # start without one would push an event about a file that does not
        # exist. ``None`` is a real announced value, meaning "absent", so a
        # deletion is a change; that is why this is a separate flag rather
        # than a sentinel in ``_announced``.
        self._primed = False
        self._announced = None
        self._unstable_since = None

    async def tick(self, now):
        """Sample the file once and broadcast if it changed and looks settled.

        Returns True if an event was broadcast, which is what the tests assert
        on. A change whose bytes do not yet parse is waited on rather than
        announced, but only up to ``max_defer``: a file being rewritten
        continuously never looks settled, and a watcher that waits for quiet
        that never comes is a watcher that never fires. Past that bound the
        event goes out anyway, which is safe because the browser tolerates a
        callouts fetch that fails to parse and the next change produces
        another event.
        """
        loop = asyncio.get_running_loop()
        try:
            digest, parses = await loop.run_in_executor(
                None, _callouts_state, self._serve_dir)
        except OSError:
            # A read that fails outright (a permission change, a directory
            # replacing the file) is left for the next tick rather than
            # treated as a change: there is nothing to tell the browser to
            # refetch, and /callouts will report the same failure itself.
            return False
        if not self._primed:
            self._primed = True
            self._announced = digest
            return False
        if digest == self._announced:
            self._unstable_since = None
            return False
        if not parses:
            if self._unstable_since is None:
                self._unstable_since = now
            if now - self._unstable_since < self._max_defer:
                return False
        self._announced = digest
        self._unstable_since = None
        event = CalloutsChanged()
        seq = self._event_log.append(event)
        await self._registry.broadcast(protocol.build_event(seq, event.to_wire()))
        return True

    async def run(self):
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(self._interval)
            await self.tick(loop.time())


# How recently a model must have been written for its timestamp to be treated
# as inconclusive. A filesystem updates mtime at its own granularity, and two
# writes landing inside one granule share a timestamp exactly; a parametric edit
# that moves coordinates without changing the triangle count also preserves the
# size, so such a pair is invisible to size and mtime together. Two seconds is
# far longer than any real granularity and is only ever the window in which a
# digest is computed at all.
_MTIME_INCONCLUSIVE_WINDOW = 2.0

# Read in chunks so hashing a large model does not hold its whole contents in
# memory alongside everything else this process is doing.
_DIGEST_CHUNK = 1 << 20


def _models_signature(serve_dir, previous=None, now=None):
    """``{rel: (size, mtime_ns, digest)}`` for every model ``/manifest`` would list.

    ``scan_models`` supplies the listing, so this sees exactly the files the
    manifest does, with the same exclusions: dotdirs, symlinks and the scan caps
    all apply here for free rather than by being restated.

    Size and modification time carry the signal, and a content digest settles
    the case they cannot. An STL is routinely megabytes and this is sampled
    several times a second, so hashing every model on every tick would cost
    orders of magnitude more than the question is worth; hashing none of them
    would miss a regeneration that preserved both size and timestamp, which is
    not hypothetical, since a parametric edit that moves coordinates without
    changing the triangle count preserves the size exactly.

    So a digest is computed only when this cannot already tell: for a model that
    is new, one whose size or timestamp moved, and one whose timestamp is recent
    enough to still be inside its own granule. For everything else, and that is
    the steady state, ``previous``'s digest is carried forward untouched and no
    bytes are read at all.
    """
    models, _truncated = paths.scan_models(serve_dir)
    previous = previous or {}
    now = time.time() if now is None else now
    signature = {}
    for model in models:
        rel = model["rel"]
        try:
            st = os.stat(model["path"])
        except OSError:
            # Vanished between the scan and the stat, which is a change in its
            # own right; leaving it out of the signature is what records that.
            continue
        stat_key = (st.st_size, st.st_mtime_ns)
        was = previous.get(rel)
        settled = now - (st.st_mtime_ns / 1e9) > _MTIME_INCONCLUSIVE_WINDOW
        if was is not None and was[:2] == stat_key and settled:
            digest = was[2]
        else:
            digest = _file_digest(model["path"])
        signature[rel] = stat_key + (digest,)
    return signature


def _file_digest(path):
    """A digest of ``path``'s bytes, or None if it could not be read.

    None rather than raising: an unreadable model is the next tick's problem,
    and it compares unequal to a digest, so it is treated as a change rather
    than as proof of no change.
    """
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(_DIGEST_CHUNK), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def _model_is_complete(serve_dir, rel):
    """Whether ``rel``'s bytes currently form a readable STL.

    The readiness test, and the reason it is the same reader the rest of the
    tool uses rather than a cheaper lookalike: a second opinion about what
    counts as a valid STL would eventually disagree with the first, and the
    disagreement would show up as a model the watcher announced and the viewer
    could not load.

    Only called for a file whose stat just changed, so the cost is paid per
    change and not per poll.
    """
    try:
        stl.read_stl_facts(str(pathlib.Path(serve_dir) / rel))
    except (stl.StlError, OSError):
        return False
    return True


class ModelsWatcher:
    """Pushes ``models_changed`` when a model is added, regenerated or removed.

    Shares ``CalloutsWatcher``'s shape deliberately, including the split between
    ``tick`` (which is handed the time) and ``run`` (which sleeps), so the
    deferral rule is testable by calling ``tick`` with chosen times rather than
    by sleeping and hoping.

    The deferral matters more here than for callouts. An agent regenerating a
    model writes a file that is large enough to be observed half-written, and
    announcing that moment would make the viewer fetch a truncated STL and
    report a load failure for a part that is about to be perfectly fine. So a
    changed model is waited on until it parses, bounded by ``max_defer`` for the
    same reason as callouts: a file being rewritten continuously never looks
    settled, and a watcher that waits for quiet that never comes never fires.
    """

    def __init__(self, serve_dir, registry, event_log,
                 interval=CALLOUTS_POLL_INTERVAL, max_defer=CALLOUTS_MAX_DEFER,
                 invalidate_index=None):
        self._serve_dir = serve_dir
        self._registry = registry
        self._event_log = event_log
        self._interval = interval
        self._max_defer = max_defer
        # Drops the route layer's cached directory scan, so the refetch this
        # watcher's event provokes is answered from a scan taken after the
        # change rather than from one taken up to a second before it.
        self._invalidate_index = invalidate_index
        # First tick records and announces nothing: this watcher reports changes
        # since it started, and the state it starts in is what the page's own
        # fetch on load already covers.
        self._primed = False
        self._announced = {}
        self._unstable_since = None

    async def tick(self, now):
        """Sample the model tree once and broadcast if it changed and looks settled.

        Returns True if an event was broadcast, which is what the tests assert on.
        """
        loop = asyncio.get_running_loop()
        try:
            signature = await loop.run_in_executor(
                None, _models_signature, self._serve_dir, self._announced)
        except OSError:
            # A scan that fails outright is left for the next tick: there is
            # nothing to tell the browser to refetch, and /manifest would report
            # the same failure itself.
            return False
        if not self._primed:
            self._primed = True
            self._announced = signature
            return False
        if signature == self._announced:
            self._unstable_since = None
            return False

        # Only the models that appeared or changed can be half-written; one that
        # was removed has nothing left to parse.
        suspect = [rel for rel, stat in signature.items()
                   if self._announced.get(rel) != stat]
        if suspect:
            incomplete = await loop.run_in_executor(
                None, lambda: any(not _model_is_complete(self._serve_dir, rel)
                                  for rel in suspect))
            if incomplete:
                if self._unstable_since is None:
                    self._unstable_since = now
                if now - self._unstable_since < self._max_defer:
                    return False

        self._announced = signature
        self._unstable_since = None
        # Before the broadcast, never after: the browser refetches as soon as it
        # sees the event, so a cache dropped afterwards would be dropped too
        # late to affect the fetch the event caused.
        if self._invalidate_index is not None:
            self._invalidate_index()
        event = ModelsChanged()
        seq = self._event_log.append(event)
        await self._registry.broadcast(protocol.build_event(seq, event.to_wire()))
        return True

    async def run(self):
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(self._interval)
            await self.tick(loop.time())


async def run(serve_dir, host, port, on_ready=None, token=None, extra_origins=(),
              build_session=None,
              mesh_session_id=None):
    """Serve ``serve_dir`` on ``host``:``port`` until interrupted.

    Binds with ``start_serving=False`` and then explicitly awaits
    ``Server.start_serving()`` before calling ``on_ready``: binding alone
    only creates the socket, the kernel does not call ``listen()`` on it
    until serving actually starts, so a caller connecting between bind and
    that call would see a refused connection. ``on_ready`` therefore
    describes a socket that is already accepting connections, not one that
    merely will be. ``on_ready`` may be a plain function or a coroutine
    function; if calling it returns an awaitable, that awaitable is awaited
    before this function proceeds, which lets a caller offload blocking
    work (such as opening a browser) onto the event loop's executor instead
    of running it inline on the loop that is meant to already be serving.

    ``start_serving()`` alone is enough to keep the server accepting
    connections; nothing further needs to run for that to continue, so this
    then simply blocks until the caller cancels the task (``KeyboardInterrupt``
    in the CLI). ``asyncio.Server.serve_forever()`` is deliberately not used
    for that block: its own ``CancelledError`` handler runs an *unbounded*
    ``close()`` plus ``wait_closed()`` internally before re-raising, which
    would defeat ``SHUTDOWN_DRAIN_TIMEOUT`` below by blocking on any
    still-open connection before this function's own bounded wait ever gets
    a chance to run. Shutdown, on ``KeyboardInterrupt`` or task cancellation,
    stops accepting new connections and waits up to
    ``SHUTDOWN_DRAIN_TIMEOUT`` for in-flight requests to finish; past that
    bound it returns anyway, since microdot has no way to cut an in-flight
    request off short of dropping the connection.
    """
    app = create_app(serve_dir, token=token, host=host, port=port,
                     extra_origins=extra_origins, mesh_session_id=mesh_session_id,
                     build_session=build_session)
    # Started after the app is built and before the socket accepts anything, so
    # a browser cannot connect to a session that has not begun connecting. A
    # failure inside start() is reported as an event and never raised, so this
    # cannot stop the viewer from being served.
    if app.mesh_session is not None:
        await app.mesh_session.start()
    server = await app.start_server(host=host, port=port, start_serving=False)
    await server.start_serving()
    # Started here rather than in create_app, because create_app is called by
    # tests that have no running loop to own a background task and no interest
    # in one; a watcher per constructed app would leak a task per test.
    watcher = asyncio.ensure_future(
        CalloutsWatcher(serve_dir, app.mesh_registry, app.mesh_event_log).run())
    # Watched for the same reason as the callouts file, and started the same
    # way: a regenerated model is the agent changing the subject of the
    # conversation, and a viewer showing the previous geometry is showing
    # something that no longer exists.
    models_task = asyncio.ensure_future(
        ModelsWatcher(serve_dir, app.mesh_registry, app.mesh_event_log,
                      invalidate_index=app.mesh_invalidate_model_index).run())
    pinger = asyncio.ensure_future(ping_forever(app.mesh_registry))
    if on_ready is not None:
        result = on_ready()
        if inspect.isawaitable(result):
            await result
    try:
        await asyncio.Event().wait()
    finally:
        watcher.cancel()
        models_task.cancel()
        pinger.cancel()
        if app.mesh_session is not None:
            # Before the viewers are told to go away, so a permission request
            # still open is denied and its event reaches the browser on the
            # socket that is about to close, rather than vanishing with it.
            await app.mesh_session.close()
        # Viewers are told before the listener closes, so a browser reconnects
        # or falls back at once instead of waiting out its liveness timeout,
        # and so the bounded drain below is not spent waiting on WebSocket
        # handlers that would never return on their own.
        await app.mesh_registry.close_all()
        server.close()
        try:
            await asyncio.wait_for(server.wait_closed(), timeout=SHUTDOWN_DRAIN_TIMEOUT)
        except asyncio.TimeoutError:
            pass
