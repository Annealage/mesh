/**
 * Renderer, camera, controls, lights, grid, axes, sizing and the render
 * loop. Fit view, the up-axis toggle, reading and setting the camera and
 * capturing the canvas all live here because every one of them acts on the
 * camera or the renderer; they take no mesh table of their own, instead
 * calling the `getMeshes` callback the caller supplies, since the loaded
 * meshes are a side table owned by models.js, not scene state.
 *
 * `cameraState`, `setView` and `captureView` are what js/commands.js answers
 * the server's camera and capture calls with. They are here rather than there
 * so that no second module does arithmetic on the camera: the depth range
 * after a move and the drawing-buffer rules for a screenshot are both
 * properties of this renderer, and a caller reasoning about them from outside
 * would be reasoning about state it cannot see.
 */

import * as THREE from "three";
import { OrbitControls } from "./vendor/OrbitControls.js";
import { makeLabelSprite } from "./sprites.js";
import { store } from "./store.js";

const AXLEN = 60;

/**
 * @param container element whose content box the renderer is sized to
 * @param getMeshes  () => {[rel]: THREE.Mesh}, the live mesh side table
 */
export function initScene(container, { getMeshes }) {
  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0x14161a);

  const camera = new THREE.PerspectiveCamera(45, 1, 0.5, 20000);
  camera.up.set(0, 0, 1); // default: Z up

  const renderer = new THREE.WebGLRenderer({ antialias: true });
  renderer.setPixelRatio(devicePixelRatio);
  container.appendChild(renderer.domElement);
  renderer.domElement.style.touchAction = "none"; // let OrbitControls own touch gestures

  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.08;
  controls.enableRotate = true;
  controls.enablePan = true;
  controls.enableZoom = true;
  // one finger = orbit, two fingers = pinch-zoom + pan
  controls.touches = { ONE: THREE.TOUCH.ROTATE, TWO: THREE.TOUCH.DOLLY_PAN };
  controls.mouseButtons = { LEFT: THREE.MOUSE.ROTATE, MIDDLE: THREE.MOUSE.DOLLY, RIGHT: THREE.MOUSE.PAN };

  scene.add(new THREE.HemisphereLight(0xffffff, 0x30343c, 1.1));
  const key = new THREE.DirectionalLight(0xffffff, 1.6);
  key.position.set(1, 0.7, 1.4);
  scene.add(key);
  const fill = new THREE.DirectionalLight(0xffffff, 0.6);
  fill.position.set(-1, -0.8, 0.4);
  scene.add(fill);

  const grid = new THREE.GridHelper(400, 40, 0x3a3f47, 0x282c32);
  grid.rotation.x = Math.PI / 2; // grid on XY (z=0)
  scene.add(grid);
  scene.add(new THREE.AxesHelper(AXLEN)); // X red, Y green, Z blue
  // labelled axis ends; colours match the AxesHelper.
  [
    ["X", "#ff4d4d", [AXLEN + 10, 0, 0]],
    ["Y", "#4dd24d", [0, AXLEN + 10, 0]],
    ["Z", "#4d8cff", [0, 0, AXLEN + 10]],
  ].forEach(([t, col, pos]) => {
    const s = makeLabelSprite(t, col);
    s.position.set(pos[0], pos[1], pos[2]);
    s.scale.set(14, 14, 1);
    scene.add(s);
  });

  function visibleBounds() {
    const box = new THREE.Box3();
    let any = false;
    Object.values(getMeshes()).forEach((m) => {
      if (m.visible) {
        box.expandByObject(m);
        any = true;
      }
    });
    return any ? box.getBoundingSphere(new THREE.Sphere()) : null;
  }

  /** Scale a marker to the visible scene so it reads on both small and large parts. */
  function markerRadius() {
    const sph = visibleBounds();
    const r = sph ? sph.radius : 100;
    return Math.max(1.5, r * 0.018);
  }

  // Returns whether it actually framed anything, which is false when no mesh
  // is both loaded and visible. The click handler and the keyboard shortcut
  // ignore that; a tool call reports it, because "fitted" and "there was
  // nothing to fit" are different answers and the camera looks identical
  // either way.
  function fitView() {
    const sph = visibleBounds();
    if (!sph) return false;
    const r = sph.radius || 100;
    controls.target.copy(sph.center);
    const dir = new THREE.Vector3(1.1, -1.2, 0.75).normalize();
    camera.position.copy(sph.center).addScaledVector(dir, r * 2.6);
    camera.near = r / 50;
    camera.far = r * 50;
    camera.updateProjectionMatrix();
    controls.update();
    return true;
  }

  function round(v, d = 3) {
    const f = Math.pow(10, d);
    return Math.round(v * f) / f;
  }

  /**
   * The camera as plain numbers, for a tool to read and for the reply to
   * every command that moves it. Rounded, because a camera position is read
   * by a model and by a human, and seventeen significant figures of float
   * noise is not information either of them wants.
   */
  function cameraState() {
    return {
      position: [round(camera.position.x), round(camera.position.y), round(camera.position.z)],
      target: [round(controls.target.x), round(controls.target.y), round(controls.target.z)],
      up_axis: store.getState().upAxis,
      fov: camera.fov,
      size: { width: renderer.domElement.width, height: renderer.domElement.height },
    };
  }

  /**
   * Move the camera, its target, or both, and keep the depth range usable.
   *
   * `near` and `far` are recomputed from the new eye-to-target distance in
   * the same proportions `fitView` uses (there, distance is `r * 2.6`, near
   * is `r / 50` and far is `r * 50`). Without that, a camera moved from a
   * fit on a 10 mm part out to look at a 400 mm one keeps the previous
   * fit's `far` and the part is simply clipped away, which reads as an
   * empty view rather than as a camera in the wrong place.
   */
  function setView({ position, target }) {
    if (target) controls.target.set(target[0], target[1], target[2]);
    if (position) camera.position.set(position[0], position[1], position[2]);
    const dist = camera.position.distanceTo(controls.target) || 1;
    camera.near = dist / 130;
    camera.far = dist * 20;
    camera.updateProjectionMatrix();
    controls.update();
    return cameraState();
  }

  /**
   * Render one frame and return it as a data URL, plus the encoding used.
   *
   * Synchronous from the render to the last `toDataURL`, and that is
   * load-bearing rather than incidental: the renderer is created without
   * `preserveDrawingBuffer`, so the drawing buffer is cleared when the
   * browser next composites, and any `await` in here would let a
   * `requestAnimationFrame` land in between and hand back a blank image.
   *
   * `encodings` is tried in order and the first result within `maxChars` is
   * returned, so an ordinary view comes back as lossless PNG and a busy one
   * falls back to JPEG rather than producing a frame the server's inbound
   * ceiling would reject (see `http/ws.py`'s MAX_WS_MESSAGE: an oversized
   * frame is not refused, it drops the connection). Returns null when even
   * the last encoding is too large, which the caller reports as a failure.
   *
   * `width`, when given, is the exact pixel width of the returned image:
   * the device pixel ratio is set to 1 for the capture, because otherwise a
   * request for 1024 would return 2048 pixels on a retina display and quietly
   * pass whatever cap the caller thought it was setting.
   */
  function captureView({ width, encodings, maxChars }) {
    const canvas = renderer.domElement;
    const cssSize = renderer.getSize(new THREE.Vector2());
    const previousRatio = renderer.getPixelRatio();
    const resize = !!width && width !== canvas.width;
    if (resize) {
      const height = Math.max(1, Math.round((width * canvas.height) / canvas.width));
      renderer.setPixelRatio(1);
      renderer.setSize(width, height, false);
    }
    try {
      renderer.render(scene, camera);
      for (const [format, quality] of encodings) {
        const image = canvas.toDataURL("image/" + format, quality);
        if (image.length <= maxChars) {
          return { image, format, width: canvas.width, height: canvas.height };
        }
      }
      return null;
    } finally {
      if (resize) {
        renderer.setPixelRatio(previousRatio);
        renderer.setSize(cssSize.x, cssSize.y, false);
      }
    }
  }

  document.getElementById("fit").addEventListener("click", fitView);
  addEventListener("keydown", (e) => {
    if (e.target && (e.target.tagName === "TEXTAREA" || e.target.tagName === "INPUT")) return;
    if (e.key === "f" || e.key === "F") fitView();
  });

  // Some CAD tools export Y-up STLs instead of Z-up; flip camera.up and refit.
  const upBtn = document.getElementById("upBtn");
  function applyUpAxis(axis) {
    camera.up.set(0, axis === "y" ? 1 : 0, axis === "z" ? 1 : 0);
    upBtn.textContent = axis === "z" ? "Z-up" : "Y-up";
  }
  applyUpAxis(store.getState().upAxis);
  upBtn.addEventListener("click", () => {
    store.setUpAxis(store.getState().upAxis === "z" ? "y" : "z");
  });
  store.subscribe("upAxis", (state) => {
    applyUpAxis(state.upAxis);
    fitView();
  });

  // The renderer is sized from the container's own content box, via
  // ResizeObserver, never from innerWidth/innerHeight: opening the side
  // panel changes that box without firing a window resize event, which a
  // window-resize listener would simply never see.
  //
  // A hidden pane reports a 0-sized box. renderer.setSize(0, 0) and an
  // aspect of 0/0 produce a NaN projection matrix and a canvas that never
  // recovers even once the pane becomes visible again, so a zero dimension
  // is skipped entirely here rather than applied. `haveSize` separately
  // stops the render loop below from drawing into that state;
  // requestAnimationFrame keeps running regardless, so the next real
  // observation (the pane reappearing) resumes drawing with no extra
  // wiring.
  let haveSize = false;
  const ro = new ResizeObserver((entries) => {
    const { width, height } = entries[0].contentRect;
    if (width === 0 || height === 0) {
      haveSize = false;
      return;
    }
    camera.aspect = width / height;
    camera.updateProjectionMatrix();
    renderer.setSize(width, height);
    haveSize = true;
  });
  ro.observe(container);

  (function loop() {
    requestAnimationFrame(loop);
    controls.update();
    if (haveSize) renderer.render(scene, camera);
  })();

  return { scene, camera, renderer, controls, fitView, markerRadius, cameraState,
           setView, captureView };
}
