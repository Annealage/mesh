/**
 * Sketch mode: draw directly over the 3D pane to point at exact geometry,
 * entered from a control in the composer rather than the topbar, since a
 * sketch is an attachment to the message being composed, not a viewer-wide
 * mode (and the topbar has no room left at 390px; see its own overflow
 * guard in tests/test_viewer_e2e.py).
 *
 * An overlay canvas, sized from the same container box three-scene.js sizes
 * the renderer from, sits above the renderer's own canvas for as long as
 * sketch mode is on. Being on top, and appended after that canvas, is what
 * keeps OrbitControls from ever seeing a pointer event while a stroke is
 * drawn: a pointerdown hit-tests to whichever element is topmost at that
 * pixel, and OrbitControls' listeners are bound to the renderer's own
 * canvas, a sibling this overlay physically covers, so those listeners are
 * simply never invoked. A camera that moved mid-stroke would leave the
 * points drawn before and after the move pointing at different parts of the
 * geometry, which would defeat the feature.
 *
 * A stroke is an array of `{x, y}` points normalised to the overlay's own
 * content box (0..1 on both axes), not device pixels. What that buys is one
 * thing only: a stroke keeps its position relative to the overlay across a
 * backing-store change, so a devicePixelRatio change redraws it where it was
 * rather than at a stale pixel offset. It buys nothing at all about which
 * geometry lies underneath. A camera move, and equally a resize, reframes the
 * render, so the same fraction of the box is then a different point on the
 * model, and a composite built after either would show one view wearing
 * another view's marks. Attach therefore compares the camera and the box
 * against what they were when the first stroke was drawn and refuses when
 * they no longer agree; the strokes are kept so the human can decide what to
 * do. The agent's own camera tools are pre-allowed, so this is not a rare
 * case: only Pause stands between them and the view being drawn on.
 *
 * Attach composites the strokes onto captureView's own render, at that
 * image's natural size, and uploads the result through uploadImage, the one
 * path every composer attachment goes through. This module never touches
 * chat DOM or chat state itself: a successful upload lands in
 * state.chat.attachments, and chat.js's existing subscription to that state
 * is what turns it into a chip. A failed upload has already been reported
 * by uploadImage's own toast, so the overlay is left open rather than
 * silently discarding the strokes.
 */

import { uploadImage } from "./uploads.js";
import { toast } from "./ui.js";
import { activateTab } from "./layout.js";
import { MAX_CAPTURE_CHARS, MAX_CAPTURE_WIDTH } from "./commands.js";

// Fixed here because there is no settings layer yet to carry a colour or
// width choice (a later settings pane owns this). #35c7e0 is the same cyan
// already used for agent callouts and the "live" connection state, and
// reads clearly against both a light-coloured model and this viewer's dark
// background.
const STROKE_COLOR = "#35c7e0";
const STROKE_WIDTH = 3; // CSS pixels in the overlay; scaled for the composite

export function initSketch({ container, captureView, cameraState }) {
  const sketchBtn = document.getElementById("chatSketchBtn");

  let active = false;
  let overlay = null;
  let ctx = null;
  let ro = null;
  let strokes = []; // Point[][], each point {x, y} normalised 0..1
  let current = null;
  let drawing = false;
  // The view the first stroke of this sketch was drawn against, captured at
  // that pointerdown and compared again at Attach. Null until then.
  let drawnAgainst = null;
  let attaching = false;

  // Which view a set of strokes means. The camera's own position, target and
  // field of view, plus the shape of the box it is projected into: an aspect
  // change reframes the render just as a move does, so a resize has to count
  // as a different view even though the camera did not move.
  function viewFingerprint() {
    const cam = cameraState();
    const rect = container.getBoundingClientRect();
    return JSON.stringify({
      position: cam.position,
      target: cam.target,
      fov: cam.fov,
      width: Math.round(rect.width),
      height: Math.round(rect.height),
    });
  }

  function normPoint(e) {
    const rect = overlay.getBoundingClientRect();
    return {
      x: Math.min(1, Math.max(0, (e.clientX - rect.left) / rect.width)),
      y: Math.min(1, Math.max(0, (e.clientY - rect.top) / rect.height)),
    };
  }

  function strokePath(strokeCtx, stroke, w, h) {
    if (stroke.length < 2) return;
    strokeCtx.beginPath();
    strokeCtx.moveTo(stroke[0].x * w, stroke[0].y * h);
    stroke.slice(1).forEach((p) => strokeCtx.lineTo(p.x * w, p.y * h));
    strokeCtx.stroke();
  }

  function allStrokes() {
    return current && current.length > 1 ? [...strokes, current] : strokes;
  }

  function redraw() {
    const w = overlay.clientWidth, h = overlay.clientHeight;
    ctx.clearRect(0, 0, w, h);
    ctx.strokeStyle = STROKE_COLOR;
    ctx.lineWidth = STROKE_WIDTH;
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    allStrokes().forEach((stroke) => strokePath(ctx, stroke, w, h));
  }

  // The overlay's backing store tracks devicePixelRatio, like the renderer's
  // own canvas, so a stroke reads as crisply as the render underneath it;
  // the transform below is what lets every draw call above stay in
  // CSS-pixel units regardless of that backing-store resolution.
  function resizeOverlay() {
    const rect = container.getBoundingClientRect();
    if (rect.width === 0 || rect.height === 0) return; // a hidden pane reports zero; nothing to size yet
    const ratio = devicePixelRatio || 1;
    overlay.width = Math.max(1, Math.round(rect.width * ratio));
    overlay.height = Math.max(1, Math.round(rect.height * ratio));
    overlay.style.width = rect.width + "px";
    overlay.style.height = rect.height + "px";
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    redraw();
  }

  function onPointerDown(e) {
    overlay.setPointerCapture(e.pointerId);
    if (drawnAgainst === null) drawnAgainst = viewFingerprint();
    current = [normPoint(e)];
    drawing = true;
    redraw();
  }

  function onPointerMove(e) {
    if (!drawing) return;
    current.push(normPoint(e));
    redraw();
  }

  function endStroke(e) {
    if (!drawing) return;
    drawing = false;
    if (current.length > 1) strokes.push(current);
    current = null;
    try { overlay.releasePointerCapture(e.pointerId); } catch (err) { /* capture already gone */ }
    redraw();
  }

  function onKeydown(e) {
    if (e.key === "Escape") { e.preventDefault(); cancel(); }
  }

  function undo() { strokes.pop(); redraw(); }

  // Clearing forgets which view the strokes belonged to as well: the next
  // stroke establishes it again, so drawing after a camera move is allowed
  // and only mixing the two is not.
  function clear() { strokes = []; current = null; drawnAgainst = null; redraw(); }

  function buildToolbar() {
    const bar = document.createElement("div");
    bar.className = "sketchbar";
    const addButton = (id, label, handler, cls) => {
      const b = document.createElement("button");
      b.type = "button";
      b.id = id;
      if (cls) b.className = cls;
      b.textContent = label;
      b.addEventListener("click", handler);
      bar.appendChild(b);
    };
    addButton("sketchUndoBtn", "Undo", undo);
    addButton("sketchClearBtn", "Clear", clear);
    addButton("sketchCancelBtn", "Cancel", cancel);
    addButton("sketchAttachBtn", "Attach", attach, "primary");
    return bar;
  }

  function enter() {
    if (active) return;
    active = true;
    strokes = [];
    current = null;
    drawing = false;
    drawnAgainst = null;
    attaching = false;

    // At 900px and below the 3D pane is one tab among three; entering
    // sketch mode with no way to see the canvas would leave the sketch
    // blind, so bringing the Model tab forward is part of entering.
    activateTab("model");

    overlay = document.createElement("canvas");
    overlay.className = "sketch-overlay";
    ctx = overlay.getContext("2d");
    container.appendChild(overlay);
    container.appendChild(buildToolbar());

    ro = new ResizeObserver(resizeOverlay);
    ro.observe(container);
    resizeOverlay();

    overlay.addEventListener("pointerdown", onPointerDown);
    overlay.addEventListener("pointermove", onPointerMove);
    overlay.addEventListener("pointerup", endStroke);
    overlay.addEventListener("pointercancel", endStroke);
    addEventListener("keydown", onKeydown);
    sketchBtn.classList.add("on");
  }

  function teardown() {
    active = false;
    drawing = false;
    strokes = [];
    current = null;
    drawnAgainst = null;
    attaching = false;
    if (ro) { ro.disconnect(); ro = null; }
    if (overlay) { overlay.remove(); overlay = null; ctx = null; }
    const bar = container.querySelector(".sketchbar");
    if (bar) bar.remove();
    removeEventListener("keydown", onKeydown);
    sketchBtn.classList.remove("on");
  }

  function cancel() { teardown(); }

  function loadImage(dataUrl) {
    return new Promise((resolve, reject) => {
      const img = new Image();
      img.onload = () => resolve(img);
      img.onerror = () => reject(new Error("could not decode the captured render"));
      img.src = dataUrl;
    });
  }

  async function attach() {
    const finished = allStrokes();
    if (!finished.length) {
      toast("Draw at least one stroke before attaching.", false);
      return;
    }
    // One composite per click. Without this a second click while the first
    // upload is in flight would write a second identical file into the
    // project and take a second attachment slot for it.
    if (attaching) return;
    // The view has to be the one the strokes were drawn on, or the composite
    // would tell the model the human pointed somewhere they did not.
    if (drawnAgainst !== null && viewFingerprint() !== drawnAgainst) {
      toast("The view moved since you started drawing, so the marks no longer "
            + "line up. Clear and draw again, or put the view back.", false);
      return;
    }
    attaching = true;
    setAttachEnabled(false);
    try {
      await composeAndUpload(finished);
    } finally {
      attaching = false;
      setAttachEnabled(true);
    }
  }

  function setAttachEnabled(enabled) {
    const btn = container.querySelector("#sketchAttachBtn");
    if (btn) btn.disabled = !enabled;
  }

  async function composeAndUpload(finished) {
    // Synchronous, and before any await: three-scene.js's renderer has no
    // preserveDrawingBuffer, so the pixels captureView reads are only
    // guaranteed to still be there for the render call that produced them.
    // Bounded by the same width and character ceiling a capture_view tool
    // call uses, since this image has the same destination: a full
    // device-pixel canvas from a large display would be stored but refused as
    // too large to send inline, which is the opposite of the point.
    const captured = captureView({
      width: MAX_CAPTURE_WIDTH,
      encodings: [["png", 1], ["jpeg", 0.92], ["jpeg", 0.8]],
      maxChars: MAX_CAPTURE_CHARS,
    });
    if (!captured) {
      toast("Could not capture the current view to sketch on.", false);
      return;
    }
    const overlayWidth = overlay.clientWidth;
    let image;
    try {
      image = await loadImage(captured.image);
    } catch (err) {
      toast("Could not build the sketch image.", false);
      return;
    }
    const canvas = document.createElement("canvas");
    canvas.width = captured.width;
    canvas.height = captured.height;
    const cctx = canvas.getContext("2d");
    cctx.drawImage(image, 0, 0, canvas.width, canvas.height);
    // Points are normalised to the overlay's own CSS-pixel box; scaling the
    // line width by the ratio between this canvas and that box keeps a
    // stroke's apparent thickness the same in the upload as it was on
    // screen, even though the two canvases rarely share a pixel size.
    const scale = overlayWidth ? canvas.width / overlayWidth : 1;
    cctx.strokeStyle = STROKE_COLOR;
    cctx.lineWidth = STROKE_WIDTH * scale;
    cctx.lineCap = "round";
    cctx.lineJoin = "round";
    finished.forEach((stroke) => strokePath(cctx, stroke, canvas.width, canvas.height));
    const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/png"));
    if (!blob) {
      toast("Could not build the sketch image.", false);
      return;
    }
    const attachment = await uploadImage(blob, "sketch");
    // A null return means uploadImage already told the human why, via its
    // own toast; the overlay stays open so the strokes are not lost.
    if (!attachment) return;
    teardown();
    // The chip and the composer are in the chat pane, which at 900px and
    // below is a different tab from the one entering sketch mode switched to.
    activateTab("chat");
  }

  sketchBtn.addEventListener("click", () => (active ? cancel() : enter()));
}
