/**
 * Renderer, camera, controls, lights, grid, axes, sizing and the render
 * loop. Fit view and the up-axis toggle live here because both act on the
 * camera; they take no mesh table of their own, instead calling the
 * `getMeshes` callback the caller supplies, since the loaded meshes are a
 * side table owned by models.js, not scene state.
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

  function fitView() {
    const sph = visibleBounds();
    if (!sph) return;
    const r = sph.radius || 100;
    controls.target.copy(sph.center);
    const dir = new THREE.Vector3(1.1, -1.2, 0.75).normalize();
    camera.position.copy(sph.center).addScaledVector(dir, r * 2.6);
    camera.near = r / 50;
    camera.far = r * 50;
    camera.updateProjectionMatrix();
    controls.update();
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

  return { scene, camera, renderer, controls, fitView, markerRadius };
}
