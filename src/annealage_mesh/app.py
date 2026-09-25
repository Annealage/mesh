"""Mesh's application: the agent layer's app plus the viewer, and the two
watchers that tell the page when the served files change.

``create_app`` builds the agent layer's app (``agent/app.py``) for one served
directory with Mesh's pieces plugged in: the viewer routes
(``http/routes_viewer.py``), Mesh's tool server, and the viewer page whose
inline script the Content-Security-Policy hashes. ``run`` serves it with the
callouts and models watchers running beside it. A fresh app per call means
independent served directories never share route state, which matters for
tests.
"""

import asyncio
import hashlib
import json
import os
import pathlib
import time

from . import (
    paths,
    product,  # noqa: F401  (installs Mesh as this process's product)
    stl,
)
from .agent import app as agent_app
from .agent import files, net, protocol
from .events import CalloutsChanged, ModelsChanged
from .http.routes_viewer import VIEWER_HTML, register_routes

# The port used when a caller does not name one. Kept here rather than only in
# argparse so an app built directly, as the tests do, computes the same
# Origin and Host allowlists the command line would.
DEFAULT_PORT = 8765

# How often the callouts file is checked, and how long a file that keeps
# changing without ever parsing is waited on before an event is pushed
# anyway. One stat-and-read of a small JSON file every quarter second is
# cheaper than an inotify dependency and behaves the same on every platform.
CALLOUTS_POLL_INTERVAL = 0.25
CALLOUTS_MAX_DEFER = 5.0


def create_app(
    serve_dir,
    *,
    token=None,
    agent_token=None,
    host=net.DEFAULT_HOST,
    port=DEFAULT_PORT,
    extra_origins=(),
    mesh_session_id=None,
    build_session=None,
    settings=None,
    login=None,
):
    """Build a Microdot app serving ``serve_dir``, routes registered, not started.

    Everything but the viewer is the agent layer's ``create_app``; see it for
    ``host``, ``port``, ``extra_origins``, ``settings``, ``login`` and the two tokens.
    ``token`` is the browser token, ``agent_token`` the separate one only
    ``/mcp`` accepts.

    ``token`` of ``None`` means no token was configured, and ``/ws`` then
    refuses every request. The read-only viewer routes stay open either way,
    because gating them buys nothing: their exposure was closed by deleting
    the serve-anything fallback, not by a secret.

    ``mesh_session_id`` is the id ``cli.py`` resolved for this run (fresh or
    resumed, per plan section 3.4), or None for viewer-only. It is set for
    agent mode and None for viewer-only, which is also what tells /submit
    whether to require the token.
    """

    def _register_viewer_routes(app, allowed_origins):
        register_routes(
            app,
            serve_dir,
            token=token,
            allowed_origins=allowed_origins,
            require_token=mesh_session_id is not None,
        )

    return agent_app.create_app(
        serve_dir,
        page_html=VIEWER_HTML,
        port=port,
        token=token,
        agent_token=agent_token,
        host=host,
        extra_origins=extra_origins,
        session_id=mesh_session_id,
        build_session=build_session,
        register_routes=_register_viewer_routes,
        settings=settings,
        login=login,
    )


def _callouts_state(serve_dir):
    """Return ``(digest, parses)`` for the callouts file as it is right now.

    The change signal is a digest of the bytes actually read, not the file's
    size and modification time. A digest cannot miss a rewrite that happens to
    preserve both, and it removes the gap between deciding a file changed and
    reading what it changed to, because there is only one read. Reading goes
    through ``files.read_fixed_file``, so a symlink, a hard link or a FIFO left
    at that name is refused here exactly as it is on the ``/callouts`` route.

    ``parses`` is the readiness test. The agent writes this file directly and
    the published skill does not require it to do so atomically, so a change
    can be observed mid-write. Stability of a stat signature across two
    samples does not prove a write finished: a same-size rewrite, or a stall
    inside one ``write`` call, produces two identical samples of an
    incomplete file. Whether the bytes parse as JSON does prove it, for every
    incomplete write that is not itself coincidentally valid JSON.
    """
    raw = files.read_fixed_file(serve_dir, paths.CALLOUTS_JSON_NAME)
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

    def __init__(
        self,
        serve_dir,
        registry,
        event_log,
        interval=CALLOUTS_POLL_INTERVAL,
        max_defer=CALLOUTS_MAX_DEFER,
    ):
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
            digest, parses = await loop.run_in_executor(None, _callouts_state, self._serve_dir)
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

    def __init__(
        self,
        serve_dir,
        registry,
        event_log,
        interval=CALLOUTS_POLL_INTERVAL,
        max_defer=CALLOUTS_MAX_DEFER,
        invalidate_index=None,
    ):
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
                None, _models_signature, self._serve_dir, self._announced
            )
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
        suspect = [rel for rel, stat in signature.items() if self._announced.get(rel) != stat]
        if suspect:
            incomplete = await loop.run_in_executor(
                None, lambda: any(not _model_is_complete(self._serve_dir, rel) for rel in suspect)
            )
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


async def run(
    serve_dir,
    host,
    port,
    on_ready=None,
    token=None,
    agent_token=None,
    extra_origins=(),
    build_session=None,
    mesh_session_id=None,
    settings=None,
    login=None,
):
    """Serve ``serve_dir`` on ``host``:``port`` until interrupted.

    The serving itself is the agent layer's ``serve`` (see it for when
    ``on_ready`` fires and how shutdown drains). What Mesh adds is the two
    watchers, started after the session: the callouts file, and the models
    themselves, because a regenerated model is the agent changing the subject
    of the conversation, and a viewer showing the previous geometry is showing
    something that no longer exists.
    """
    app = create_app(
        serve_dir,
        token=token,
        agent_token=agent_token,
        host=host,
        port=port,
        extra_origins=extra_origins,
        mesh_session_id=mesh_session_id,
        build_session=build_session,
        settings=settings,
        login=login,
    )
    await agent_app.serve(
        app,
        host,
        port,
        on_ready=on_ready,
        background=(
            CalloutsWatcher(serve_dir, app.agent_registry, app.agent_event_log).run,
            ModelsWatcher(
                serve_dir,
                app.agent_registry,
                app.agent_event_log,
                invalidate_index=app.mesh_invalidate_model_index,
            ).run,
        ),
    )
