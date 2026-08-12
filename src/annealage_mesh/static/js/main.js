/**
 * Bootstrap and wiring. Creates the one side table of loaded THREE.Mesh
 * objects and hands it to the three modules that each need a different view
 * of it (three-scene.js to fit and scale against, models.js to fill,
 * pins.js to raycast against), then wires up the debug surface.
 */

import { store } from "./store.js";
import { initScene } from "./three-scene.js";
import { initModels } from "./models.js";
import { initPins } from "./pins.js";
import { initMeasure } from "./measure.js";
import { initLayout } from "./layout.js";

const appEl = document.getElementById("app");

// rel -> THREE.Mesh. Never store state; see store.js's module doc for why.
const meshes = {};

const scene3d = initScene(appEl, { getMeshes: () => meshes });

initModels({ scene: scene3d.scene, fitView: scene3d.fitView, meshes });

initPins({
  scene: scene3d.scene,
  camera: scene3d.camera,
  controls: scene3d.controls,
  renderer: scene3d.renderer,
  markerRadius: scene3d.markerRadius,
  getMeshes: () => meshes,
});

initMeasure({ scene: scene3d.scene, markerRadius: scene3d.markerRadius });

initLayout();

// Inspection surface for the browser console and for the Playwright tests
// in tests/test_viewer_e2e.py. Not read by any other module in this tree.
window.mesh = {
  store,
  scene: scene3d.scene,
  camera: scene3d.camera,
  controls: scene3d.controls,
  renderer: scene3d.renderer,
  meshes,
};
