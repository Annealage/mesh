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
import { initWs } from "./ws.js";
import { initChat } from "./chat.js";
import { initCommands } from "./commands.js";
import { initSketch } from "./sketch.js";

const appEl = document.getElementById("app");

// rel -> THREE.Mesh. Never store state; see store.js's module doc for why.
const meshes = {};

const scene3d = initScene(appEl, { getMeshes: () => meshes });

const modelsApi = initModels({
  scene: scene3d.scene,
  fitView: scene3d.fitView,
  meshes,
});

const pinsApi = initPins({
  scene: scene3d.scene,
  camera: scene3d.camera,
  controls: scene3d.controls,
  renderer: scene3d.renderer,
  markerRadius: scene3d.markerRadius,
  getMeshes: () => meshes,
});

initMeasure({ scene: scene3d.scene, markerRadius: scene3d.markerRadius });

initLayout();

// `wsApi` is assigned after initWs runs below, but the `send` callbacks
// chat.js and commands.js are given are only ever invoked later, from a user
// interaction or an inbound frame, by which point the assignment has happened;
// this closure is what lets those modules and ws.js be constructed in either
// order despite each needing a handle to the other.
let wsApi;
const send = (frame) => wsApi && wsApi.send(frame);

const chatApi = initChat({ send });
const commandsApi = initCommands({ scene3d, send });
initSketch({ container: appEl, captureView: scene3d.captureView,
             cameraState: scene3d.cameraState });

// The 1.5s /callouts poll is pins.js's fallback for whenever ws.js decides
// the socket is not live; ws.js owns that decision, pins.js only owns the
// poll's mechanics, so the three functions cross the boundary here.
wsApi = initWs({
  startCalloutsPoll: pinsApi.startCalloutsPoll,
  stopCalloutsPoll: pinsApi.stopCalloutsPoll,
  refetchCallouts: pinsApi.refetchCallouts,
  refetchModels: modelsApi.refetchModels,
  onHello: chatApi.handleHello,
  onAgentEvent: chatApi.handleEvent,
  onPaused: commandsApi.setPausedFromServer,
  dispatchCall: commandsApi.dispatch,
});

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
