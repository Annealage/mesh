"""Playwright end-to-end tests for the packaged viewer.

These drive a real Chromium against the real microdot app on a real,
ephemeral loopback socket; every other test file in this suite avoids
sockets entirely and reaches the app through microdot's in-process
TestClient, which cannot observe what only a browser can: whether a request
actually left the page for another origin, whether a ResizeObserver
recovers a canvas that briefly reported a zero-sized box, or whether a
click on the WebGL canvas turns into a store mutation and a file on disk.

Playwright and a Chromium binary are not part of this project's .venv, so a
plain `.venv/bin/python -m pytest` must skip this file rather than error;
`pytest.importorskip` below and the browser-binary search a few lines under
it both exist to make that skip clean instead of a collection error. Run
these for real with:

    uv run --with playwright --with pytest --with microdot --with pytest-asyncio \
        python -m pytest tests/test_viewer_e2e.py -q
"""

import asyncio
import base64
import glob
import json
import os
import secrets
import socket
import struct
import threading
import time

import pytest

pytest.importorskip("playwright.sync_api")

from playwright.sync_api import sync_playwright

from annealage_mesh import app as mesh_app
from annealage_mesh.session import base as session_base
from annealage_mesh.session.fake import FakeSession


def _find_chrome():
    """Locate a Chromium binary for Playwright to drive.

    Checked in order: the MESH_E2E_CHROME environment variable, for a
    machine or a CI image that keeps the browser somewhere of its own
    choosing, then a glob over Playwright's own cache layout
    (~/.cache/ms-playwright/chromium-<rev>/chrome-linux64/chrome), which
    covers a normal `playwright install`. Neither found means there is no
    browser to drive here, which is an environment gap rather than a test
    failure, so the caller skips the module instead of erroring.
    """
    env = os.environ.get("MESH_E2E_CHROME")
    if env:
        return env
    matches = sorted(glob.glob(os.path.expanduser(
        "~/.cache/ms-playwright/chromium-*/chrome-linux64/chrome")))
    return matches[0] if matches else None


CHROME_PATH = _find_chrome()
if not CHROME_PATH:
    pytest.skip(
        "no chromium binary found for playwright (set MESH_E2E_CHROME to point at one)",
        allow_module_level=True,
    )


def _free_port():
    """Return a TCP port free at the moment of the call.

    The socket is closed before the caller binds it again, so there is a
    theoretical window another process could steal the port in; on the
    loopback address, in a test run, that is an acceptable risk rather than
    one worth a retry loop.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _ServerThread:
    """Runs annealage_mesh.app.run on its own event loop in a background thread.

    A test that drives a browser cannot also run the asyncio loop serving
    that browser's requests on the main thread, so the loop lives on a
    thread of its own; `start` blocks the calling (test) thread only until
    `on_ready` fires, never by making an HTTP call into the loop it is
    waiting on.

    ``token``, when given, is passed to ``mesh_app.run`` verbatim, the same
    fixed-token path ``--token`` gives a real invocation (M4 brief): a test
    that needs a known, reusable value for the URL fragment and the `/ws`
    query parameter cannot rely on a per-run random one it never sees. Left
    ``None`` for every test that predates M4's auth, so those calls into
    ``mesh_app.run`` are unchanged and unaffected by whether this server's
    build has grown a ``token`` parameter yet.
    """

    def __init__(self, serve_dir, token=None, build_session=None):
        self.serve_dir = serve_dir
        self.host = "127.0.0.1"
        self.port = _free_port()
        self.base_url = "http://%s:%d" % (self.host, self.port)
        self.token = token
        self.build_session = build_session
        self._loop = None
        self._task = None
        self._thread = None
        self._ready = threading.Event()

    @property
    def viewer_url(self):
        """The URL a human (or this test) opens: base plus the token
        fragment, `#t=<token>`, mirroring how the real startup banner embeds
        it (M4 brief) so a bookmark of the visible address bar, which never
        includes a fragment, cannot carry the token anywhere it would be
        logged."""
        return self.base_url + "/#t=" + self.token

    def start(self):
        def _run():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            run_kwargs = {"on_ready": self._ready.set}
            if self.token is not None:
                run_kwargs["token"] = self.token
            if self.build_session is not None:
                run_kwargs["build_session"] = self.build_session
            # on_ready is threading.Event.set: synchronous, non-blocking,
            # not awaitable, so run() proceeds immediately after calling it
            # rather than waiting on anything that touches this same loop.
            coro = mesh_app.run(self.serve_dir, self.host, self.port, **run_kwargs)
            task = loop.create_task(coro)
            self._task = task
            # Cancelling the task alone would leave run_forever spinning
            # with nothing left to do; stopping the loop from the task's own
            # done callback, which runs on this same thread, is what lets
            # this thread's run_forever() return once shutdown (server.close
            # plus its bounded wait_closed, both inside run()'s finally)
            # actually finishes.
            task.add_done_callback(lambda t: loop.stop())
            try:
                loop.run_forever()
            finally:
                loop.close()

        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=10):
            raise RuntimeError("annealage_mesh server did not become ready within 10s")

    def stop(self):
        """Stop the server, and tolerate being called twice.

        A test that stops a server mid-run to drop a viewer's socket leaves its
        fixture's own teardown to call this again on a loop that is already
        closed, where ``call_soon_threadsafe`` raises rather than no-opping.
        Idempotence here is cheaper than making every such test remember to
        neutralise its fixture.
        """
        if self._loop is None:
            return
        try:
            self._loop.call_soon_threadsafe(self._task.cancel)
        except RuntimeError:
            return  # already stopped: the loop is closed
        self._thread.join(timeout=5)
        self._loop = None


def _cube_stl_bytes(center, half):
    """Return a minimal binary STL for an axis-aligned cube, as bytes.

    12 triangles (2 per face), exact `84 + 12*50` byte size so the vendored
    STLLoader's size check classifies it as binary regardless of what the
    80-byte header contains. Winding is not load-bearing here: models.js
    loads every mesh with `side: THREE.DoubleSide`, so a raycast hits either
    winding the same way.
    """
    cx, cy, cz = center
    x0, x1 = cx - half, cx + half
    y0, y1 = cy - half, cy + half
    z0, z1 = cz - half, cz + half
    v = {
        0: (x0, y0, z0), 1: (x1, y0, z0), 2: (x1, y1, z0), 3: (x0, y1, z0),
        4: (x0, y0, z1), 5: (x1, y0, z1), 6: (x1, y1, z1), 7: (x0, y1, z1),
    }
    faces = [
        ((0.0, 0.0, -1.0), (0, 2, 1), (0, 3, 2)),
        ((0.0, 0.0, 1.0), (4, 5, 6), (4, 6, 7)),
        ((0.0, -1.0, 0.0), (0, 1, 5), (0, 5, 4)),
        ((0.0, 1.0, 0.0), (3, 7, 6), (3, 6, 2)),
        ((-1.0, 0.0, 0.0), (0, 4, 7), (0, 7, 3)),
        ((1.0, 0.0, 0.0), (1, 2, 6), (1, 6, 5)),
    ]
    triangles = []
    for normal, t1, t2 in faces:
        triangles.append((normal, t1))
        triangles.append((normal, t2))

    header = b"annealage-mesh e2e fixture cube".ljust(80, b"\x00")
    body = bytearray()
    for normal, tri in triangles:
        body += struct.pack("<3f", *normal)
        for idx in tri:
            body += struct.pack("<3f", *v[idx])
        body += struct.pack("<H", 0)
    return header + struct.pack("<I", len(triangles)) + bytes(body)


@pytest.fixture(scope="module")
def served_dir(tmp_path_factory):
    """Two small cube models. `alpha.stl` sorts first, so it is the one
    models.js's default-visibility rule (`visibility[rel] = i === 0`) shows
    on load; `beta.stl` starts hidden. Their centres are far enough apart
    that nothing about the test depends on that separation, only on `alpha`
    being visible by default and `beta` not."""
    d = tmp_path_factory.mktemp("mesh_e2e")
    (d / "alpha.stl").write_bytes(_cube_stl_bytes(center=(0.0, 0.0, 0.0), half=10.0))
    (d / "beta.stl").write_bytes(_cube_stl_bytes(center=(60.0, 0.0, 0.0), half=10.0))
    return d


@pytest.fixture(scope="module")
def mesh_server(served_dir):
    server = _ServerThread(served_dir)
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def token_mesh_server(tmp_path_factory):
    """A fresh, single-model, token-gated server for one test.

    Function-scoped rather than shared with `mesh_server` above: the tests
    that use this fixture toggle the browser's network state, hand-edit a
    file the server watches, and force a protocol mismatch, none of which
    should carry any state into a sibling test, and none of which any of
    the eight pre-M4 tests need a token for at all. Each instance gets its
    own directory, its own port (`_ServerThread` already draws a fresh one)
    and its own `secrets.token_urlsafe` value, exactly the shape a real
    per-run token takes.
    """
    d = tmp_path_factory.mktemp("mesh_e2e_auth")
    (d / "alpha.stl").write_bytes(_cube_stl_bytes(center=(0.0, 0.0, 0.0), half=10.0))
    server = _ServerThread(d, token=secrets.token_urlsafe(16))
    server.start()
    try:
        yield server
    finally:
        server.stop()


def _emit_on_loop(server, session, event):
    """Push a scripted `AgentEvent` through `session.emit` on `server`'s own
    event loop thread, then block until it has actually run.

    `FakeSession.emit` calls straight through to the `on_event` callback
    `app.py` built (`_event_publisher`), which ends by calling
    `asyncio.ensure_future` to schedule the broadcast onto "the running
    loop": that resolves implicitly to whichever loop is running on the
    calling thread, so calling `emit` directly from this test's own thread
    would either schedule onto the wrong loop or raise for having none at
    all. `call_soon_threadsafe` is the one API safe to call from here; the
    `threading.Event` is what turns its fire-and-forget scheduling back into
    something this synchronous test can wait on and see an exception from.
    """
    done = threading.Event()
    failure = []

    def _run():
        try:
            session.emit(event)
        except Exception as exc:  # pragma: no cover - surfaced via failure[0]
            failure.append(exc)
        finally:
            done.set()

    server._loop.call_soon_threadsafe(_run)
    if not done.wait(timeout=5.0):
        raise AssertionError("emit did not run on the server loop within 5s")
    if failure:
        raise failure[0]


def _wait_until(predicate, timeout=5.0, message="condition not met"):
    """Poll a plain Python predicate (never a page.* call; see
    `_wait_for_request_growth`'s own comment for why a Playwright wait needs
    `page.wait_for_timeout` instead) until it is true or `timeout` elapses.

    Used only to observe state on the fake session's side (`submitted_turns`,
    `permission_decisions`, `interrupted`), which this test's own thread
    reads while the server's loop thread writes; a plain `list.append` and a
    plain integer increment are each atomic under the GIL, so polling here
    needs no lock of its own, only patience.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(message)


@pytest.fixture
def chat_server(tmp_path_factory):
    """A fresh, single-model, token-gated server wired to a scripted
    `FakeSession` (`session/fake.py`) through `create_app`'s `build_session`
    factory, the injection point named in the M5 brief. No `claude_agent_sdk`
    import happens anywhere in this fixture or in `fake.py`, no subprocess is
    spawned, and no network beyond the loopback socket this same fixture
    binds is reachable from it.
    """
    d = tmp_path_factory.mktemp("mesh_e2e_chat")
    (d / "alpha.stl").write_bytes(_cube_stl_bytes(center=(0.0, 0.0, 0.0), half=10.0))
    sessions = []
    buses = []

    def build_session(on_event, *, bus):
        session = FakeSession(on_event)
        sessions.append(session)
        buses.append(bus)
        return session

    server = _ServerThread(d, token=secrets.token_urlsafe(16), build_session=build_session)
    server.start()
    # The `ViewerBus` this app built, kept on the server handle rather than
    # yielded alongside it so the tests that predate M6 are untouched. The
    # mesh-tool tests need it: it is the only way to reach the browser the way a
    # tool does, through the same registry, timeout and pause flag.
    server.bus = buses[0]
    try:
        yield server, sessions[0]
    finally:
        server.stop()


def _wait_connection_state(page, state, timeout=15000):
    """Block until `window.mesh.store`'s `connection` key equals `state`.

    Centralised for the same reason `_wait_both_meshes_loaded` is: every M4
    test below needs this precondition before it can assert anything about
    what caused the transition into or out of that state.
    """
    page.wait_for_function(
        "(s) => window.mesh && window.mesh.store.getState().connection === s",
        arg=state, timeout=timeout,
    )


def _wait_for_request_growth(page, requests, baseline, timeout=5.0):
    """Block until ``requests`` holds more entries than it did at ``baseline``.

    Ties a wait to an actual request landing rather than a fixed sleep
    guessed to be long enough: a request already known to be expected
    (e.g. handleHello's own refetchCallouts, fired the instant the
    connection state flips to "live") can still be in flight when a fixed
    margin expires under a slow or loaded run, landing later and being
    miscounted against whatever comparison follows the margin.
    """
    deadline = time.time() + timeout
    while len(requests) <= baseline:
        if time.time() >= deadline:
            raise AssertionError(
                "no new request landed within %.1fs (stayed at %d)" % (timeout, len(requests)))
        # page.wait_for_timeout, not time.sleep: the synchronous Playwright API
        # dispatches events only while a call into it is in progress, so a bare
        # sleep leaves every queued "request" event undelivered until the next
        # such call. A loop that sleeps while waiting for requests to arrive
        # therefore sees none arrive, however long it waits.
        page.wait_for_timeout(20)


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=CHROME_PATH, args=["--no-sandbox"])
        try:
            yield b
        finally:
            b.close()


def _wait_both_meshes_loaded(page):
    """Block until window.mesh.meshes holds both fixture models.

    Every test needs the manifest fetched and both STL bytes parsed before
    it can rely on window.mesh, checkbox state or a raycast target existing;
    centralising the wait keeps that condition written once.
    """
    page.wait_for_function("() => window.mesh && Object.keys(window.mesh.meshes).length === 2")


# 1. Page load, part list, no CDN --------------------------------------------

def test_part_list_shows_both_labels_with_no_external_requests(browser, mesh_server):
    """Proves the manifest-driven part list renders both models' `label`
    text, and that vendoring three.js means nothing on the page reaches
    outside the server's own origin: every request not aimed at
    mesh_server.base_url is aborted, and none were."""
    page = browser.new_page()
    blocked = []

    def _route(route):
        url = route.request.url
        if url.startswith(mesh_server.base_url):
            route.continue_()
        else:
            blocked.append(url)
            route.abort()

    page.route("**/*", _route)
    try:
        page.goto(mesh_server.base_url + "/")
        _wait_both_meshes_loaded(page)
        labels = page.locator("#parts label span:last-child").all_inner_texts()
        assert sorted(labels) == ["alpha", "beta"]
        assert blocked == []
    finally:
        page.close()


# 2. Checkbox -> store and mesh -----------------------------------------------

def test_unchecking_a_part_moves_both_store_visibility_and_mesh_visible(browser, mesh_server):
    """Unchecking a part's checkbox must move both `window.mesh.store`'s
    visibility and the loaded mesh's own `.visible` flag: the checkbox-to-
    mesh half of the bidirectional binding store.js exists to guarantee."""
    page = browser.new_page()
    try:
        page.goto(mesh_server.base_url + "/")
        _wait_both_meshes_loaded(page)
        rel = page.evaluate("window.mesh.store.getState().models[0].rel")
        assert page.evaluate("(rel) => window.mesh.store.getState().visibility[rel]", rel) is True
        assert page.evaluate("(rel) => window.mesh.meshes[rel].visible", rel) is True

        checkbox = page.locator("#parts label").nth(0).locator("input[type=checkbox]")
        checkbox.uncheck()

        page.wait_for_function(
            "(rel) => window.mesh.store.getState().visibility[rel] === false", arg=rel)
        assert page.evaluate("(rel) => window.mesh.meshes[rel].visible", rel) is False
    finally:
        page.close()


# 3. store.setVisibility -> checkbox and mesh --------------------------------

def test_programmatic_set_visibility_unchecks_checkbox_and_hides_mesh(browser, mesh_server):
    """The defect M3 exists to fix: calling `store.setVisibility` from
    script, with no click involved, must still move the checkbox and the
    mesh. A one-directional binding (mesh reads the store, checkbox does
    not) would pass test 2 above while failing this one."""
    page = browser.new_page()
    try:
        page.goto(mesh_server.base_url + "/")
        _wait_both_meshes_loaded(page)
        rel = page.evaluate("window.mesh.store.getState().models[0].rel")
        checkbox = page.locator("#parts label").nth(0).locator("input[type=checkbox]")
        assert checkbox.is_checked()

        page.evaluate("(rel) => window.mesh.store.setVisibility(rel, false)", rel)

        page.wait_for_function(
            "(rel) => window.mesh.meshes[rel].visible === false", arg=rel)
        assert not checkbox.is_checked()
    finally:
        page.close()


# 4. Narrow viewport: tabs, and resize recovery after a zero-sized box -------

def test_narrow_viewport_tabs_and_canvas_resize_recovery(browser, mesh_server):
    """At 390x844 the tab bar governs which of #app/#side is shown, starting
    on Model; switching to Review hides the canvas's container, and
    switching back must resume actual rendering, proving the zero-size
    guard in three-scene.js's ResizeObserver callback does not strand the
    render loop.

    A stale `renderer.domElement.width/height` or a stale `camera.aspect`
    cannot tell a resumed render loop apart from one that never resumes:
    both leave those three.js properties holding whatever they were set to
    the last time the box was non-zero, whether or not `renderer.render` is
    being called again. `renderer.info.render.frame` only advances on an
    actual render call, so watching it increase after the tab switches back
    is the one signal that distinguishes "drawing resumed" from "drawing
    stopped forever", the exact failure this guard exists to prevent.
    """
    page = browser.new_page(viewport={"width": 390, "height": 844})
    try:
        page.goto(mesh_server.base_url + "/")
        _wait_both_meshes_loaded(page)

        assert page.is_visible("#tabbar button[data-tab='model']")
        assert page.is_visible("#tabbar button[data-tab='review']")
        assert page.is_visible("#app")
        assert not page.is_visible("#side")

        page.click("#tabbar button[data-tab='review']")
        page.wait_for_function("document.querySelector('#side').style.display !== 'none'")
        assert not page.is_visible("#app")
        assert page.is_visible("#side")

        frame_while_hidden = page.evaluate("window.mesh.renderer.info.render.frame")

        page.click("#tabbar button[data-tab='model']")
        page.wait_for_function("document.querySelector('#app').style.display !== 'none'")
        page.wait_for_function(
            "(f) => window.mesh.renderer.info.render.frame > f", arg=frame_while_hidden)

        assert page.evaluate("window.mesh.renderer.domElement.width") > 0
        assert page.evaluate("window.mesh.renderer.domElement.height") > 0
        assert page.evaluate("Number.isFinite(window.mesh.camera.aspect)")
        assert page.evaluate(
            "document.querySelector('#app canvas').clientWidth === "
            "document.querySelector('#app').clientWidth")
    finally:
        page.close()


# 5. Wide viewport: no tab bar, container-box sizing -------------------------

def test_wide_viewport_no_tabbar_and_panel_toggle_resizes_canvas(browser, mesh_server):
    """At 1600 px wide the tab bar stays CSS-hidden and the Panel button
    governs #side instead; toggling it must change the canvas's own pixel
    width, which only a ResizeObserver on #app's content box can produce,
    since window.innerWidth never changes across the click."""
    page = browser.new_page(viewport={"width": 1600, "height": 900})
    try:
        page.goto(mesh_server.base_url + "/")
        _wait_both_meshes_loaded(page)

        assert page.evaluate(
            "getComputedStyle(document.querySelector('#tabbar')).display") == "none"

        page.wait_for_function("window.mesh.renderer.domElement.width > 0")
        open_width = page.evaluate("window.mesh.renderer.domElement.width")

        page.click("#panelBtn")
        page.wait_for_function(
            "(w) => window.mesh.renderer.domElement.width > w", arg=open_width)
        closed_width = page.evaluate("window.mesh.renderer.domElement.width")
        assert closed_width > open_width

        page.click("#panelBtn")
        page.wait_for_function(
            "(w) => window.mesh.renderer.domElement.width < w", arg=closed_width)
    finally:
        page.close()


# 6. Add-pin click -> store -> /submit -> disk -------------------------------

def test_click_in_add_pin_mode_creates_a_pin_and_submits_to_disk(browser, mesh_server):
    """A click on the visible model while in Add-pin mode must create a
    pin; typing a comment and clicking Submit must reach POST /submit and
    land in mesh-comments.json under the served directory, the full round
    trip from a pointer event to bytes on disk."""
    page = browser.new_page()
    try:
        page.goto(mesh_server.base_url + "/")
        _wait_both_meshes_loaded(page)

        page.click("#modeBtn")
        page.wait_for_function("window.mesh.store.getState().mode === 'annotate'")

        assert page.locator("#pins .empty").count() == 1, \
            "an empty pin list should say so, exactly once"

        page.click("#app canvas")
        page.wait_for_function("window.mesh.store.getState().pins.length === 1")

        # The empty-state message is owned by js/pins.js alone. A second copy
        # written into viewer.html would be an element nothing removes, still
        # claiming there are no pins while this pin was listed underneath it.
        assert page.locator("#pins .empty").count() == 0, \
            "\"no pins yet\" must not survive alongside a pin"

        # press_sequentially dispatches one keydown/input pair per
        # character, the way a real keyboard does; page.fill sets the whole
        # value in a single input event, which is why it cannot see the pin
        # list rebuilding out from under the textarea on every keystroke.
        comment_text = "e2e round-trip comment"
        textarea = page.locator("#pins .pin textarea")
        textarea.click()
        textarea.press_sequentially(comment_text, delay=20)
        page.wait_for_function(
            "(t) => window.mesh.store.getState().pins[0].comment === t", arg=comment_text)

        page.click("#submit")
        page.wait_for_function("document.getElementById('toast').className === 'ok'")

        comments_path = mesh_server.serve_dir / "mesh-comments.json"
        deadline = time.time() + 5.0
        while time.time() < deadline and not comments_path.is_file():
            time.sleep(0.05)
        assert comments_path.is_file()

        record = json.loads(comments_path.read_text())
        assert record["count"] == 1
        assert record["annotations"][0]["comment"] == comment_text
        assert record["annotations"][0]["part"] == "alpha"
    finally:
        page.close()


# 7. The store defers a mutator called from inside a subscriber ---------------

def test_a_mutator_called_from_inside_a_subscriber_is_deferred_not_nested(
        browser, mesh_server):
    """store.js queues a mutator called from within a notification instead of
    running it inline, so every listener observes one complete state change at
    a time. measure.js depends on this: its 'pins' subscriber re-validates the
    two dropdown selections and calls setMeasure from inside that same
    notification when a selected pin has gone.

    Two properties separate a queue from simply running the call inline, and
    the sequence one listener sees is not among them: a single listener sees
    the same two states either way. So this asserts the two that do differ.

    First, nesting depth. An inline call notifies from inside the listener
    loop it was called from, so the triggering listener is re-entered while
    still on its own stack; `maxDepth` would be 2. Queued, it is 1.

    Second, ordering as seen by a *second* listener. Inline, the second
    listener is reached by the nested notification first and only afterwards
    by the outer one, so it observes the newer state before the older one.
    Queued, both listeners see both states in the order the mutators were
    actually called. That out-of-order delivery is the concrete corruption the
    guard exists to prevent, and it is invisible to a test with one listener.
    """
    page = browser.new_page()
    try:
        page.goto(mesh_server.base_url + "/")
        _wait_both_meshes_loaded(page)
        result = page.evaluate(
            """() => {
              const store = window.mesh.store;
              const rels = Object.keys(window.mesh.meshes).sort();
              const [first, second] = rels;
              // Only the first model is visible on load, so both are shown
              // here to give the subscriber below something to turn off.
              store.setVisibility(first, true);
              store.setVisibility(second, true);
              const seen = [];
              const alsoSeen = [];
              let depth = 0;
              let maxDepth = 0;
              const unsubA = store.subscribe("visibility", (s) => {
                depth += 1;
                maxDepth = Math.max(maxDepth, depth);
                seen.push([s.visibility[first], s.visibility[second]]);
                if (s.visibility[second] !== false) store.setVisibility(second, false);
                depth -= 1;
              });
              // Registered after A, so it is reached later in the same
              // notification pass; that is what makes its ordering a witness.
              const unsubB = store.subscribe("visibility", (s) => {
                alsoSeen.push([s.visibility[first], s.visibility[second]]);
              });
              store.setVisibility(first, false);
              unsubA();
              unsubB();
              const s = store.getState();
              return {
                seen,
                alsoSeen,
                maxDepth,
                final: [s.visibility[first], s.visibility[second]],
                meshVisible: [window.mesh.meshes[first].visible,
                              window.mesh.meshes[second].visible],
              };
            }"""
        )
        assert result["maxDepth"] == 1, "a listener was re-entered from inside itself"
        assert result["alsoSeen"] == [[False, True], [False, False]], \
            "the second listener saw the two changes out of causal order"
        assert result["seen"] == [[False, True], [False, False]]
        assert result["final"] == [False, False]
        assert result["meshVisible"] == [False, False]
    finally:
        page.close()


# 8. Reload revalidates the vendored bundle instead of refetching it ---------

def test_reload_revalidates_the_vendored_three_js_rather_than_refetching(
        browser, mesh_server):
    """The vendored three.module.js is over a megabyte and every page load
    wants it, which is the cost of dropping the CDN. The packaged assets
    therefore carry a validator and Cache-Control: no-cache, so a reload asks
    and is told 304 rather than being sent the bytes again.

    Asserted through a real browser because the property is whether a browser
    actually issues the conditional request and accepts our 304, which no
    in-process client can show.
    """
    page = browser.new_page()
    statuses = []
    page.on("response", lambda r: statuses.append((r.url, r.status)))
    try:
        page.goto(mesh_server.base_url + "/")
        _wait_both_meshes_loaded(page)
        three_url = mesh_server.base_url + "/static/js/vendor/three.module.js"
        assert (three_url, 200) in statuses

        statuses.clear()
        page.reload()
        _wait_both_meshes_loaded(page)
        three = [s for (u, s) in statuses if u == three_url]
        assert three, "the reload did not request three.module.js at all"
        assert three[0] == 304, "expected a revalidation, got %r" % (three,)
    finally:
        page.close()


# 9. A hand-edited callouts file arrives by push, not by the 1.5s poll ------

def test_hand_edited_callouts_file_arrives_by_push_not_poll(browser, token_mesh_server):
    """M4's whole demonstrable deliverable (plan section 4, milestone M4):
    writing mesh-callouts.json by hand, with the socket live and nothing
    reloaded, makes a pin appear. Fact 4 in the M4 brief is why this has to
    be a browser test at all: a coded close and a pushed event both need a
    real socket to observe.

    Proved two ways, per the brief, not just one: the store's callouts
    length changes inside a window no 1.5s poll interval could produce, and
    exactly one /callouts request happens for the whole edit, the one the
    push itself triggers. A flaky timing margin alone would leave open the
    possibility that a poll just happened to land early; the request count
    rules that out regardless of how fast or slow the run is.
    """
    server = token_mesh_server
    page = browser.new_page()
    callouts_requests = []
    page.on("request", lambda r: callouts_requests.append(r) if r.url.endswith("/callouts") else None)
    try:
        page.goto(server.viewer_url)
        _wait_connection_state(page, "live")
        assert page.evaluate("window.mesh.store.getState().callouts.length") == 0

        # handleHello's own refetchCallouts (js/ws.js) fires in the same
        # tick "live" is set, but the fetch it starts is itself async;
        # waiting for that one request to actually land, rather than a
        # fixed margin, is what makes the baseline below trustworthy
        # instead of leaving that request still in flight to be miscounted
        # against the file-edit request that follows.
        _wait_for_request_growth(page, callouts_requests, 0)
        baseline = len(callouts_requests)

        record = {"annotations": [{
            "id": 1, "part": "alpha", "label": "+Z",
            "point": [0.0, 0.0, 10.0], "comment": "hand-edited callout",
        }]}
        callouts_path = server.serve_dir / "mesh-callouts.json"
        start = time.time()
        # Written via a temp-file-plus-rename rather than a direct write, so
        # the watcher's debounce (M4 brief: "the agent writes this file
        # directly and the published skill does not require an atomic
        # write") is exercised on a genuinely atomic replace rather than
        # relying on a single write() call being fast enough to look atomic
        # on this filesystem.
        tmp_path = callouts_path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(record))
        os.replace(tmp_path, callouts_path)

        page.wait_for_function(
            "() => window.mesh.store.getState().callouts.length === 1", timeout=5000)
        elapsed = time.time() - start

        assert page.evaluate("window.mesh.store.getState().callouts[0].comment") == \
            "hand-edited callout"
        assert page.evaluate("window.mesh.store.getState().connection") == "live", \
            "the socket must still be live; a fallback poll would also explain the pin appearing"
        assert len(callouts_requests) - baseline == 1, (
            "expected exactly one /callouts fetch after the baseline (the push-triggered "
            "refetch), got %d; a second one would mean a poll was also running"
            % (len(callouts_requests) - baseline))
        assert elapsed < 1.2, (
            "took %.3fs; the 1.5s poll interval could have produced this on its own, "
            "which is exactly what this test must rule out" % elapsed)
    finally:
        page.close()


# 10. A protocol-version mismatch closes the socket with code 4400 ----------

def test_protocol_version_mismatch_closes_with_code_4400(browser, token_mesh_server):
    """Verified fact 4 in the M4 brief is explicit that this is the one
    post-handshake close code this milestone ships, and that it can only be
    asserted here: microdot's in-process TestClient silently discards every
    frame that is not TEXT or BINARY, so a CLOSE frame's code byte never
    reaches it, and any test that "asserts" this through TestClient passes
    vacuously.

    Drives a raw WebSocket from page script rather than through this
    project's own js/ws.js, because ws.js always sends its own hard-coded
    PROTOCOL_VERSION and so can never produce this frame from inside this
    test run; the mismatch has to be forged by hand, which is also exactly
    what a genuinely stale cached page would do without meaning to.
    """
    server = token_mesh_server
    page = browser.new_page()
    try:
        page.goto(server.viewer_url)
        _wait_connection_state(page, "live")

        close_code = page.evaluate(
            """(token) => new Promise((resolve, reject) => {
                const ws = new WebSocket(
                    "ws://" + location.host + "/ws?t=" + encodeURIComponent(token));
                const giveUp = setTimeout(
                    () => reject(new Error("no close event within 5s")), 5000);
                ws.addEventListener("open", () => {
                    ws.send(JSON.stringify({v: 999, type: "hello", token: token}));
                });
                ws.addEventListener("close", (ev) => {
                    clearTimeout(giveUp);
                    resolve(ev.code);
                });
            })""",
            server.token,
        )
        assert close_code == 4400
    finally:
        page.close()


# 11. No token in the URL: the page ends refused, not stuck connecting -----

def test_no_token_in_url_ends_refused_with_stale_url_message(browser, token_mesh_server):
    """Per the M4 brief, fact 2: an auth failure on /ws is an HTTP 403
    before any upgrade, never a close code, so the browser's WebSocket API
    (per fact 2's own consequence) cannot see why the handshake failed.
    js/ws.js resolves that blind spot with a plain fetch of the same path,
    which does expose the status; the outcome a phone user with no terminal
    actually needs is the "refused" connection state and a message telling
    them what to do, both asserted here.

    Opens the page with no `#t=...` fragment at all, the exact shape of a
    bookmark, a shared link with the fragment stripped, or a plain reload
    of a page that already dropped its own fragment on load: none of those
    carry a token forward, since none of them are the printed URL from a
    fresh server start.
    """
    server = token_mesh_server
    page = browser.new_page()
    try:
        page.goto(server.base_url + "/")
        _wait_connection_state(page, "refused")

        assert page.locator("#err").is_visible()
        message = page.locator("#err").inner_text().lower()
        assert "stale" in message
        assert "reopen the url" in message
    finally:
        page.close()


# 12. A dropped socket resumes the poll, and a reconnect stops it again ----

def test_socket_drop_resumes_poll_and_reconnect_stops_it_again(browser, token_mesh_server):
    """Plan section 3.3 / the M4 brief: "a viewer whose socket is closed or
    never opened resumes the 1500 ms poll after one backoff interval,"
    and "the poll and the push must never both be running."

    The socket is dropped by stopping the server, which is one of the causes
    the fallback exists for (the plan names "a network blip, a laptop sleep or
    a server restart") and, unlike the alternatives, actually drops it.
    Emulating the browser as offline does not: measured against a real
    Chromium, ``context.set_offline(True)`` flips ``navigator.onLine`` to false
    and fires the ``offline`` event, but leaves an already-established
    WebSocket open and still delivering the server's pings, so the page
    correctly goes on reporting "live" and such a test would be asserting on a
    drop that never happened.

    Checked in both directions: /callouts requests resume once the indicator
    reports "polling", and they stop again once it reports "live" after a
    server is listening again, so the two states this test can directly
    observe never overlap with which connection state js/ws.js reports.
    """
    server = token_mesh_server
    page = browser.new_page()
    replacement = None
    callouts_requests = []
    page.on("request", lambda r: callouts_requests.append(r) if r.url.endswith("/callouts") else None)
    try:
        page.goto(server.viewer_url)
        _wait_connection_state(page, "live")
        callouts_requests.clear()

        server.stop()
        try:
            _wait_connection_state(page, "polling", timeout=15000)
            deadline = time.time() + 6.0
            while time.time() < deadline and len(callouts_requests) < 2:
                page.wait_for_timeout(100)
            assert len(callouts_requests) >= 2, \
                "the fallback poll never fired while the socket was down"
        finally:
            # Same directory, same port and the same token, so the page's own
            # reconnect attempts start succeeding without it being reloaded:
            # this is a server restart from the page's point of view, which is
            # exactly the case being covered.
            replacement = _ServerThread(server.serve_dir, token=server.token)
            replacement.port = server.port
            replacement.base_url = server.base_url
            replacement.start()

        pre_reconnect = len(callouts_requests)
        _wait_connection_state(page, "live", timeout=20000)
        # Going live triggers exactly one /callouts request of its own
        # (handleHello's refetchCallouts); waiting for it to actually land
        # before taking the baseline below is what stops a slow run from
        # leaving it still in flight to land during the sleep that
        # follows, where it would be miscounted as evidence the poll kept
        # running after reconnecting.
        _wait_for_request_growth(page, callouts_requests, pre_reconnect)
        baseline = len(callouts_requests)
        page.wait_for_timeout(2000)
        assert len(callouts_requests) == baseline, \
            "the poll kept firing after the socket reconnected and went live again"
    finally:
        page.close()
        if replacement is not None:
            replacement.stop()


# 13. Chat pane: a typed turn reaches the AgentSession -----------------------

def test_typed_turn_reaches_the_session(browser, chat_server):
    """Typing a message and clicking Send must submit it to the
    `AgentSession` exactly as composed. This is the only path from the
    browser to `AgentSession.submit_turn`: the inbound `turn` frame plan
    section 3.3 defines, dispatched by `http/ws.py`'s `_dispatch`. The
    composer clearing is chat.js's own signal that the frame actually went
    out, not merely that the click handler ran.
    """
    server, session = chat_server
    page = browser.new_page()
    try:
        page.goto(server.viewer_url)
        _wait_connection_state(page, "live")

        page.fill("#chatInput", "why is this wall thin?")
        page.click("#chatSend")

        _wait_until(lambda: len(session.submitted_turns) == 1,
                    message="the turn frame never reached AgentSession.submit_turn")
        blocks, viewer = session.submitted_turns[0]
        assert blocks == [{"type": "text", "text": "why is this wall thin?"}]
        # Not asserted: that `viewer` carries the tab id the browser's own
        # `hello` sent. `http/ws.py` calls `registry.add(ws)` with no
        # `tab_id` argument, so `conn.tab_id`, and therefore this value, is
        # `None` for every connection regardless of what the browser
        # declared; see this file's report for the defect this leaves in
        # both the `viewer` stamp on outbound turns and `ViewerPrimary`.
        assert page.input_value("#chatInput") == ""
    finally:
        page.close()


# 14. Chat pane: a streamed reply arrives incrementally ----------------------

def test_streamed_reply_arrives_incrementally_not_all_at_once(browser, chat_server):
    """Two `text_delta` events for one turn must each be individually
    observable in the DOM, not only their final concatenation: that is what
    separates real streaming (`include_partial_messages=True`, plan fact 12)
    from a client that happened to coalesce two events before rendering
    either. The assertion that the second delta's text is absent after the
    first is what rules the latter out; asserting only the final text would
    pass even if chat.js buffered every delta until `turn_end`.
    """
    server, session = chat_server
    page = browser.new_page()
    try:
        page.goto(server.viewer_url)
        _wait_connection_state(page, "live")

        page.fill("#chatInput", "why is this wall thin?")
        page.click("#chatSend")
        _wait_until(lambda: len(session.submitted_turns) == 1)

        text_sel = "div.turn[data-turn='1'] .msg.assistant .text"

        _emit_on_loop(server, session, session_base.TextDelta(turn=1, text="The wall "))
        page.wait_for_function(
            "(sel) => !!document.querySelector(sel) && "
            "document.querySelector(sel).textContent === 'The wall '",
            arg=text_sel,
        )
        assert "measures 2mm" not in page.evaluate(
            "(sel) => document.querySelector(sel).textContent", text_sel)

        _emit_on_loop(server, session, session_base.TextDelta(turn=1, text="measures 2mm."))
        page.wait_for_function(
            "(sel) => !!document.querySelector(sel) && "
            "document.querySelector(sel).textContent === 'The wall measures 2mm.'",
            arg=text_sel,
        )
    finally:
        page.close()


# 15. Chat pane: a permission card appears and Allow reaches the session -----

def test_permission_card_allow_reaches_the_session(browser, chat_server):
    """A `permission_request` event must render as a card naming the tool, and
    clicking Allow must deliver an `allow` decision to
    `AgentSession.decide_permission` for that exact request id, the inbound
    `permission` frame plan section 3.3 defines.

    The card's lifetime is a pair of events, so clicking is not the end of it:
    the card stays, showing the decision is in flight and refusing further
    clicks, until the server's `permission_resolved` says the request is over.
    Removing it on the click instead would make a decision that another view had
    already answered look exactly like one that took effect.
    """
    server, session = chat_server
    page = browser.new_page()
    try:
        page.goto(server.viewer_url)
        _wait_connection_state(page, "live")

        _emit_on_loop(server, session, session_base.PermissionRequest(
            request_id="pr_1", tool="Bash", input={"command": "curl example.com"}))

        card = "div.permcard[data-request-id='pr_1']"
        page.wait_for_selector(card)
        assert page.evaluate(
            "(sel) => document.querySelector(sel).textContent", card + " .ptool") == "Bash"

        page.click(card + " .pactions button:nth-of-type(1)")

        _wait_until(lambda: len(session.permission_decisions) == 1,
                    message="the allow decision never reached AgentSession.decide_permission")
        assert session.permission_decisions[0] == ("pr_1", "allow", "")

        page.wait_for_selector(card + ".submitted")
        assert page.locator(card + " .pactions button:disabled").count() == 3, \
            "an in-flight decision must not accept a second click"

        _emit_on_loop(server, session, session_base.PermissionResolved(
            request_id="pr_1", outcome="allow"))
        page.wait_for_selector(card, state="detached")
    finally:
        page.close()


def test_a_decision_that_did_not_apply_says_so(browser, chat_server):
    """The bug this pairing exists to prevent: a human denies a card that
    another view has already allowed. The deny changed nothing, and without the
    outcome on the resolution event the card would simply vanish, which is
    indistinguishable from the deny having worked.
    """
    server, session = chat_server
    page = browser.new_page()
    try:
        page.goto(server.viewer_url)
        _wait_connection_state(page, "live")

        _emit_on_loop(server, session, session_base.PermissionRequest(
            request_id="pr_9", tool="Write", input={"file_path": "/etc/hosts"}))
        card = "div.permcard[data-request-id='pr_9']"
        page.wait_for_selector(card)
        page.click(card + " .pactions button:nth-of-type(3)")
        page.wait_for_selector(card + ".submitted")

        # What the other view's Allow produces on this one.
        _emit_on_loop(server, session, session_base.PermissionResolved(
            request_id="pr_9", outcome="allow"))

        page.wait_for_function(
            "() => document.getElementById('toast').style.display === 'block'")
        message = page.evaluate("() => document.getElementById('toast').textContent")
        assert "did not apply" in message
        assert "was allowed" in message
        page.wait_for_selector(card, state="detached")
    finally:
        page.close()


def test_a_part_the_agent_generates_appears_without_a_reload(browser, chat_server):
    """The modelling loop, end to end in a real browser: the agent writes a part
    and the human sees it.

    Before the models watcher existed this was the loop's broken step. The viewer
    loaded the manifest once, so a part regenerated by a CAD script went on being
    displayed with its previous geometry until the page was reopened, which is
    the moment a reviewer is least likely to suspect the picture is stale.
    """
    server, _session = chat_server
    page = browser.new_page()
    try:
        page.goto(server.viewer_url)
        _wait_connection_state(page, "live")
        page.wait_for_function(
            "() => window.mesh && Object.keys(window.mesh.meshes).length === 1")

        # The widest x coordinate in the loaded geometry. Triangle count cannot
        # be the signal here: a cube has twelve triangles whatever its size, so
        # a resized part would count the same and the assertion would pass
        # against geometry that never reloaded.
        widest = """(rel) => {
            const m = window.mesh.meshes[rel];
            if (!m) return null;
            const p = m.geometry.attributes.position.array;
            let mx = -Infinity;
            for (let i = 0; i < p.length; i += 3) mx = Math.max(mx, p[i]);
            return mx;
        }"""
        assert abs(page.evaluate(widest, "alpha.stl") - 10.0) < 0.001

        # A part that did not exist when the page loaded.
        (server.serve_dir / "bracket.stl").write_bytes(
            _cube_stl_bytes(center=(30.0, 0.0, 0.0), half=4.0))
        page.wait_for_function(
            "() => window.mesh.meshes['bracket.stl'] !== undefined", timeout=15000)
        # And it is a real part, listed for toggling like any other.
        page.wait_for_selector("#parts label")
        assert page.evaluate(
            "() => window.mesh.store.getState().models.map(m => m.rel).sort()"
        ) == ["alpha.stl", "bracket.stl"]

        # An existing part regenerated with different geometry, which is what a
        # CAD script re-run produces.
        (server.serve_dir / "alpha.stl").write_bytes(
            _cube_stl_bytes(center=(0.0, 0.0, 0.0), half=6.0))
        page.wait_for_function(
            "(fn) => { const f = eval(fn); const mx = f('alpha.stl');"
            "  return mx !== null && Math.abs(mx - 6.0) < 0.001; }",
            arg=widest, timeout=15000)

        # One mesh per part, not a new copy layered over the old one.
        assert page.evaluate(
            "() => Object.keys(window.mesh.meshes).length") == 2
        assert page.evaluate(
            "() => window.mesh.scene.children.filter(c => c.userData && c.userData.rel === 'alpha.stl').length"
        ) == 1, "a regenerated part must replace its mesh, not add another"
    finally:
        page.close()


def test_a_part_the_agent_deletes_disappears_without_a_reload(browser, chat_server):
    server, _session = chat_server
    page = browser.new_page()
    try:
        (server.serve_dir / "spare.stl").write_bytes(
            _cube_stl_bytes(center=(40.0, 0.0, 0.0), half=3.0))
        page.goto(server.viewer_url)
        _wait_connection_state(page, "live")
        page.wait_for_function(
            "() => window.mesh.meshes['spare.stl'] !== undefined", timeout=15000)
        rows_before = page.locator("#parts label").count()

        (server.serve_dir / "spare.stl").unlink()

        page.wait_for_function(
            "() => window.mesh.meshes['spare.stl'] === undefined", timeout=15000)
        # The checkbox goes too: a row for a file that no longer exists offers a
        # toggle that silently does nothing.
        page.wait_for_function(
            "(n) => document.querySelectorAll('#parts label').length === n - 1",
            arg=rows_before)
    finally:
        page.close()


def test_reloading_re_shows_a_prompt_that_is_still_waiting(browser, chat_server):
    """A request outstanding across a reload has to come back, and an answered one
    must not. Both fall out of replaying the request-and-resolution pair from the
    event log: a request with no resolution behind it is exactly the card the
    pane should be showing.
    """
    server, session = chat_server
    page = browser.new_page()
    try:
        page.goto(server.viewer_url)
        _wait_connection_state(page, "live")

        _emit_on_loop(server, session, session_base.PermissionRequest(
            request_id="pr_20", tool="Write", input={"file_path": "/tmp/a"}))
        _emit_on_loop(server, session, session_base.PermissionRequest(
            request_id="pr_21", tool="Bash", input={"command": "ls"}))
        _emit_on_loop(server, session, session_base.PermissionResolved(
            request_id="pr_21", outcome="allow"))
        page.wait_for_selector("div.permcard[data-request-id='pr_20']")
        page.wait_for_selector("div.permcard[data-request-id='pr_21']", state="detached")

        # Re-opening the tokened URL rather than page.reload(): ws.js strips the
        # token from the address bar deliberately, so that a token cannot survive
        # in a bookmark or a screen share, and a bare reload therefore arrives
        # with no token and is refused. Re-opening the URL is what the pane's own
        # "Reopen URL" state tells the human to do, and it is the same fresh
        # connection and replay path either way.
        page.goto(server.viewer_url)
        _wait_connection_state(page, "live")

        page.wait_for_selector("div.permcard[data-request-id='pr_20']")
        assert page.locator("div.permcard[data-request-id='pr_21']").count() == 0, \
            "an answered request must not come back as a live card on reload"
        # Still answerable after the reload, which is the point of re-showing it.
        page.click("div.permcard[data-request-id='pr_20'] .pactions button:nth-of-type(1)")
        _wait_until(lambda: ("pr_20", "allow", "") in session.permission_decisions,
                    message="the decision made after a reload never reached the session")
    finally:
        page.close()


def test_a_multiline_command_is_shown_as_a_script_not_as_escapes(browser, chat_server):
    """A Bash command is a script, and it is the field a human most needs to read
    before approving it. Rendered as JSON its every line break becomes a literal
    backslash-n and the whole thing arrives as one unreadable line.
    """
    server, session = chat_server
    page = browser.new_page()
    try:
        page.goto(server.viewer_url)
        _wait_connection_state(page, "live")

        command = "python3 - <<'EOF'\nimport struct\nprint('hi')\nEOF"
        _emit_on_loop(server, session, session_base.PermissionRequest(
            request_id="pr_11", tool="Bash",
            input={"command": command, "description": "measure the STL"}))

        card = "div.permcard[data-request-id='pr_11']"
        page.wait_for_selector(card)
        shown = page.evaluate(
            "(sel) => document.querySelector(sel).textContent", card + " .toolinput")

        assert "\\n" not in shown, "line breaks must be line breaks, not escapes"
        assert "import struct" in shown
        assert shown.count("\n") >= 3
        # The second argument is still shown; it is not dropped in favour of the
        # first just because the first is the interesting one.
        assert "measure the STL" in shown
    finally:
        page.close()


def test_a_request_that_expired_is_not_reported_as_answered(browser, chat_server):
    """A card that goes away because nobody answered in time looks, without
    this, exactly like one somebody answered."""
    server, session = chat_server
    page = browser.new_page()
    try:
        page.goto(server.viewer_url)
        _wait_connection_state(page, "live")

        _emit_on_loop(server, session, session_base.PermissionRequest(
            request_id="pr_10", tool="Bash", input={"command": "ls"}))
        page.wait_for_selector("div.permcard[data-request-id='pr_10']")

        _emit_on_loop(server, session, session_base.PermissionResolved(
            request_id="pr_10", outcome="timeout"))

        page.wait_for_function(
            "() => document.getElementById('toast').style.display === 'block'")
        assert "expired" in page.evaluate(
            "() => document.getElementById('toast').textContent")
    finally:
        page.close()


# 16. Chat pane: Deny carries the human's typed reason ------------------------

def test_permission_card_deny_sends_the_typed_reason(browser, chat_server):
    """A deny's `message` reaches the model verbatim (M5 brief fact 2), so
    the pane's deny path has to carry whatever the human typed. The reason
    textarea is filled with `press_sequentially`, one keydown/input pair per
    character as a real keyboard produces, rather than `page.fill`'s single
    input event, per this file's own harness note: a control whose state
    depends on a rebuild triggered by an unrelated store change only shows
    that bug under per-keystroke input, and the pending-permission list is
    exactly such a rebuild-on-change control (`renderPending` in chat.js).
    """
    server, session = chat_server
    page = browser.new_page()
    try:
        page.goto(server.viewer_url)
        _wait_connection_state(page, "live")

        _emit_on_loop(server, session, session_base.PermissionRequest(
            request_id="pr_2", tool="Write", input={"file_path": "/etc/hosts"}))
        card = "div.permcard[data-request-id='pr_2']"
        page.wait_for_selector(card)

        reason = "not this file, ask about the enclosure instead"
        reason_box = page.locator(card + " textarea.preason")
        reason_box.click()
        reason_box.press_sequentially(reason, delay=20)
        assert reason_box.input_value() == reason

        page.click(card + " .pactions button:nth-of-type(3)")

        _wait_until(lambda: len(session.permission_decisions) == 1,
                    message="the deny decision never reached AgentSession.decide_permission")
        assert session.permission_decisions[0] == ("pr_2", "deny", reason)

        _emit_on_loop(server, session, session_base.PermissionResolved(
            request_id="pr_2", outcome="deny"))
        page.wait_for_selector(card, state="detached")
    finally:
        page.close()


# 17. Chat pane: Interrupt reaches the session --------------------------------

def test_interrupt_button_ends_a_turn(browser, chat_server):
    """The Interrupt button starts disabled (nothing to interrupt), becomes
    enabled once a turn is outstanding, and clicking it must deliver the
    inbound `interrupt` frame to `AgentSession.interrupt`, plan section
    3.3's mechanism for the human to stop a turn already in flight.
    """
    server, session = chat_server
    page = browser.new_page()
    try:
        page.goto(server.viewer_url)
        _wait_connection_state(page, "live")

        assert page.is_disabled("#chatInterrupt")

        page.fill("#chatInput", "keep exploring the model for defects")
        page.click("#chatSend")
        page.wait_for_function("!document.getElementById('chatInterrupt').disabled")

        page.click("#chatInterrupt")

        _wait_until(lambda: session.interrupted == 1,
                    message="the interrupt frame never reached AgentSession.interrupt")
    finally:
        page.close()


# 18. Chat pane: model text is escaped, never executed -----------------------

def test_model_text_is_escaped_and_never_executes(browser, chat_server):
    """A reply carrying a `<script>` tag and an `<img onerror=...>` handler
    must render as literal text and must not run: the locked decision in
    plan sections 3.10 and 5 that model text is never inserted as HTML.
    `<script>` inserted via `innerHTML` never executes in any browser
    regardless of escaping, so it alone would not prove this; an `<img
    src=x onerror=...>` inserted unescaped does fire its handler on the
    broken load, which is what makes the assertion on `window.__xssFired`
    a real test of execution rather than of parsing.
    """
    server, session = chat_server
    page = browser.new_page()
    try:
        page.goto(server.viewer_url)
        _wait_connection_state(page, "live")
        page.evaluate("() => { window.__xssFired = false; }")

        page.fill("#chatInput", "show me")
        page.click("#chatSend")
        _wait_until(lambda: len(session.submitted_turns) == 1)

        payload = ('<script>window.__xssFired = true</script>'
                   '<img src=x onerror="window.__xssFired = true">')
        _emit_on_loop(server, session, session_base.TextDelta(turn=1, text=payload))

        text_sel = "div.turn[data-turn='1'] .msg.assistant .text"
        page.wait_for_function(
            "(sel) => !!document.querySelector(sel) && "
            "document.querySelector(sel).textContent.includes('<script>')",
            arg=text_sel,
        )
        # An img's onerror, if it were going to fire, fires as soon as the
        # broken load is detected; wait_for_timeout rather than a bare sleep
        # pumps the page so that callback, if scheduled at all, has actually
        # run by the time the assertion below reads it.
        page.wait_for_timeout(200)

        assert page.evaluate("() => window.__xssFired") is False
        assert page.evaluate("() => document.querySelectorAll('#chatLog script').length") == 0
        assert page.evaluate("() => document.querySelectorAll('#chatLog img').length") == 0
        rendered = page.evaluate("(sel) => document.querySelector(sel).textContent", text_sel)
        assert "<script>" in rendered
        assert "onerror" in rendered
    finally:
        page.close()


# 19. Agent-authored callout text is never markup ------------------------------

def test_a_callout_carrying_markup_renders_as_text_not_html(browser, mesh_server):
    """mesh-callouts.json is written by the agent and served verbatim, with no
    field validation, so every field in it is untrusted input to this page.

    The escalation this closes is specific rather than theoretical: script
    running in the viewer's origin can set the chat composer's value and click
    Send, so markup in a callout field becomes an arbitrary instruction to an
    agent holding a shell. A crafted STL filename reaches the pin list the same
    way, via the manifest's `part`.
    """
    page = browser.new_page()
    fired = page.evaluate_handle("() => { window.__xss = false; return null; }")
    del fired
    try:
        page.goto(mesh_server.base_url + "/")
        _wait_both_meshes_loaded(page)

        payload = '<img src=x onerror="window.__xss = true">'
        (mesh_server.serve_dir / "mesh-callouts.json").write_text(json.dumps({
            "annotations": [{
                "id": 1, "part": payload, "label": payload,
                "point": [0, 0, 0], "comment": "look here",
            }],
        }))
        page.wait_for_function(
            "() => document.querySelectorAll('#agentPins .apin').length === 1",
            timeout=15000)

        row = page.locator("#agentPins .apin .part").first
        # The markup arrived as characters, so it is visible as text and no
        # element was created from it.
        assert payload in row.inner_text()
        assert page.evaluate("() => document.querySelectorAll('#agentPins img').length") == 0
        assert page.evaluate("() => window.__xss === true") is False
    finally:
        page.close()


# 20. Mesh tools: a real tool call drives the browser --------------------------

def _mesh_tools(server):
    """`{name: handler}` for a tool server built on this app's own bus.

    Built here rather than in the fixture because it is what a session does:
    `cli.py` constructs `MeshTools` from the bus `create_app` handed its
    factory, and reaching the browser any other way (calling `registry.call`
    directly, say) would test the transport while skipping the layer that
    turns a viewer failure into something a model can act on.
    """
    from annealage_mesh.tools.registry import MeshTools

    return {t.name: t.handler for t in MeshTools(server.bus, server.serve_dir).tools}


def _call_tool(server, name, args=None):
    """Run one mesh tool to completion on the server's own event loop.

    `run_coroutine_threadsafe`, not `call_soon_threadsafe`: a tool call is a
    round trip into the browser and back, so this test thread has to be able to
    wait for its result and see its exceptions, which a fire-and-forget
    scheduling cannot give it.
    """
    handler = _mesh_tools(server)[name]
    future = asyncio.run_coroutine_threadsafe(handler(args or {}), server._loop)
    return future.result(timeout=20)


def _tool_text(result):
    for item in result["content"]:
        if item["type"] == "text":
            return item["text"]
    return ""


def test_a_set_view_tool_call_actually_moves_the_camera(browser, chat_server):
    """M6's deliverable, end to end: a tool handler, the pending-future
    correlation in `viewers.py`, the `call` frame, `ws.js`'s dispatch,
    `commands.js`'s method table, and a camera that is somewhere else
    afterwards. Every layer in that list is covered on its own elsewhere; this
    is the only test that proves they are joined up.
    """
    server, _session = chat_server
    page = browser.new_page()
    try:
        page.goto(server.viewer_url)
        _wait_connection_state(page, "live")
        page.wait_for_function("() => window.mesh && Object.keys(window.mesh.meshes).length === 1")

        result = _call_tool(server, "set_view", {"position": [80, -60, 40],
                                                 "target": [1, 2, 3]})
        assert "is_error" not in result, _tool_text(result)

        assert page.evaluate("() => window.mesh.camera.position.toArray()") == \
            pytest.approx([80, -60, 40], abs=0.01)
        assert page.evaluate("() => window.mesh.controls.target.toArray()") == \
            pytest.approx([1, 2, 3], abs=0.01)
        # And the tool told the model where the camera ended up, rather than
        # only that the call succeeded.
        assert json.loads(_tool_text(result))["target"] == pytest.approx([1, 2, 3], abs=0.01)
    finally:
        page.close()


def test_a_set_visibility_tool_call_moves_the_checkbox_and_the_mesh(browser, chat_server):
    """The demo from the M6 brief, "ask the model to hide a part". It goes
    through `store.setVisibility`, so the checkbox and the mesh's own `visible`
    flag both move: a tool that reached into the mesh directly would leave the
    human looking at a checked box for a hidden part."""
    server, _session = chat_server
    page = browser.new_page()
    try:
        page.goto(server.viewer_url)
        _wait_connection_state(page, "live")
        page.wait_for_function("() => window.mesh && Object.keys(window.mesh.meshes).length === 1")
        rel = page.evaluate("() => window.mesh.store.getState().models[0].rel")
        checkbox = page.locator("#parts label").nth(0).locator("input[type=checkbox]")
        assert checkbox.is_checked()

        result = _call_tool(server, "set_visibility", {"rel": rel, "visible": False})
        assert "is_error" not in result, _tool_text(result)

        page.wait_for_function("(rel) => window.mesh.meshes[rel].visible === false", arg=rel)
        assert not checkbox.is_checked()
    finally:
        page.close()


def test_a_capture_view_tool_call_returns_the_real_canvas_as_png(browser, chat_server):
    """Fact 8's payoff, and the one command whose correctness cannot be
    reasoned about from the source: the renderer has no `preserveDrawingBuffer`,
    so a capture taken outside the synchronous render-then-read block comes back
    as a valid PNG of nothing at all. This decodes the bytes and checks the
    image is the size the canvas was asked for."""
    server, _session = chat_server
    page = browser.new_page()
    try:
        page.goto(server.viewer_url)
        _wait_connection_state(page, "live")
        page.wait_for_function("() => window.mesh && Object.keys(window.mesh.meshes).length === 1")

        result = _call_tool(server, "capture_view", {"width": 320})
        assert "is_error" not in result, _tool_text(result)
        images = [item for item in result["content"] if item["type"] == "image"]
        assert len(images) == 1
        assert images[0]["mimeType"] == "image/png"
        raw = base64.b64decode(images[0]["data"])
        assert raw.startswith(b"\x89PNG\r\n\x1a\n")
        # A PNG's IHDR carries the dimensions at a fixed offset, which is enough
        # to prove the requested width was honoured without a decoder.
        width, height = struct.unpack(">II", raw[16:24])
        assert width == 320
        assert height > 0
        assert json.loads(_tool_text(result))["width"] == 320
        # Not blank: an all-one-colour render compresses to almost nothing, and
        # a cube lit by three lights does not.
        assert len(raw) > 2000
    finally:
        page.close()


def test_a_snapshot_tool_call_writes_a_file_the_asset_route_serves(browser, chat_server):
    """The other half of capture: the same bytes, written where a human can
    commit them, and reachable through the route that already serves images/."""
    server, _session = chat_server
    page = browser.new_page()
    try:
        page.goto(server.viewer_url)
        _wait_connection_state(page, "live")
        page.wait_for_function("() => window.mesh && Object.keys(window.mesh.meshes).length === 1")

        result = _call_tool(server, "snapshot", {"name": "front", "width": 256})
        assert "is_error" not in result, _tool_text(result)
        payload = json.loads(_tool_text(result))
        written = server.serve_dir / "images" / "front.png"
        assert written.is_file()
        assert written.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")

        served = page.request.get(server.base_url + payload["url"])
        assert served.status == 200
        assert served.body() == written.read_bytes()
    finally:
        page.close()


def test_an_unknown_rel_comes_back_as_the_viewers_own_refusal(browser, chat_server):
    """A domain failure inside the browser has to reach the model as a failed
    tool call carrying the viewer's code, not as a timeout or a hang: the
    `error` frame answers the pending future immediately."""
    server, _session = chat_server
    page = browser.new_page()
    try:
        page.goto(server.viewer_url)
        _wait_connection_state(page, "live")
        page.wait_for_function("() => window.mesh && Object.keys(window.mesh.meshes).length === 1")

        result = _call_tool(server, "set_visibility", {"rel": "nope.stl", "visible": False})
        assert result["is_error"] is True
        assert "unknown_model" in _tool_text(result)
        # And it names what does exist, so the model's next call can be right.
        assert "alpha.stl" in _tool_text(result)
    finally:
        page.close()


# 21. Mesh tools: the human's pause switch is enforced in the server -----------

def test_the_topbar_pause_button_stops_the_agent_driving_the_view(browser, chat_server):
    """What the previous version of this control claimed and did not do. The
    click has to travel to the server, because that is where the tools run: a
    button that latched locally would show "Paused" while the agent went on
    moving the camera.

    This is the human's only control over the camera and visibility tools, since
    those are pre-allowed and never produce an approval card, so the round trip
    working is the whole safeguard rather than a second one. Tools that change
    nothing stay available on purpose, so the model can still look.
    """
    server, _session = chat_server
    page = browser.new_page()
    try:
        page.goto(server.viewer_url)
        _wait_connection_state(page, "live")
        page.wait_for_function("() => window.mesh && Object.keys(window.mesh.meshes).length === 1")

        page.click("#pauseBtn")
        # The button follows the server's announcement, not the click, so
        # waiting for it is waiting for the round trip to have completed.
        page.wait_for_function("() => window.mesh.store.getState().paused === true")
        assert page.locator("#pauseBtn").get_attribute("aria-pressed") == "true"
        assert server.bus.paused is True

        refused = _call_tool(server, "fit_view")
        assert refused["is_error"] is True
        assert "paused viewer control" in _tool_text(refused)
        assert "is_error" not in _call_tool(server, "get_view")

        page.click("#pauseBtn")
        page.wait_for_function("() => window.mesh.store.getState().paused === false")
        assert "is_error" not in _call_tool(server, "fit_view")
    finally:
        page.close()


def test_a_second_tab_shows_the_pause_another_tab_set(browser, chat_server):
    """The switch is one property of the server, so it is broadcast: a phone
    still offering to pause what the laptop already paused invites a click that
    changes nothing."""
    server, _session = chat_server
    first = browser.new_page()
    second = browser.new_page()
    try:
        first.goto(server.viewer_url)
        _wait_connection_state(first, "live")
        second.goto(server.viewer_url)
        _wait_connection_state(second, "live")

        first.click("#pauseBtn")
        second.wait_for_function("() => window.mesh.store.getState().paused === true")

        # And a tab that arrives afterwards learns it from the greeting, which
        # is the case no event can cover.
        third = browser.new_page()
        try:
            third.goto(server.viewer_url)
            _wait_connection_state(third, "live")
            third.wait_for_function("() => window.mesh.store.getState().paused === true")
        finally:
            third.close()
    finally:
        second.close()
        first.close()


def test_a_callout_the_agent_adds_appears_as_a_pin_without_a_reload(browser, chat_server):
    """The agent's half of the pointing, through its own tool rather than a
    hand-written file: `add_callout` writes the same `mesh-callouts.json` the
    published contract documents, so the callouts watcher notices and the pin
    arrives by push."""
    server, _session = chat_server
    page = browser.new_page()
    try:
        page.goto(server.viewer_url)
        _wait_connection_state(page, "live")

        result = _call_tool(server, "add_callout", {
            "point": [0, 0, 10], "comment": "moved this wall out 2mm",
            "part": "alpha", "label": "+Z"})
        assert "is_error" not in result, _tool_text(result)

        page.wait_for_function(
            "() => document.querySelectorAll('#agentPins .apin').length === 1",
            timeout=15000)
        assert "moved this wall out 2mm" in page.locator("#agentPins .apin").first.inner_text()
    finally:
        page.close()


def test_the_topbar_still_fits_a_narrow_viewport(browser, chat_server):
    """The topbar has gained a button per milestone, and a phone is a required
    workflow rather than a nice-to-have, so this is a regression guard on the
    row rather than on any one control: at 390 px nothing in it may overflow."""
    server, _session = chat_server
    page = browser.new_page(viewport={"width": 390, "height": 844})
    try:
        page.goto(server.viewer_url)
        _wait_connection_state(page, "live")
        overflow = page.evaluate(
            "() => { const b = document.getElementById('topbar');"
            " return b.scrollWidth - b.clientWidth; }")
        assert overflow <= 0, "the topbar overflows by %dpx at 390px wide" % overflow
        assert page.locator("#pauseBtn").is_visible()
    finally:
        page.close()


# 22. Chat pane: image attachments --------------------------------------------

# A real, decodable 1x1 PNG, not just bytes matching paths.sniff_image's
# magic-number check: the sent-turn thumbnail test loads this through a real
# `<img>` and reads it back over `/asset/<name>`, so a file that decoded to
# nothing would leave that assertion unable to tell a broken image apart
# from a wrong `src`.
_TINY_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY"
    "42YAAAAASUVORK5CYII=")


def _drop_files_onto(page, selector, files):
    """Dispatch a real `dragover` then `drop` `DragEvent` carrying `files` at
    the element `selector` matches, the same sequence a browser fires for an
    OS file drag: chat.js's own `dragover` handler must run first (it is what
    arms the pane as a drop target at all) or `drop` never fires.

    Playwright has no file-drop API of its own (`page.set_input_files` only
    reaches an `<input type=file>`), so the `File` objects and the
    `DataTransfer` carrying them are built inside the page, from
    `files` = `[(name, mime_type, base64_bytes), ...]`.
    """
    page.evaluate(
        """([sel, files]) => {
            const dt = new DataTransfer();
            for (const [name, mimeType, b64] of files) {
                const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
                dt.items.add(new File([bytes], name, {type: mimeType}));
            }
            const el = document.querySelector(sel);
            const opts = {dataTransfer: dt, bubbles: true, cancelable: true};
            el.dispatchEvent(new DragEvent('dragover', opts));
            el.dispatchEvent(new DragEvent('drop', opts));
        }""",
        [selector, files],
    )


def test_a_picker_upload_becomes_a_chip_then_an_image_path_block(browser, chat_server):
    """The first of the composer's three ways to attach an image: the file
    picker. The chip must reach its "done" (thumbnail shown) state before Send
    is clicked, proving the upload actually finished rather than merely having
    been started, and the blocks reach the session image first, text last.
    """
    server, session = chat_server
    page = browser.new_page()
    try:
        page.goto(server.viewer_url)
        _wait_connection_state(page, "live")

        page.set_input_files("#chatFileInput", {
            "name": "photo.png", "mimeType": "image/png", "buffer": _TINY_PNG_BYTES,
        })
        page.wait_for_selector("#chatAttachStrip .attachchip .attachthumb:not([hidden])")
        assert page.locator("#chatAttachStrip .attachchip.error").count() == 0

        page.fill("#chatInput", "what do you make of this?")
        page.click("#chatSend")

        _wait_until(lambda: len(session.submitted_turns) == 1,
                    message="the turn frame never reached AgentSession.submit_turn")
        blocks, _viewer = session.submitted_turns[0]
        assert len(blocks) == 2
        assert blocks[0]["type"] == "image_path"
        assert blocks[0]["path"].startswith("images/upload-") and blocks[0]["path"].endswith(".png")
        assert blocks[1] == {"type": "text", "text": "what do you make of this?"}
        assert (server.serve_dir / blocks[0]["path"]).is_file()

        # Sent, so the strip is cleared for whatever the human attaches next.
        assert page.locator("#chatAttachStrip .attachchip").count() == 0
    finally:
        page.close()


def test_a_dropped_file_becomes_a_chip_then_an_image_path_block(browser, chat_server):
    """The second of the composer's three ways to attach an image: a drop
    onto the whole chat pane, not only the composer row. Sent with no typed
    text, which is allowed, and the turn then carries the image block alone:
    an empty text block is refused by the API outright and would fail the whole
    turn, image included, so a composer that always appended one would break
    exactly the case it was meant to support.
    """
    server, session = chat_server
    page = browser.new_page()
    try:
        page.goto(server.viewer_url)
        _wait_connection_state(page, "live")

        _drop_files_onto(page, "#chat", [
            ("dropped.png", "image/png", base64.b64encode(_TINY_PNG_BYTES).decode()),
        ])
        page.wait_for_selector("#chatAttachStrip .attachchip .attachthumb:not([hidden])")

        page.click("#chatSend")

        _wait_until(lambda: len(session.submitted_turns) == 1,
                    message="the turn frame never reached AgentSession.submit_turn")
        blocks, _viewer = session.submitted_turns[0]
        assert len(blocks) == 1, blocks
        assert blocks[0]["type"] == "image_path"
        assert blocks[0]["path"].startswith("images/upload-")
        assert (server.serve_dir / blocks[0]["path"]).is_file()
    finally:
        page.close()


def test_an_oversized_or_wrong_typed_drop_is_refused_without_a_request(browser, chat_server):
    """`paths.py`'s own type and size limits, refused from the `File`
    object's own `type`/`size` before a single byte leaves the page: a large
    or wrong-typed file must fail at once, not
    after the whole body has streamed to the server only to be refused
    there. No request to `/upload` is the proof; a toast alone would also be
    produced by a refusal the server sent back.
    """
    server, session = chat_server
    page = browser.new_page()
    upload_requests = []
    # Matched on the path with any query string stripped, and anchored to the
    # end, so this does not also catch the unrelated static asset
    # `/static/js/uploads.js`, whose URL contains the same substring.
    page.on("request", lambda r: upload_requests.append(r)
            if r.url.split("?", 1)[0].endswith("/upload") else None)
    try:
        page.goto(server.viewer_url)
        _wait_connection_state(page, "live")

        page.set_input_files("#chatFileInput", {
            "name": "notes.txt", "mimeType": "text/plain", "buffer": b"not an image"})
        page.wait_for_function(
            "() => document.getElementById('toast').style.display === 'block'")
        assert "PNG, JPEG or WEBP" in page.evaluate(
            "() => document.getElementById('toast').textContent")
        assert page.locator("#chatAttachStrip .attachchip").count() == 0

        oversized = b"\x89PNG\r\n\x1a\n" + os.urandom(8 * 1024 * 1024)
        page.set_input_files("#chatFileInput", {
            "name": "big.png", "mimeType": "image/png", "buffer": oversized})
        page.wait_for_function(
            "() => document.getElementById('toast').textContent.includes('8 MB')")
        assert page.locator("#chatAttachStrip .attachchip").count() == 0

        assert upload_requests == []
        assert len(session.submitted_turns) == 0
    finally:
        page.close()


def test_a_sent_turns_user_bubble_shows_the_uploaded_thumbnail(browser, chat_server):
    """Fact 7's consequence: the bubble's thumbnail is
    built from the composer's own record (chat.js's `renderUserAttachments`),
    never from the agent's echo of the turn, so its `src` is a plain
    `/asset/<name>` string this test can also fetch directly, confirming the
    route serves exactly the bytes the picker uploaded rather than merely
    that some `<img>` tag exists.
    """
    server, session = chat_server
    page = browser.new_page()
    try:
        page.goto(server.viewer_url)
        _wait_connection_state(page, "live")

        page.set_input_files("#chatFileInput", {
            "name": "photo.png", "mimeType": "image/png", "buffer": _TINY_PNG_BYTES,
        })
        page.wait_for_selector("#chatAttachStrip .attachchip .attachthumb:not([hidden])")
        page.click("#chatSend")
        _wait_until(lambda: len(session.submitted_turns) == 1)

        # A turn row only exists once some event has touched its turn number
        # (store.js's `ensureTurn`); TurnEnd is the event a real reply ends
        # with.
        _emit_on_loop(server, session, session_base.TurnEnd(
            turn=1, stop_reason="end_turn", cost_usd=0.0))

        thumb = page.wait_for_selector("div.turn[data-turn='1'] .msg.user .attachthumbs img")
        src = thumb.get_attribute("src")
        assert src.startswith("/asset/")
        served = page.request.get(server.base_url + src)
        assert served.status == 200
        assert served.body() == _TINY_PNG_BYTES
    finally:
        page.close()


# 23. Chat pane: sketch overlay ------------------------------------------------

def test_drawing_a_sketch_stroke_never_moves_the_camera(browser, chat_server):
    """Sketch mode's whole reason to exist: the overlay canvas sits above the
    renderer's own canvas so that OrbitControls' listeners, bound to that
    canvas, never see a pointer event while a stroke is being drawn. A drag
    that reached OrbitControls would rotate the camera mid-stroke, leaving the
    points drawn before and after the move pointing at different geometry.
    """
    server, _session = chat_server
    page = browser.new_page()
    try:
        page.goto(server.viewer_url)
        _wait_connection_state(page, "live")
        page.wait_for_function("() => window.mesh && Object.keys(window.mesh.meshes).length === 1")

        before_pos = page.evaluate("() => window.mesh.camera.position.toArray()")
        before_target = page.evaluate("() => window.mesh.controls.target.toArray()")

        page.click("#chatSketchBtn")
        box = page.wait_for_selector("#app canvas.sketch-overlay").bounding_box()

        cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        page.mouse.move(cx - 50, cy - 30)
        page.mouse.down()
        page.mouse.move(cx, cy, steps=6)
        page.mouse.move(cx + 50, cy + 30, steps=6)
        page.mouse.up()

        after_pos = page.evaluate("() => window.mesh.camera.position.toArray()")
        after_target = page.evaluate("() => window.mesh.controls.target.toArray()")
        # abs=1e-6 rather than exact equality: OrbitControls recomputes its
        # position from spherical coordinates on every animation frame even
        # when idle, which reintroduces float rounding far below this
        # tolerance; a real rotate from the drag this test performs moves
        # the camera by whole units.
        assert after_pos == pytest.approx(before_pos, abs=1e-6), \
            "the camera moved while a stroke was being drawn"
        assert after_target == pytest.approx(before_target, abs=1e-6), \
            "the camera's target moved while a stroke was being drawn"
    finally:
        page.close()


# The stroke colour js/sketch.js draws with, as the bytes a decoded PNG
# reports. Sampled with a tolerance because the composite's own scaling and
# PNG's per-scanline filtering both leave a drawn line's edge pixels blended
# with what was under them, so an exact match would only ever find the middle
# of a thick stroke.
_STROKE_RGB = (0x35, 0xC7, 0xE0)
_STROKE_TOLERANCE = 24


def _composite_pixel_facts(page, asset_url):
    """Decode ``asset_url`` in the page and count what is in it.

    The browser's own PNG decoder is used rather than one written here: the
    bytes under test are the bytes the server serves, this is the decoder that
    will render them, and a hand-rolled inflate-and-defilter in a test file
    would be a second implementation to get wrong.
    """
    return page.evaluate(
        """async ([url, rgb, tol]) => {
            const res = await fetch(url);
            const bitmap = await createImageBitmap(await res.blob());
            const canvas = new OffscreenCanvas(bitmap.width, bitmap.height);
            const ctx = canvas.getContext('2d');
            ctx.drawImage(bitmap, 0, 0);
            const { data } = ctx.getImageData(0, 0, bitmap.width, bitmap.height);
            const colors = new Set();
            let strokePixels = 0;
            for (let i = 0; i < data.length; i += 4) {
                colors.add((data[i] << 16) | (data[i + 1] << 8) | data[i + 2]);
                if (Math.abs(data[i] - rgb[0]) <= tol
                    && Math.abs(data[i + 1] - rgb[1]) <= tol
                    && Math.abs(data[i + 2] - rgb[2]) <= tol) strokePixels += 1;
            }
            return { width: bitmap.width, height: bitmap.height,
                     strokePixels, distinctColors: colors.size };
        }""",
        [asset_url, list(_STROKE_RGB), _STROKE_TOLERANCE],
    )


def test_attaching_a_sketch_delivers_a_composited_image_path_block(browser, chat_server):
    """Attach's compositing contract: the uploaded file is the real render
    with the stroke drawn on top, not a blank canvas, and it reaches the
    model exactly the way any other composer attachment does, by handing the
    finished blob to the same `uploadImage` path
    the picker and drop handlers use.

    Both halves are checked on the served pixels rather than on the file's
    size, by handing the bytes back to the browser's own PNG decoder: a count
    of stroke-coloured pixels proves the stroke is in the image, and a count of
    distinct colours proves the render is, since a stroke drawn on an otherwise
    flat background cannot produce many while a shaded part trivially does. A
    size comparison would pass for the wrong reason, because a composite that
    lost its strokes lands within a few bytes of one that never had any.
    """
    server, session = chat_server
    page = browser.new_page()
    try:
        page.goto(server.viewer_url)
        _wait_connection_state(page, "live")
        page.wait_for_function("() => window.mesh && Object.keys(window.mesh.meshes).length === 1")

        page.click("#chatSketchBtn")
        box = page.wait_for_selector("#app canvas.sketch-overlay").bounding_box()
        cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        page.mouse.move(cx - 50, cy - 30)
        page.mouse.down()
        page.mouse.move(cx, cy, steps=6)
        page.mouse.move(cx + 50, cy + 30, steps=6)
        page.mouse.up()

        page.click("#sketchAttachBtn")
        page.wait_for_selector("#chatAttachStrip .attachchip .attachthumb:not([hidden])")
        page.wait_for_selector("#app canvas.sketch-overlay", state="detached")

        sketch_files = []
        deadline = time.time() + 5.0
        while time.time() < deadline and not sketch_files:
            sketch_files = list((server.serve_dir / "images").glob("sketch-*.png"))
            if not sketch_files:
                time.sleep(0.05)
        assert sketch_files, "no sketch-*.png landed under images/"
        sketch_bytes = sketch_files[0].read_bytes()
        assert sketch_bytes.startswith(b"\x89PNG\r\n\x1a\n")

        pixels = _composite_pixel_facts(page, "/asset/" + sketch_files[0].name)
        assert pixels["strokePixels"] > 100, (
            "the composite carries %d pixels of the stroke colour, so the stroke "
            "did not reach it" % pixels["strokePixels"])
        assert pixels["distinctColors"] > 16, (
            "the composite has only %d distinct colours, so it is a stroke on a "
            "flat background rather than a stroke on the render"
            % pixels["distinctColors"])

        page.fill("#chatInput", "look at this stroke")
        page.click("#chatSend")
        _wait_until(lambda: len(session.submitted_turns) == 1,
                    message="the turn frame never reached AgentSession.submit_turn")
        blocks, _viewer = session.submitted_turns[0]
        assert blocks[0]["type"] == "image_path"
        assert blocks[0]["path"].startswith("images/sketch-") and blocks[0]["path"].endswith(".png")
        assert blocks[1] == {"type": "text", "text": "look at this stroke"}
        assert (server.serve_dir / blocks[0]["path"]).is_file()
    finally:
        page.close()


def test_a_sketch_is_refused_when_the_view_moved_under_it(browser, chat_server):
    """The correspondence the whole feature rests on: a stroke means "this spot
    on the model", which is only true of the view it was drawn against.

    The agent's camera tools are pre-allowed, so it can reframe the view with
    no approval card while a human is mid-sketch, and only Pause stops it. If
    Attach composited regardless, the upload would show the new view wearing
    the old view's marks and the turn would tell the model that is where the
    human pointed. Nothing is uploaded and the strokes are kept, so the human
    can put the view back rather than starting over.
    """
    server, session = chat_server
    page = browser.new_page()
    upload_requests = []
    page.on("request", lambda r: upload_requests.append(r)
            if r.url.split("?", 1)[0].endswith("/upload") else None)
    try:
        page.goto(server.viewer_url)
        _wait_connection_state(page, "live")
        page.wait_for_function("() => window.mesh && Object.keys(window.mesh.meshes).length === 1")

        page.click("#chatSketchBtn")
        box = page.wait_for_selector("#app canvas.sketch-overlay").bounding_box()
        cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        page.mouse.move(cx - 40, cy)
        page.mouse.down()
        page.mouse.move(cx + 40, cy, steps=5)
        page.mouse.up()

        # The agent's own tool, through the real bus, which is how this happens
        # in practice rather than a camera nudged from the test.
        moved = _call_tool(server, "set_view", {"position": [80, 80, 80]})
        assert "is_error" not in moved, _tool_text(moved)

        page.click("#sketchAttachBtn")
        page.wait_for_function(
            "() => document.getElementById('toast').style.display === 'block'")
        assert "view moved" in page.evaluate(
            "() => document.getElementById('toast').textContent")

        assert upload_requests == []
        assert not list((server.serve_dir / "images").glob("sketch-*.png"))
        # Still in sketch mode, still holding the strokes.
        assert page.locator("#app canvas.sketch-overlay").count() == 1
        assert len(session.submitted_turns) == 0
    finally:
        page.close()


def test_send_waits_for_an_upload_still_in_flight(browser, chat_server):
    """Send is refused, visibly, while a chip is still uploading.

    A send allowed through would post a turn with no image in it and then carry
    that image on whatever was typed next, so the human's picture would arrive
    alongside a different question and read as having been ignored.
    """
    server, session = chat_server
    page = browser.new_page()
    try:
        page.goto(server.viewer_url)
        _wait_connection_state(page, "live")

        # The slot is reserved directly rather than by holding a real upload
        # open: a Playwright route handler blocking to delay the response also
        # blocks the thread these assertions run on, so the request would have
        # long since completed by the time they ran. What is under test is the
        # guard's reading of the state, and this is that state, put there by the
        # same mutator uploads.js calls.
        page.evaluate("() => window.mesh.store.reserveChatAttachment('upload')")
        page.wait_for_selector("#chatAttachStrip .attachchip .attachspinner:not([hidden])")

        page.fill("#chatInput", "what about this?")
        assert page.locator("#chatSend").is_disabled()
        assert "uploading" in page.locator("#chatSend").get_attribute("title")
        page.keyboard.press("Enter")
        assert len(session.submitted_turns) == 0, \
            "Enter sent the turn while an attachment was still uploading"

        # The chip lands, and only then is the turn allowed to carry it.
        page.evaluate("""() => {
            const id = window.mesh.store.getState().chat.attachments[0].id;
            window.mesh.store.completeChatAttachment(id, {
              path: 'images/upload-x.png', url: '/asset/upload-x.png',
              bytes: 12, mediaType: 'image/png' });
        }""")
        page.wait_for_function("() => !document.getElementById('chatSend').disabled")

        page.click("#chatSend")
        _wait_until(lambda: len(session.submitted_turns) == 1,
                    message="the turn frame never reached AgentSession.submit_turn")
        blocks, _viewer = session.submitted_turns[0]
        assert [b["type"] for b in blocks] == ["image_path", "text"]
    finally:
        page.close()


def test_escape_leaves_sketch_mode_with_nothing_uploaded(browser, chat_server):
    """Escape is a plain cancel, not a discard-and-report: nothing is
    uploaded, nothing is attached, and the request never leaves the page,
    the same "refused before a byte leaves" property the picker's own type
    and size guard has."""
    server, session = chat_server
    page = browser.new_page()
    upload_requests = []
    page.on("request", lambda r: upload_requests.append(r)
            if r.url.split("?", 1)[0].endswith("/upload") else None)
    try:
        page.goto(server.viewer_url)
        _wait_connection_state(page, "live")
        page.wait_for_function("() => window.mesh && Object.keys(window.mesh.meshes).length === 1")

        page.click("#chatSketchBtn")
        box = page.wait_for_selector("#app canvas.sketch-overlay").bounding_box()
        cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        page.mouse.move(cx - 30, cy)
        page.mouse.down()
        page.mouse.move(cx + 30, cy, steps=4)
        page.mouse.up()

        page.keyboard.press("Escape")
        page.wait_for_selector("#app canvas.sketch-overlay", state="detached")

        assert upload_requests == []
        assert page.locator("#chatAttachStrip .attachchip").count() == 0
        assert len(session.submitted_turns) == 0
    finally:
        page.close()


def test_leaving_sketch_mode_restores_orbit_controls(browser, chat_server):
    """The overlay must be actually removed, not merely hidden or left
    capturing pointer events underneath its own `display: none`: a real drag
    on the canvas after Cancel has to reach OrbitControls and move the
    camera, proving nothing sketch-related is still intercepting it."""
    server, _session = chat_server
    page = browser.new_page()
    try:
        page.goto(server.viewer_url)
        _wait_connection_state(page, "live")
        page.wait_for_function("() => window.mesh && Object.keys(window.mesh.meshes).length === 1")

        page.click("#chatSketchBtn")
        page.wait_for_selector("#app canvas.sketch-overlay")
        page.click("#sketchCancelBtn")
        page.wait_for_selector("#app canvas.sketch-overlay", state="detached")
        assert page.locator(".sketchbar").count() == 0

        before = page.evaluate("() => window.mesh.camera.position.toArray()")
        box = page.locator("#app canvas").first.bounding_box()
        cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        page.mouse.move(cx - 60, cy)
        page.mouse.down()
        page.mouse.move(cx + 60, cy + 30, steps=6)
        page.mouse.up()

        page.wait_for_function(
            "(before) => JSON.stringify(window.mesh.camera.position.toArray()) !== JSON.stringify(before)",
            arg=before)
    finally:
        page.close()


def test_the_pause_control_is_not_offered_when_there_is_no_agent(browser, token_mesh_server):
    """Viewer-only mode runs no tools, so there is nothing to pause and the
    server refuses the frame. A control that produced a refusal toast on every
    click would be worse than one that plainly cannot be pressed."""
    page = browser.new_page()
    try:
        page.goto(token_mesh_server.viewer_url)
        _wait_connection_state(page, "live")
        page.wait_for_function(
            "() => window.mesh.store.getState().chat.agentStatus === 'unavailable'")
        assert page.locator("#pauseBtn").is_disabled()
    finally:
        page.close()
