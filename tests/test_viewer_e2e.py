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

import glob
import json
import os
import socket
import struct
import threading
import time

import pytest

pytest.importorskip("playwright.sync_api")

from playwright.sync_api import sync_playwright

from annealage_mesh import app as mesh_app


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
    """

    def __init__(self, serve_dir):
        self.serve_dir = serve_dir
        self.host = "127.0.0.1"
        self.port = _free_port()
        self.base_url = "http://%s:%d" % (self.host, self.port)
        self._loop = None
        self._task = None
        self._thread = None
        self._ready = threading.Event()

    def start(self):
        def _run():
            import asyncio

            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            # on_ready is threading.Event.set: synchronous, non-blocking,
            # not awaitable, so run() proceeds immediately after calling it
            # rather than waiting on anything that touches this same loop.
            coro = mesh_app.run(self.serve_dir, self.host, self.port, on_ready=self._ready.set)
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
        if self._loop is None:
            return
        self._loop.call_soon_threadsafe(self._task.cancel)
        self._thread.join(timeout=5)


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

        page.click("#app canvas")
        page.wait_for_function("window.mesh.store.getState().pins.length === 1")

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
