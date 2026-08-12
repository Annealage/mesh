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

export function initMeasure({ scene, markerRadius }) {
  const mA = document.getElementById("mA");
  const mB = document.getElementById("mB");
  const mResult = document.getElementById("mResult");
  const measureGroup = new THREE.Group();
  scene.add(measureGroup);
  let measureLine = null;
  let measureLabel = null;

  function round(v, d = 2) {
    const f = Math.pow(10, d);
    return Math.round(v * f) / f;
  }

  function measurables(state) {
    const list = [];
    state.pins.forEach((p) => list.push({ key: "u" + p.id, label: "U" + p.id + " · " + p.part, point: p.point }));
    state.callouts.forEach((a, i) => {
      const num = a.id != null ? a.id : i + 1;
      list.push({ key: "a" + num, label: "A" + num + " · " + (a.part || "unknown"), point: a.point || [0, 0, 0] });
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
      mResult.innerHTML = '<span class="empty">Pick two pins to measure.</span>';
      return;
    }
    const [x0, y0, z0] = a.point, [x1, y1, z1] = b.point;
    const dx = x1 - x0, dy = y1 - y0, dz = z1 - z0;
    const dist = Math.sqrt(dx * dx + dy * dy + dz * dz);
    mResult.innerHTML =
      "<div>&Delta;X " + round(dx) + " &middot; &Delta;Y " + round(dy) + " &middot; &Delta;Z " + round(dz) + " mm</div>" +
      '<div class="len">' + round(dist) + " mm</div>";

    const geo = new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(x0, y0, z0), new THREE.Vector3(x1, y1, z1)]);
    const mat = new THREE.LineDashedMaterial({ color: 0xffd24a, dashSize: 3, gapSize: 2, depthTest: false });
    measureLine = new THREE.Line(geo, mat);
    measureLine.computeLineDistances();
    measureGroup.add(measureLine);

    const rad = markerRadius();
    measureLabel = makeTagSprite(round(dist) + " mm", "#ffd24a");
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
      none.textContent = "-";
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
