/**
 * User pins, agent callouts, picking, the annotate/navigate mode toggle,
 * the legend toggles, list rendering and submit.
 *
 * A pin's marker (a small sphere) and its number sprite are THREE objects
 * kept in `userSide`, a side table keyed by pin id, never in the store; the
 * 'pins' subscriber below is the only place they are created or disposed,
 * reconciling `userSide` against `state.pins` on every change so the scene
 * always holds exactly one marker pair per pin the store knows about,
 * regardless of whether the pin arrived from a pointer pick or (in a later
 * milestone) a pushed update.
 *
 * The pin *list* DOM is reconciled the same way, against `pinRows` (pin id
 * -> row element), instead of being rebuilt from `innerHTML = ""` on every
 * 'pins' notification. A full rebuild would tear the comment textarea out
 * of the document on the very next keystroke: `setPinComment` notifies
 * 'pins' on every input event, and a destroyed textarea can no longer be
 * the document's active element, so focus falls back to <body> and the
 * global keydown shortcuts (mode toggle, fit view) start intercepting the
 * text the reviewer is still typing. Reconciling means a surviving row's
 * textarea node is never replaced, so it can stay focused across an
 * arbitrary number of keystrokes.
 */

import * as THREE from "three";
import { store } from "./store.js";
import { disposeSprite, makeLabelSprite } from "./sprites.js";
import { buildMetaRows, toast } from "./ui.js";
import { isNarrow } from "./layout.js";

const USER_COLOR = 0xe86b34;
const USER_SEL = 0xffd24a;
const AGENT_COLOR = 0x35c7e0;
const TAP_SLOP = 8; // px; below this a pointerup counts as a tap, not an orbit drag

/**
 * @param scene, camera, controls, renderer  the three-scene.js context
 * @param markerRadius  () => number, scaled to the currently visible geometry
 * @param getMeshes     () => {[rel]: THREE.Mesh}, raycast targets
 */
export function initPins({ scene, camera, controls, renderer, markerRadius, getMeshes }) {
  const markerGroup = new THREE.Group(); // user pins (orange)
  scene.add(markerGroup);
  const agentGroup = new THREE.Group(); // agent callouts (cyan, read-only)
  scene.add(agentGroup);

  const userSide = new Map(); // pin id -> { marker, sprite }
  const agentSide = []; // rendered agent markers, disposed and rebuilt on each 'callouts' change

  const raycaster = new THREE.Raycaster();
  const pointer = new THREE.Vector2();

  function round(v, d = 2) {
    const f = Math.pow(10, d);
    return Math.round(v * f) / f;
  }

  // Classify the dominant face by which axis the normal points along.
  function classify(n) {
    const ax = Math.abs(n.x), ay = Math.abs(n.y), az = Math.abs(n.z);
    if (ax >= ay && ax >= az) return n.x >= 0 ? "+X" : "-X";
    if (ay >= ax && ay >= az) return n.y >= 0 ? "+Y" : "-Y";
    return n.z >= 0 ? "+Z" : "-Z";
  }

  // ---- mode: gates picking so a touch-swipe to orbit doesn't drop a stray pin ----
  const modeBtn = document.getElementById("modeBtn");
  function applyMode(mode) {
    const on = mode === "annotate";
    modeBtn.classList.toggle("on", on);
    modeBtn.textContent = on ? "● Add pin" : "○ Navigate";
    renderer.domElement.style.cursor = on ? "crosshair" : "grab";
  }
  applyMode(store.getState().mode);
  store.subscribe("mode", (state) => applyMode(state.mode));
  function toggleMode() {
    store.setMode(store.getState().mode === "annotate" ? "nav" : "annotate");
  }
  modeBtn.addEventListener("click", toggleMode);
  addEventListener("keydown", (e) => {
    if (e.target && (e.target.tagName === "TEXTAREA" || e.target.tagName === "INPUT")) return;
    if (e.key === "a" || e.key === "A") toggleMode();
  });

  // ---- picking: distinguishes a tap from an orbit drag; only fires in annotate mode ----
  let downPos = null;
  renderer.domElement.addEventListener("pointerdown", (e) => {
    downPos = { x: e.clientX, y: e.clientY, btn: e.button };
  });
  renderer.domElement.addEventListener("pointerup", (e) => {
    if (!downPos) return;
    const moved = Math.hypot(e.clientX - downPos.x, e.clientY - downPos.y);
    const wasPrimary = downPos.btn === 0;
    downPos = null;
    if (store.getState().mode !== "annotate") return; // navigate mode: never pin
    if (!wasPrimary || moved > TAP_SLOP) return; // a drag = orbit, not a pin
    pick(e);
  });

  function pick(e) {
    const rect = renderer.domElement.getBoundingClientRect();
    pointer.x = ((e.clientX - rect.left) / rect.width) * 2 - 1;
    pointer.y = -((e.clientY - rect.top) / rect.height) * 2 + 1;
    raycaster.setFromCamera(pointer, camera);
    const targets = Object.values(getMeshes()).filter((m) => m.visible);
    const hits = raycaster.intersectObjects(targets, false);
    if (!hits.length) return;
    const h = hits[0];

    // World-space normal (meshes are un-transformed so this equals model
    // space, but normalise via the matrix to be robust if that ever
    // changes).
    let n = new THREE.Vector3(0, 0, 1);
    if (h.face) {
      n = h.face.normal.clone().transformDirection(h.object.matrixWorld).normalize();
    }
    const faceLabel = classify(n);
    const id = store.addPin({
      part: h.object.userData.label || "unknown",
      rel: h.object.userData.rel || "",
      point: [round(h.point.x), round(h.point.y), round(h.point.z)],
      normal: [round(n.x, 3), round(n.y, 3), round(n.z, 3)],
      faceIndex: h.faceIndex != null ? h.faceIndex : -1,
      label: faceLabel,
    });
    const panelVisible = isNarrow() ? store.getState().activeTab === "review" : store.getState().panelOpen;
    if (!panelVisible) {
      toast("Pin #" + id + " added, open Panel to comment", true);
    }
  }

  // ---- reconcile user-pin markers against state.pins ----
  store.subscribe("pins", (state) => {
    const seen = new Set();
    state.pins.forEach((p) => {
      seen.add(p.id);
      if (userSide.has(p.id)) return;
      const rad = markerRadius();
      const marker = new THREE.Mesh(
        new THREE.SphereGeometry(rad, 16, 12),
        new THREE.MeshBasicMaterial({ color: USER_COLOR, depthTest: true }),
      );
      marker.position.set(p.point[0], p.point[1], p.point[2]);
      markerGroup.add(marker);
      const sprite = makeLabelSprite(p.id, "#e86b34");
      sprite.position.set(p.point[0], p.point[1], p.point[2] + rad * 2.2);
      sprite.scale.set(rad * 3, rad * 3, 1);
      markerGroup.add(sprite);
      userSide.set(p.id, { marker, sprite });
    });
    for (const [id, obj] of userSide) {
      if (seen.has(id)) continue;
      markerGroup.remove(obj.marker, obj.sprite);
      obj.marker.geometry.dispose();
      obj.marker.material.dispose();
      disposeSprite(obj.sprite);
      userSide.delete(id);
    }
    renderPinList(state);
  });

  store.subscribe("selectedPinId", (state) => {
    userSide.forEach((obj, id) => obj.marker.material.color.set(id === state.selectedPinId ? USER_SEL : USER_COLOR));
    const p = state.pins.find((x) => x.id === state.selectedPinId);
    if (p) {
      controls.target.set(p.point[0], p.point[1], p.point[2]);
      controls.update();
    }
    document.querySelectorAll("#pins .pin").forEach((el) => {
      el.classList.toggle("sel", Number(el.dataset.id) === state.selectedPinId);
    });
  });

  const pinsDiv = document.getElementById("pins");
  const emptyMsg = document.createElement("div");
  emptyMsg.className = "empty";
  emptyMsg.textContent = "No pins yet, tap “Add pin”, then tap a visible part.";

  const pinRows = new Map(); // pin id -> { el, num, meta, ta }, one row per pin, never rebuilt

  function makePinRow(p) {
    const el = document.createElement("div");
    el.className = "pin";
    el.dataset.id = p.id;

    const top = document.createElement("div");
    top.className = "top";
    const num = document.createElement("div");
    num.className = "num";
    const meta = document.createElement("div");
    meta.className = "meta";
    const del = document.createElement("button");
    del.className = "del";
    del.title = "delete";
    del.textContent = "×";
    del.addEventListener("click", (ev) => {
      ev.stopPropagation();
      store.removePin(p.id);
    });
    top.append(num, meta, del);
    top.addEventListener("click", () => store.selectPin(p.id));

    const ta = document.createElement("textarea");
    ta.addEventListener("input", () => store.setPinComment(p.id, ta.value));
    ta.addEventListener("focus", () => store.selectPin(p.id));

    el.append(top, ta);
    return { el, num, meta, ta };
  }

  // Updates everything about a row except the textarea's value while it is
  // focused: the reviewer typing into it is the store's source of truth for
  // that keystroke, not the state this render pass is reacting to, and
  // overwriting `.value` out from under an active edit is the whole defect
  // reconciliation exists to avoid.
  function updatePinRow(row, p) {
    row.num.textContent = p.id;
    row.meta.replaceChildren(
      ...buildMetaRows(p.part, p.label, p.point, (v) => v).children);
    row.ta.placeholder = "Comment for pin #" + p.id + "…";
    if (row.ta !== document.activeElement && row.ta.value !== p.comment) {
      row.ta.value = p.comment;
    }
  }

  function renderPinList(state) {
    const seen = new Set();
    state.pins.forEach((p) => {
      seen.add(p.id);
      let row = pinRows.get(p.id);
      if (!row) {
        row = makePinRow(p);
        pinRows.set(p.id, row);
        // Appended only on creation, never on every render: pin ids are
        // assigned in increasing order and a pin is only ever removed, never
        // reordered, so appending a new row at the end always matches
        // state.pins order without re-inserting a row already in place.
        // Re-appending an already-attached node still detaches and
        // reattaches it per the DOM spec, which blurs a focused descendant,
        // so doing that on every notification is what would tear focus out
        // of the very textarea whose own `input` event caused the
        // notification.
        pinsDiv.appendChild(row.el);
      }
      updatePinRow(row, p);
    });
    for (const [id, row] of pinRows) {
      if (seen.has(id)) continue;
      row.el.remove();
      pinRows.delete(id);
    }
    if (state.pins.length) {
      emptyMsg.remove();
    } else {
      pinsDiv.appendChild(emptyMsg);
    }
    updateStatus(state);
  }

  const statusEl = document.getElementById("status");
  function updateStatus(state) {
    statusEl.textContent =
      state.pins.length + " pin" + (state.pins.length === 1 ? "" : "s") +
      (state.dirty ? " · unsaved" : " · submitted");
  }
  store.subscribe("dirty", updateStatus);

  document.getElementById("clear").addEventListener("click", () => {
    const count = store.getState().pins.length;
    if (count && !confirm("Delete all " + count + " pins?")) return;
    store.clearPins();
  });

  document.getElementById("submit").addEventListener("click", async () => {
    const payload = store.getState().pins.map((p) => ({
      id: p.id,
      part: p.part,
      label: p.label,
      point: p.point,
      normal: p.normal,
      faceIndex: p.faceIndex,
      comment: p.comment,
    }));
    try {
      const res = await fetch("/submit", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const j = await res.json().catch(() => ({}));
      if (res.ok && j.ok) {
        store.setDirty(false);
        toast("Submitted " + j.count + " annotation" + (j.count === 1 ? "" : "s"), true);
      } else {
        toast("Submit failed: " + (j.error || res.status), false);
      }
    } catch (err) {
      toast("Submit failed: " + err.message, false);
    }
  });

  // ---- agent callouts: rendered from state.callouts, reaching it only via refetchCallouts/setCallouts below ----
  const agentPinsDiv = document.getElementById("agentPins");
  let agentSig = null;
  let calloutsGen = 0; // bumped on every refetchCallouts call; guards against an in-flight fetch applying its reply after a later call has already started

  function clearAgentMarkers() {
    agentSide.forEach((m) => {
      agentGroup.remove(m.marker, m.sprite);
      m.marker.geometry.dispose();
      m.marker.material.dispose();
      disposeSprite(m.sprite);
    });
    agentSide.length = 0;
  }

  store.subscribe("callouts", (state) => {
    clearAgentMarkers();
    const rad = markerRadius();
    state.callouts.forEach((a, i) => {
      const p = a.point || [0, 0, 0];
      const num = a.id != null ? a.id : i + 1;
      const marker = new THREE.Mesh(
        new THREE.SphereGeometry(rad, 16, 12),
        new THREE.MeshBasicMaterial({ color: AGENT_COLOR, depthTest: true }),
      );
      marker.position.set(p[0], p[1], p[2]);
      agentGroup.add(marker);
      const sprite = makeLabelSprite(num, "#35c7e0");
      sprite.position.set(p[0], p[1], p[2] + rad * 2.2);
      sprite.scale.set(rad * 3, rad * 3, 1);
      agentGroup.add(sprite);
      agentSide.push({ marker, sprite, point: p });
    });

    agentPinsDiv.innerHTML = "";
    if (!state.callouts.length) return;
    const hdr = document.createElement("div");
    hdr.className = "sec-hdr";
    hdr.textContent = "Agent callouts (" + state.callouts.length + ")";
    agentPinsDiv.appendChild(hdr);
    state.callouts.forEach((a, i) => {
      const num = a.id != null ? a.id : i + 1;
      const p = a.point || [0, 0, 0];
      const el = document.createElement("div");
      el.className = "apin";
      const top = document.createElement("div");
      top.className = "top";
      const n = document.createElement("div");
      n.className = "num";
      n.textContent = num;
      const meta = buildMetaRows(a.part || "unknown", a.label || "", p, round);
      top.append(n, meta);
      const cmt = document.createElement("div");
      cmt.className = "cmt";
      cmt.textContent = a.comment || "(no comment)";
      el.append(top, cmt);
      el.addEventListener("click", () => {
        controls.target.set(p[0], p[1], p[2]);
        controls.update();
      });
      agentPinsDiv.appendChild(el);
    });
  });

  // ws.js pushes a callouts_changed event over the socket when it is live
  // and calls refetchCallouts directly; startCalloutsPoll/stopCalloutsPoll
  // are ws.js's fallback for when the socket is not live. Whichever one is
  // driving is ws.js's decision, not this module's: pollTimer is only ever
  // non-null while ws.js has decided the socket is down, so the poll and
  // the push are never both running, which keeps setCallouts a single
  // logical writer even though both paths call it.
  let pollTimer = null;

  async function refetchCallouts() {
    const gen = ++calloutsGen;
    try {
      const res = await fetch("/callouts", { cache: "no-store" });
      if (!res.ok) return;
      const j = await res.json();
      if (gen !== calloutsGen) return; // a later refetchCallouts call has since started; this reply is stale
      const list = Array.isArray(j) ? j : j && Array.isArray(j.annotations) ? j.annotations : [];
      const sig = JSON.stringify(list);
      if (sig !== agentSig) {
        agentSig = sig;
        store.setCallouts(list);
      }
    } catch (e) {
      // Only the fetch and the JSON parse are expected to fail transiently. An
      // error raised by a store subscriber reaches here too, because
      // setCallouts notifies synchronously inside this try, and swallowing that
      // silently is how a real defect looks exactly like a dropped request.
      if (!(e instanceof TypeError || e instanceof SyntaxError)) throw e;
      // transient network hiccup; the next poll tick, or the next
      // callouts_changed event once the socket recovers, tries again
    }
  }

  function startCalloutsPoll() {
    if (pollTimer !== null) return; // already running; a second interval would double every refetch
    refetchCallouts();
    pollTimer = setInterval(refetchCallouts, 1500);
  }

  function stopCalloutsPoll() {
    if (pollTimer === null) return;
    clearInterval(pollTimer);
    pollTimer = null;
  }

  // ---- legend toggles ----
  const tgAgent = document.getElementById("tgAgent");
  const tgUser = document.getElementById("tgUser");
  tgAgent.addEventListener("change", () => store.setShowAgent(tgAgent.checked));
  tgUser.addEventListener("change", () => store.setShowUser(tgUser.checked));
  store.subscribe("showAgent", (state) => {
    agentGroup.visible = state.showAgent;
    agentPinsDiv.style.display = state.showAgent ? "" : "none";
  });
  store.subscribe("showUser", (state) => {
    markerGroup.visible = state.showUser;
  });

  // Unconditional, so first paint of the callout list never depends on the
  // socket reaching hello first: ws.js's own refetchCallouts calls (on hello
  // and on callouts_changed) cover every update after this one, but without
  // this call the panel would show nothing until that first hello arrives,
  // even though /callouts has been servable since the page loaded.
  refetchCallouts();

  // Once at startup, for the same reason as the call above: the pin list is
  // otherwise only rendered when `pins` changes, so on a freshly loaded page,
  // which has no pins and therefore no pending change, nothing would say so.
  // This module owns that message, including the first time it is shown.
  renderPinList(store.getState());

  // Handed to ws.js by main.js, which is the only place these three and
  // ws.js's connect logic are both in scope. refetchCallouts is exposed on
  // its own, separately from the poll, because ws.js also calls it once,
  // outside the poll, on every hello and on every callouts_changed event.
  return { startCalloutsPoll, stopCalloutsPoll, refetchCallouts };
}
