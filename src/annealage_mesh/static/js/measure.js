/**
 * Straight-line distance between any two placed pins, user or agent.
 *
 * The two dropdown selections are store state (`state.measure`), not local
 * DOM state: a `change` event calls `setMeasure`, and the 'measure'
 * subscriber is what actually redraws the dashed line and the pill label,
 * so a pin deleted out from under a current selection (handled by the
 * 'pins'/'callouts' subscriber re-validating the selection) and a
 * programmatic `setMeasure` both go through the same path as a user
 * picking from the dropdown.
 */

import * as THREE from "three";
import { store } from "./store.js";
import { disposeSprite, makeTagSprite } from "./sprites.js";
import { onPaletteChange, palette } from "./palette.js";
import { mm } from "./format.js";

export function initMeasure({ scene, markerRadius }) {
  const mA = document.getElementById("mA");
  const mB = document.getElementById("mB");
  const mResult = document.getElementById("mResult");
  const mEmpty = document.getElementById("mEmpty");
  const hud = document.getElementById("measureHud");
  const measureGroup = new THREE.Group();
  scene.add(measureGroup);
  let measureLine = null;
  let measureLabel = null;
  let pal = palette();

  function measurables(state) {
    const list = [];
    state.pins.forEach((p) => list.push({ key: "u" + p.id, noun: "pin " + p.id, label: p.id + " " + p.part, point: p.point }));
    state.callouts.forEach((a, i) => {
      const num = a.id != null ? a.id : i + 1;
      list.push({ key: "a" + num, noun: "callout A" + num, label: "A" + num + " " + (a.part || "unknown"), point: a.point || [0, 0, 0] });
    });
    return list;
  }

  function clearLine() {
    if (measureLine) {
      measureGroup.remove(measureLine);
      measureLine.geometry.dispose();
      measureLine.material.dispose();
      measureLine = null;
    }
    if (measureLabel) {
      measureGroup.remove(measureLabel);
      disposeSprite(measureLabel);
      measureLabel = null;
    }
  }

  function updateMeasure(items, aKey, bKey) {
    const a = items.find((it) => it.key === aKey);
    const b = items.find((it) => it.key === bKey);
    clearLine();
    if (!a || !b || a.key === b.key) {
      hud.textContent = "";
      mResult.replaceChildren();
      mResult.hidden = true;
      mEmpty.hidden = false;
      return;
    }
    const [x0, y0, z0] = a.point, [x1, y1, z1] = b.point;
    const dx = x1 - x0, dy = y1 - y0, dz = z1 - z0;
    const dist = Math.sqrt(dx * dx + dy * dy + dz * dz);
    const text = mm(dist) + " mm";
    // Every string here is built with textContent: a part name is untrusted.
    hud.textContent = a.noun.charAt(0).toUpperCase() + a.noun.slice(1) + " to " + b.noun + ": " + text;
    mResult.replaceChildren(
      ...[["\u0394X", dx], ["\u0394Y", dy], ["\u0394Z", dz], ["Distance", dist]].map(([name, v]) => {
        const row = document.createElement("div");
        const dt = document.createElement("dt");
        dt.textContent = name;
        const dd = document.createElement("dd");
        dd.textContent = mm(v) + " mm";
        row.append(dt, dd);
        return row;
      }),
    );
    mResult.hidden = false;
    mEmpty.hidden = true;

    const geo = new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(x0, y0, z0), new THREE.Vector3(x1, y1, z1)]);
    const mat = new THREE.LineDashedMaterial({ color: pal.measure, dashSize: 3, gapSize: 2, depthTest: false });
    measureLine = new THREE.Line(geo, mat);
    measureLine.computeLineDistances();
    measureGroup.add(measureLine);

    const rad = markerRadius();
    measureLabel = makeTagSprite(text, pal.measure, pal.well);
    measureLabel.position.set((x0 + x1) / 2, (y0 + y1) / 2, (z0 + z1) / 2 + rad * 1.5);
    measureLabel.scale.set(rad * 3 * measureLabel.userData.aspect, rad * 3, 1);
    measureGroup.add(measureLabel);
  }

  function refreshOptions(state) {
    const items = measurables(state);
    const validA = items.some((it) => it.key === state.measure.a) ? state.measure.a : "";
    const validB = items.some((it) => it.key === state.measure.b) ? state.measure.b : "";
    [
      [mA, validA],
      [mB, validB],
    ].forEach(([sel, val]) => {
      // Option text is built as a text node for the same reason the pin and
      // callout rows are: an item's label carries a model name from an STL
      // filename or a callout's own part field, neither of which this viewer
      // controls.
      sel.replaceChildren();
      const none = document.createElement("option");
      none.value = "";
      none.textContent = "None";
      sel.append(none);
      items.forEach((it) => {
        const opt = document.createElement("option");
        opt.value = it.key;
        opt.textContent = it.label;
        sel.append(opt);
      });
      sel.value = val;
    });
    updateMeasure(items, validA, validB);
    // A pin that vanished (deleted, or an agent callout that aged out) out
    // from under a current selection must clear that selection in state
    // too, not just in the dropdown, or state.measure would name a pin the
    // rest of the app no longer has. This runs from inside the
    // 'pins'/'callouts' notify below; store.js queues it and drains it once
    // that notify pass finishes.
    if (validA !== state.measure.a || validB !== state.measure.b) {
      store.setMeasure(validA, validB);
    }
  }

  onPaletteChange((next) => {
    pal = next;
    const state = store.getState();
    updateMeasure(measurables(state), state.measure.a, state.measure.b);
  });

  mA.addEventListener("change", () => store.setMeasure(mA.value, mB.value));
  mB.addEventListener("change", () => store.setMeasure(mA.value, mB.value));

  store.subscribe("pins", refreshOptions);
  store.subscribe("callouts", refreshOptions);
  store.subscribe("measure", (state) => {
    const items = measurables(state);
    mA.value = state.measure.a;
    mB.value = state.measure.b;
    updateMeasure(items, state.measure.a, state.measure.b);
  });
}
