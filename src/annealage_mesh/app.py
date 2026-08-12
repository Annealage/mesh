"""Builds the microdot application and owns its asyncio startup and shutdown.

``create_app`` wires one served directory's routes onto a fresh ``Microdot``
instance; a fresh instance per call means independent served directories
never share route state, which matters for tests. ``run`` binds the socket,
hands control back to the caller once it is actually listening, then serves
until interrupted and closes the listener before returning.
"""

import asyncio
import inspect
import sys

from microdot import Microdot, Request

from . import __version__
from .http.routes_viewer import register_routes

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


def configure_request_limits():
    """Raise microdot's process-global request body limits to MAX_REQUEST_BODY.

    Called from create_app rather than run at import time, so the global
    mutation happens as a deliberate step tied to building an app, not as a
    side effect of merely importing this module.
    """
    Request.max_content_length = MAX_REQUEST_BODY
    Request.max_body_length = MAX_REQUEST_BODY


def create_app(serve_dir):
    """Build a Microdot app serving ``serve_dir``, routes registered, not started."""
    configure_request_limits()
    app = Microdot()
    register_routes(app, serve_dir)

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


async def run(serve_dir, host, port, on_ready=None):
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
    app = create_app(serve_dir)
    server = await app.start_server(host=host, port=port, start_serving=False)
    await server.start_serving()
    if on_ready is not None:
        result = on_ready()
        if inspect.isawaitable(result):
            await result
    try:
        await asyncio.Event().wait()
    finally:
        server.close()
        try:
            await asyncio.wait_for(server.wait_closed(), timeout=SHUTDOWN_DRAIN_TIMEOUT)
        except asyncio.TimeoutError:
            pass
