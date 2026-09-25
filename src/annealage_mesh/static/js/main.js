/**
 * Bootstrap and wiring. Creates the one side table of loaded THREE.Mesh
 * objects and hands it to the three modules that each need a different view
 * of it (three-scene.js to fit and scale against, models.js to fill,
 * pins.js to raycast against), then mounts the agent layer's front end
 * (`agent/*.js`: the chat pane, the socket, the settings window, the pause
 * control and the layout) with Mesh's parts plugged into it, and wires up the
 * debug surface.
 *
 * This file is the one place the agent layer's modules meet Mesh's: every
 * Mesh-specific thing they need (which events Mesh handles, what to refetch
 * on a reconnect, how to fall back to polling, which tabs exist, what the
 * saved up axis means) is passed in here, so none of them names a Mesh module.
 */

import { initChat } from "agent/chat.js";
import { initLayout } from "agent/layout.js";
import { initPause } from "agent/pause.js";
import { initSettings } from "agent/settings.js";
import { initWs } from "agent/ws.js";
import { store } from "./store.js";
import { initScene } from "./three-scene.js";
import { initModels } from "./models.js";
import { initPins } from "./pins.js";
import { initMeasure } from "./measure.js";
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

// The chat pane is the third pane at wide viewports and the third tab at
// narrow ones, after the model and the review panel.
initLayout({
  tabs: [
    { id: "model", label: "Model", target: "#app" },
    { id: "review", label: "Review", target: "#side" },
    { id: "chat", label: "Chat", target: "#chat" },
  ],
});

// `wsApi` is assigned after initWs runs below, but the `send` callbacks
// chat.js and pause.js are given are only ever invoked later, from a user
// interaction or an inbound frame, by which point the assignment has happened;
// this closure is what lets those modules and ws.js be constructed in either
// order despite each needing a handle to the other.
let wsApi;
const send = (frame) => wsApi && wsApi.send(frame);

const chatApi = initChat({
  send,
  agentTitles: {
    unavailable:
      "The agent is not available right now. The viewer, pins and Submit keep working regardless.",
  },
});
const pauseApi = initPause({ send });
const commandsApi = initCommands({ scene3d });
initSketch({ container: appEl, captureView: scene3d.captureView,
             cameraState: scene3d.cameraState });

// The 1.5s /callouts poll is pins.js's fallback for whenever ws.js decides
// the socket is not live; ws.js owns that decision, pins.js only owns the
// poll's mechanics, so the poll's controls cross the boundary here.
wsApi = initWs({
  onEvent: {
    callouts_changed: () => pinsApi.refetchCallouts(),
    models_changed: () => modelsApi.refetchModels(),
  },
  onLive: () => {
    pinsApi.stopCalloutsPoll();
    // One refetch of each on every (re)connect, in addition to reacting to
    // their events above: a `callouts_changed` or `models_changed` pushed
    // while the socket was down is not in the replay this page will act on
    // once it is older than the server's ring, so a callout written or a part
    // regenerated during the gap would otherwise stay stale until something
    // else changed.
    pinsApi.refetchCallouts();
    modelsApi.refetchModels();
  },
  onFallback: pinsApi.startCalloutsPoll,
  connTitles: {
    live: "Live: callouts update without a reload.",
    polling:
      "Live updates are unavailable right now; falling back to checking for callouts every 1.5s.",
  },
  onHello: chatApi.handleHello,
  onAgentEvent: chatApi.handleEvent,
  onPaused: pauseApi.setPausedFromServer,
  onRefused: chatApi.handleRefused,
  dispatchCall: commandsApi.dispatch,
});

const settingsApi = initSettings({
  // Mesh's one load-effect setting. Only a value that differs is written:
  // this read arrives well after the first frame, and by then the agent may
  // already have moved the camera. `three-scene.js` reorients the view
  // whenever `upAxis` is announced, so announcing the axis it is already on
  // would throw away a view a tool had just set.
  onLoad: (settings) => {
    const axis = settings.up_axis && settings.up_axis.value;
    if ((axis === "z" || axis === "y") && axis !== store.getState().upAxis) {
      store.setUpAxis(axis);
    }
  },
});

// Saved viewer preferences are applied to this page as soon as they arrive,
// which is after the first frame: the alternative is blocking the whole boot on
// an HTTP round trip to learn which axis is up, and a model that appears
// immediately and then settles is better than one that waits.
settingsApi.load();

// Inspection surface for the browser console and for the Playwright tests
// in tests/test_viewer_e2e.py. Not read by any other module in this tree.
window.mesh = {
  settings: settingsApi,
  store,
  scene: scene3d.scene,
  camera: scene3d.camera,
  controls: scene3d.controls,
  renderer: scene3d.renderer,
  meshes,
};
