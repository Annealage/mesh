/**
 * Mesh's slice of the page state, and the one `store` object every Mesh
 * module imports.
 *
 * The state itself, the commit and the subscriptions belong to the agent
 * layer's store (`agent/store.js`, whose header documents the single-writer
 * discipline both halves follow and the chat, connection, pause and layout
 * keys it owns). This module declares Mesh's keys there with `defineSlice`
 * and builds its mutators on the `update` function that returns, so the
 * viewer's keys have exactly one writer, this file, and every change still
 * goes through the one commit queue the chat pane's changes do. `store` below
 * is the agent layer's store with these mutators added, which is why a Mesh
 * module reads `store.getState().chat` and `store.getState().pins` through the
 * same object.
 *
 * The one-writer rule is what makes the checkbox-vs-mesh visibility binding
 * in models.js bidirectional: both are readers of the same
 * `state.visibility`, and the only way either one changes is through
 * `setVisibility`, so a programmatic call moves both.
 *
 * No THREE object is ever a state value. A pin's location is a 3-element
 * number array, not a THREE.Vector3; a model's colour is a hex number, not a
 * THREE.Color. Any THREE object that represents a piece of state, a pin's
 * marker mesh, a loaded model's Mesh, lives in a side table in whichever
 * module created it (pins.js, models.js) and is reconciled against store
 * state by a subscriber; it never becomes a state value itself. That is what
 * lets a pin arrive over the socket through `addPin` exactly as one placed by
 * a pointer event does.
 *
 * State shape (this slice's keys):
 *   models        [{name, file, path, rel, label, color}, ...]
 *                 manifest entries as fetched, plus a display `color`
 *                 (a hex number) assigned by models.js from a fixed
 *                 palette. `rel` is the key used everywhere else. Not to be
 *                 confused with `chat.model`, the LLM backend's model.
 *   visibility    {rel: boolean}
 *                 per-part show/hide, keyed by the same `rel` as `models`.
 *   mode          'nav' | 'annotate'
 *   upAxis        'z' | 'y'
 *   pins          [{id, part, rel, point, normal, faceIndex, label,
 *                   comment}, ...]
 *                 user-authored pins. `part` is the model's display label
 *                 at the moment the pin was placed (kept verbatim for
 *                 /submit, which is a published contract); `label` is the
 *                 picked face's axis-aligned direction, e.g. '+X'.
 *   selectedPinId number | null
 *   callouts      [...]
 *                 the latest agent-authored callout list, exactly as
 *                 /callouts returned it, used for the sidebar list, the
 *                 measure dropdowns and the agent markers.
 *   dirty         boolean
 *                 true once a pin has been added, edited or removed since
 *                 the last successful submit.
 *   showUser      boolean, showAgent boolean
 *                 the two legend toggles.
 *   measure       {a: string, b: string}
 *                 the two measure-dropdown selections, each a measurable
 *                 key ('u<id>' for a user pin, 'a<id>' for an agent pin) or
 *                 '' for unset.
 */

import { defineSlice, store as pageStore } from "agent/store.js";

const update = defineSlice({
  models: Object.freeze([]),
  visibility: Object.freeze({}),
  mode: "nav",
  upAxis: "z",
  pins: Object.freeze([]),
  selectedPinId: null,
  callouts: Object.freeze([]),
  dirty: false,
  showUser: true,
  showAgent: true,
  measure: Object.freeze({ a: "", b: "" }),
});

// Assigns pin ids. Kept outside `state` because it is a generator, not a
// fact about the current app; a pin's id, once assigned, is the fact.
let nextPinId = 1;

function setModels(models) {
  update(["models", "visibility"], (state) => {
    const frozenModels = Object.freeze(models.map((m) => Object.freeze({ ...m })));
    // Default visibility (first model shown, rest hidden) applies only to a
    // rel not already in the map, so a rescan in a later milestone would
    // not undo a toggle the reviewer already made.
    const visibility = { ...state.visibility };
    frozenModels.forEach((m, i) => {
      if (!(m.rel in visibility)) visibility[m.rel] = i === 0;
    });
    return { models: frozenModels, visibility: Object.freeze(visibility) };
  });
}

function setVisibility(rel, on) {
  update(["visibility"], (state) => ({
    visibility: Object.freeze({ ...state.visibility, [rel]: !!on }),
  }));
}

function setMode(mode) {
  update(["mode"], () => ({ mode }));
}

function setUpAxis(axis) {
  update(["upAxis"], () => ({ upAxis: axis }));
}

function addPin(data) {
  const id = nextPinId++;
  update(["pins", "selectedPinId", "dirty"], (state) => {
    const pin = Object.freeze({ id, comment: "", ...data });
    return { pins: Object.freeze([...state.pins, pin]), selectedPinId: id, dirty: true };
  });
  return id;
}

function removePin(id) {
  update(["pins", "selectedPinId", "dirty"], (state) => {
    const pins = state.pins.filter((p) => p.id !== id);
    const selectedPinId = state.selectedPinId === id ? null : state.selectedPinId;
    return { pins: Object.freeze(pins), selectedPinId, dirty: true };
  });
}

function setPinComment(id, comment) {
  update(["pins", "dirty"], (state) => {
    const pins = state.pins.map((p) => (p.id === id ? Object.freeze({ ...p, comment }) : p));
    return { pins: Object.freeze(pins), dirty: true };
  });
}

function selectPin(id) {
  update(["selectedPinId"], () => ({ selectedPinId: id }));
}

function clearPins() {
  update(["pins", "selectedPinId", "dirty"], () => ({
    pins: Object.freeze([]),
    selectedPinId: null,
    dirty: true,
  }));
}

function setCallouts(list) {
  update(["callouts"], () => ({
    callouts: Object.freeze(list.map((c) => Object.freeze({ ...c }))),
  }));
}

function setDirty(v) {
  update(["dirty"], () => ({ dirty: !!v }));
}

function setShowUser(v) {
  update(["showUser"], () => ({ showUser: !!v }));
}

function setShowAgent(v) {
  update(["showAgent"], () => ({ showAgent: !!v }));
}

function setMeasure(aKey, bKey) {
  update(["measure"], () => ({ measure: Object.freeze({ a: aKey, b: bKey }) }));
}

export const store = Object.freeze({
  ...pageStore,
  setModels,
  setVisibility,
  setMode,
  setUpAxis,
  addPin,
  removePin,
  setPinComment,
  selectPin,
  clearPins,
  setCallouts,
  setDirty,
  setShowUser,
  setShowAgent,
  setMeasure,
});
