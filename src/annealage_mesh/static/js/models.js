/**
 * Manifest fetch, the parts checkbox list and mesh loading.
 *
 * The mesh table this module fills (`meshes`, injected by main.js and
 * shared with three-scene.js and pins.js) is a side table, not store state:
 * store.js documents why a THREE.Mesh never becomes a state value. The two
 * things that DO live in the store, `state.models` and `state.visibility`,
 * are what the checkbox list and `mesh.visible` are both readers of;
 * `applyVisibility` below is the one place `mesh.visible` is ever assigned,
 * which is what makes a programmatic `store.setVisibility` move the mesh
 * exactly as it moves the checkbox.
 *
 * The checkbox list is reconciled against `partRows` (rel -> row elements)
 * rather than rebuilt from `innerHTML = ""` on every 'visibility' change: a
 * rebuild replaces the very checkbox whose own 'change' handler is what
 * triggered the notification, which drops it from the document before the
 * browser delivers the keyboard activation (Space) that follows a focus
 * click, so a keyboard user's second toggle lands on nothing.
 */

import * as THREE from "three";
import { STLLoader } from "./vendor/STLLoader.js";
import { showError, toast } from "agent/ui.js";
import { store } from "./store.js";
import { onPaletteChange, palette, partColor } from "./palette.js";

/**
 * @param scene     THREE.Scene loaded meshes are added to
 * @param fitView   () => void, called once after the first mesh loads and
 *                  again for every mesh loaded already-visible
 * @param meshes    {[rel]: THREE.Mesh}, the shared side table this module
 *                  is the only writer of
 */
export function initModels({ scene, fitView, meshes }) {
  const loader = new STLLoader();
  let loadedCount = 0;

  function urlFor(rel) {
    // Encoded per path segment, not as a whole, so a '#' or '?' in a
    // filename survives percent-encoding while the '/' separators between
    // segments stay separators.
    return "/model/" + rel.split("/").map(encodeURIComponent).join("/");
  }

  // The single site that ever assigns `mesh.visible`. Called from the load
  // callback once a mesh exists, and from the 'visibility' subscriber below
  // for every mesh already loaded; either caller is just handing this
  // function a rel and getting the current store value applied.
  function applyVisibility(rel) {
    const m = meshes[rel];
    if (m) m.visible = !!store.getState().visibility[rel];
  }

  // Assigned on first sight and kept, rather than taken from the model's index
  // in the manifest: the manifest is sorted, so a newly generated part landing
  // alphabetically first would otherwise shift every other part's colour, and
  // the colours are what the parts list, the callout dots and the pins all
  // identify a part by.
  const slotFor = new Map();
  let pal = palette();

  function slotOf(rel) {
    if (!slotFor.has(rel)) slotFor.set(rel, slotFor.size);
    return slotFor.get(rel);
  }

  function loadPart(model, { fit = false } = {}) {
    loader.load(
      urlFor(model.rel),
      (geo) => {
        geo.computeVertexNormals();
        const mat = new THREE.MeshStandardMaterial({
          color: partColor(pal, model.slot),
          metalness: 0.15,
          roughness: 0.6,
          // opaque: transparent + depthWrite-off mis-sorts internal faces
          // on concave parts
          transparent: false,
          opacity: 1.0,
          depthWrite: true,
          side: THREE.DoubleSide,
          flatShading: false,
        });
        const mesh = new THREE.Mesh(geo, mat);
        mesh.userData.rel = model.rel;
        mesh.userData.label = model.label;
        // A regenerated part replaces the mesh standing in for it. Dropping the
        // old one from the scene and releasing its GPU buffers is what stops a
        // modelling session that regenerates a part twenty times from leaving
        // twenty overlapping copies of it in the view and twenty buffers on the
        // card, neither of which three.js reclaims on its own.
        disposeMesh(model.rel);
        meshes[model.rel] = mesh;
        store.setTriangles(model.rel, geo.index ? geo.index.count / 3 : geo.attributes.position.count / 3);
        applyVisibility(model.rel);
        scene.add(mesh);
        loadedCount += 1;
        // Only ever on the first load. Reframing the camera because a part was
        // regenerated would move the view out from under someone who had just
        // lined it up on the detail they were asking about.
        if (fit && loadedCount === 1) fitView();
      },
      undefined,
      () => showError("Failed to load " + model.label + ", is it in the served directory?"),
    );
  }

  function disposeMesh(rel) {
    const previous = meshes[rel];
    if (!previous) return;
    scene.remove(previous);
    if (previous.geometry) previous.geometry.dispose();
    if (previous.material) previous.material.dispose();
    delete meshes[rel];
    store.setTriangles(rel, null);
  }

  store.subscribe("visibility", () => {
    for (const rel in meshes) {
      applyVisibility(rel);
    }
  });

  // --- Parts checkbox list: a reader of state.models/state.visibility,
  // never a writer of mesh.visible itself. Reconciled against `partRows`
  // rather than rebuilt, so the checkbox a 'change' handler just fired on
  // is the same node still in the document afterward. ---
  const partsDiv = document.getElementById("parts");
  const partRows = new Map(); // rel -> { lab, cb }

  function makePartRow(m) {
    const lab = document.createElement("label");
    lab.className = "check";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.addEventListener("change", () => store.setVisibility(m.rel, cb.checked));
    const sw = document.createElement("span");
    sw.className = "sw";
    sw.style.background = partColor(pal, m.slot);
    const txt = document.createElement("span");
    txt.className = "pname";
    txt.textContent = m.label;
    txt.title = m.rel;
    const tris = document.createElement("span");
    tris.className = "hint mono";
    lab.append(cb, sw, txt, tris);
    return { lab, cb, sw, tris, slot: m.slot };
  }

  function renderParts(state) {
    const listed = new Set();
    state.models.forEach((m) => {
      listed.add(m.rel);
      let row = partRows.get(m.rel);
      if (!row) {
        row = makePartRow(m);
        partRows.set(m.rel, row);
        partsDiv.appendChild(row.lab);
      }
      row.cb.checked = !!state.visibility[m.rel];
      const n = state.triangles[m.rel];
      row.tris.textContent = n == null ? "" : n.toLocaleString();
    });
    // A part deleted from the directory loses its row. Without this the list
    // would keep offering a checkbox for a file that no longer exists, and
    // toggling it would silently do nothing.
    for (const [rel, row] of partRows) {
      if (listed.has(rel)) continue;
      row.lab.remove();
      partRows.delete(rel);
    }
  }
  store.subscribe("models", renderParts);

  onPaletteChange((next) => {
    pal = next;
    for (const [rel, row] of partRows) {
      const colour = partColor(pal, row.slot);
      row.sw.style.background = colour;
      if (meshes[rel]) meshes[rel].material.color.set(colour);
    }
  });
  store.subscribe("visibility", renderParts);
  store.subscribe("triangles", renderParts);

  // --- The view's document tab: the served folder, the triangle total of the
  // parts on show, and when this page last saw a model rebuilt. ---
  const docName = document.getElementById("docName");
  const docTris = document.getElementById("docTris");
  const docRebuilt = document.getElementById("docRebuilt");
  const docRebuiltAt = document.getElementById("docRebuiltAt");
  const docParts = document.getElementById("docParts");
  const partCount = document.getElementById("partCount");
  const hhmm = new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit" });

  function renderDocTab(state) {
    let total = 0;
    for (const rel in state.triangles) if (state.visibility[rel]) total += state.triangles[rel];
    docTris.textContent = total.toLocaleString();
    const n = state.models.length;
    docParts.textContent = n + (n === 1 ? " part" : " parts");
    partCount.textContent = n || "";
    docRebuilt.hidden = state.rebuiltAt == null;
    if (state.rebuiltAt != null) docRebuiltAt.textContent = hhmm.format(state.rebuiltAt);
  }
  ["models", "triangles", "visibility", "rebuiltAt"].forEach((k) => store.subscribe(k, renderDocTab));

  function updateTitle(models, dir) {
    // The title bar shows exactly what's being reviewed: the file's absolute
    // path for a single STL, otherwise the served directory. Right-click
    // copies it.
    shownPath = (models.length === 1 ? models[0].path || models[0].file : dir || "") || "";
    docPath.textContent = shownPath;
    docPath.title = shownPath;
  }
  let shownPath = "";
  const docPath = document.getElementById("docPath");
  docPath.addEventListener("contextmenu", (e) => {
    if (!shownPath) return;
    e.preventDefault();
    navigator.clipboard
      .writeText(shownPath)
      .then(() => toast("Copied path to clipboard", true))
      .catch((err) => toast("Copy failed: " + err.message, false));
  });

  /**
   * Refetch the manifest and bring the scene into line with it.
   *
   * Called once on load and again whenever the server pushes `models_changed`,
   * which is what makes an agent regenerating a part visible without the human
   * reloading. `first` distinguishes the two only for the things that should
   * happen once: framing the camera, and complaining that the directory is
   * empty (a directory the human is about to generate a part into is not an
   * error, and saying so mid-session would be noise).
   *
   * Geometry is reloaded for every listed model rather than only the changed
   * ones, because the event deliberately does not say which changed. That is
   * cheap rather than wasteful: `/model` answers a conditional request, so a
   * part whose bytes are unchanged costs one 304 and no transfer.
   */
  async function loadManifest({ first = false } = {}) {
    try {
      const res = await fetch("/manifest", { cache: "no-store" });
      const j = await res.json();
      const models = j && Array.isArray(j.models) ? j.models : [];
      if (!models.length) {
        // Every part is gone, so the scene should be too, rather than left
        // showing models the directory no longer contains.
        Object.keys(meshes).forEach(disposeMesh);
        store.setModels([]);
        if (first) showError("No STLs found in the served directory.");
        return;
      }
      const withSlot = models.map((m) => ({ ...m, slot: slotOf(m.rel) }));
      const listed = new Set(models.map((m) => m.rel));
      Object.keys(meshes).forEach((rel) => {
        if (!listed.has(rel)) disposeMesh(rel);
      });
      store.setModels(withSlot);
      updateTitle(models, j.dir);
      if (j.dir) {
        docName.textContent = j.dir.split(/[\\/]/).filter(Boolean).pop() || j.dir;
        docName.title = j.dir;
      }
      withSlot.forEach((m) => loadPart(m, { fit: first }));
    } catch (e) {
      showError("Failed to load /manifest: " + e.message);
    }
  }

  loadManifest({ first: true });

  // Surfaces a hard failure so it is not a silent black screen. three.js is
  // vendored and served from /static, so a page that gets this far has a
  // working renderer and the remaining failure is every /model/ fetch
  // failing. Conditioned on the manifest having actually listed something,
  // so it never overwrites the distinct empty-manifest message above with a
  // claim that models were listed when none were.
  setTimeout(() => {
    if (store.getState().models.length > 0 && loadedCount === 0) {
      showError("No STLs loaded. The manifest listed models but none of them loaded from /model/.");
    }
  }, 4000);

  // Handed to ws.js by main.js, the same way pins.js hands over
  // refetchCallouts: as the `models_changed` handler in `onEvent`, and in
  // `onLive`, once on every hello, since a reconnect may have missed the push
  // that happened while the socket was down.
  // `modelsChanged` is the `models_changed` handler and is the only thing
  // that stamps `rebuiltAt`: a reconnect's refetch reloads the same files and
  // is not a rebuild.
  return {
    refetchModels: () => loadManifest(),
    modelsChanged: () => {
      store.setRebuiltAt(Date.now());
      return loadManifest();
    },
  };
}
