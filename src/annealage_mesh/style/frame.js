// The page frame's driver: layout focus, the Panel button, the phone tab bar and
// the column resizers. It acts on the markup theme.css draws (.wb and its
// children) and has no dependencies.
//
// Load it as a classic script after the page's markup, with data-auto to start
// it by itself once the page has parsed:
//   <script src="/style/frame.js" data-auto></script>
// or without data-auto and call it by hand once the DOM exists:
//   annealageFrame({ tabs: [{ id: "drawing", label: "Drawing", target: ".view" }, ...] })
//
// Markup hooks (all optional; a missing one is skipped, a page with no .wb is
// left alone):
//   .wb[data-layout="drawing|split|agent"]   layout focus
//   [data-layout-set="drawing|split|agent"]  buttons that choose it (aria-pressed); one with
//                                            aria-disabled="true" does nothing
//   .wb[data-insp="left"]                    the inspector is the left column (no rail): the grid
//                                            is inspector, view, agent, and its handle sits on the
//                                            inspector's right edge
//   #panelBtn                                toggles body.panel-open (aria-pressed)
//   [data-panel-close]                       closes the panel
//   #tabbar                                  the phone tab bar, role="tablist"
//     button[data-tab="id"][data-target="selector"]   one tab per pane (aria-selected)
//   .split.even > .half + .half              a view split in two halves with a handle between them
//
// Settings, kept in localStorage per origin and per product (<html data-product>,
// or "platform"), and written only when the user changes something:
//   annealage.frame.<product>         {layout, insp, chat, split}  layout, column widths and the
//                                     split (split only on a page that has one)
//   annealage.frame.<product>.panel   "open" | "closed"     the Panel button
//
// annealageFrame(options) returns (and keeps as annealageFrame.current):
//   layout() / setLayout(v)       "drawing" | "split" | "agent"
//   widths() / setWidths({insp, chat})   pixels or null (the theme's default); clamped
//   split() / setSplit(f)         the first half's share of the split view, 0.2 to 0.8, or null
//                                 (the theme's default, half and half); null on a page with no split
//   panelOpen() / setPanelOpen(b)
//   isNarrow()                    900px and below, where the tab bar governs the panes
//   activeTab() / activateTab(id) the tab bar's current pane (kept at any width,
//                                 shown only at 900px and below)
//   reset()                       forget the stored settings and go back to the markup's
//   destroy()
// A "framechange" event fires on .wb, detail {layout, insp, chat, panelOpen,
// activeTab} (and split on a page that has one), after each change, so a canvas
// or WebGL view can resize.
//
// Options: root (default the first .wb), storage (default localStorage), product,
// tabs ([{id, label, target}]: the tab bar is rebuilt from it; without it the
// tabs already in #tabbar are used).
//
// The widths are written as --insp-w-user and --chat-w-user on .wb, and the split
// as --split-user (a percentage), which theme.css reads only while that column is
// open. With nothing stored no property is written, so a product's own --insp-w,
// --chat-w and --split stay the defaults. At 900px and below nothing is applied.
(function () {
  "use strict";
  var NARROW = "(max-width: 900px)";
  var LAYOUTS = ["drawing", "split", "agent"];
  var MIN_VIEW = 320; // the view keeps at least this much while a column is dragged
  var STEP = 16;
  // The resizable columns. index is the column's place in .wb's grid-template-columns
  // (rail, view, then these), pane the element that fills it. leftIndex is the place
  // when the inspector is on the left (data-insp="left": inspector, view, agent).
  var COLUMNS = [
    { key: "insp", pane: ".inspector", prop: "--insp-w-user", index: 2, leftIndex: 0, min: 240, max: 560, def: 300, label: "Resize the inspector", leftLabel: "Resize the left pane" },
    { key: "chat", pane: "#chat, .console", prop: "--chat-w-user", index: 3, leftIndex: 2, min: 320, max: 720, def: 400, label: "Resize the agent pane", leftLabel: "Resize the agent pane" },
  ];
  // The split between two halves of the view: a share of the host's width, not pixels,
  // so it survives a resize. Each half keeps at least minPx.
  var SPLIT = { prop: "--split-user", min: 0.2, max: 0.8, def: 0.5, minPx: 240, label: "Resize the editor and the screen" };
  var auto = !!(document.currentScript && document.currentScript.hasAttribute("data-auto"));

  function defaultStorage() {
    try { return window.localStorage; } catch (e) { return null; }
  }
  function read(storage, key) {
    try { return storage ? storage.getItem(key) : null; } catch (e) { return null; }
  }
  function write(storage, key, value) {
    try { if (storage) storage.setItem(key, value); } catch (e) { /* private mode, quota */ }
  }
  function drop(storage, key) {
    try { if (storage) storage.removeItem(key); } catch (e) { /* private mode */ }
  }
  function between(v, lo, hi) { return Math.min(hi, Math.max(lo, v)); }
  function clampCol(c, v) { return Math.round(between(v, c.min, c.max)); }
  function clampShare(v) { return Math.round(between(v, SPLIT.min, SPLIT.max) * 10000) / 10000; }

  function inert() {
    var noop = function () {};
    return {
      layout: function () { return "split"; }, setLayout: noop,
      widths: function () { return { insp: null, chat: null }; }, setWidths: noop,
      split: function () { return null; }, setSplit: noop,
      panelOpen: function () { return false; }, setPanelOpen: noop,
      isNarrow: function () { return false; },
      activeTab: function () { return null; }, activateTab: noop,
      reset: noop, destroy: noop,
    };
  }

  function initFrame(opts) {
    opts = opts || {};
    var doc = document;
    var win = window;
    var root = opts.root !== undefined ? opts.root : doc.querySelector(".wb");
    if (!root) return (initFrame.current = inert());
    if (initFrame.current && initFrame.current.root === root) initFrame.current.destroy();

    var storage = opts.storage !== undefined ? opts.storage : defaultStorage();
    var product = opts.product || (doc.documentElement.getAttribute("data-product")) || "platform";
    var KEY = "annealage.frame." + product;
    var PANEL_KEY = KEY + ".panel";
    var mq = win.matchMedia ? win.matchMedia(NARROW) : { matches: false, addEventListener: function () {}, removeEventListener: function () {} };
    var homeLayout = root.getAttribute("data-layout");
    var left = root.getAttribute("data-insp") === "left"; // the inspector is the first column, there is no rail
    var pref = {}; // column key -> stored width, or null for the theme's default; split -> stored share
    var cols = COLUMNS.filter(function (c) { return root.querySelector(c.pane); });
    COLUMNS.forEach(function (c) { pref[c.key] = null; });
    pref.split = null;
    var host = root.querySelector(".split.even");
    var halves = host ? host.querySelectorAll(".half") : [];
    var hasSplit = halves.length >= 2;
    var grips = [];
    var splitGrip = null;
    var drag = null;
    var pending = null;
    var gone = false;

    var tabbar = root.querySelector("#tabbar");
    var panelBtn = root.querySelector("#panelBtn");
    var tabs = [];
    var active = null;

    // The column's place in the grid now.
    function at(c) { return left ? c.leftIndex : c.index; }
    // The handle grows its pane rightwards (+1) for the left inspector, leftwards (-1) otherwise.
    function dirOf(c) { return left && c.key === "insp" ? 1 : -1; }

    // ---- State

    function layout() { return root.getAttribute("data-layout") || "split"; }
    function panelOpen() { return !!(doc.body && doc.body.classList.contains("panel-open")); }
    function isNarrow() { return mq.matches; }
    function widths() { return { insp: pref.insp, chat: pref.chat }; }

    function snapshot() {
      var s = { layout: layout(), insp: pref.insp, chat: pref.chat, panelOpen: panelOpen(), activeTab: active };
      if (hasSplit) s.split = pref.split;
      return s;
    }
    function emit() {
      root.dispatchEvent(new win.CustomEvent("framechange", { bubbles: true, detail: snapshot() }));
    }
    function persist() {
      var rec = { layout: layout() };
      COLUMNS.forEach(function (c) { if (pref[c.key] != null) rec[c.key] = pref[c.key]; });
      if (pref.split != null) rec.split = pref.split;
      write(storage, KEY, JSON.stringify(rec));
    }

    // ---- Layout focus

    function syncLayoutButtons() {
      var now = layout();
      root.querySelectorAll("[data-layout-set]").forEach(function (b) {
        b.setAttribute("aria-pressed", String(b.getAttribute("data-layout-set") === now));
      });
    }
    function setLayout(v) {
      if (LAYOUTS.indexOf(v) < 0) return;
      root.setAttribute("data-layout", v);
      syncLayoutButtons();
      persist();
      fit();
      emit();
    }

    // ---- Panel

    function applyPanel(open) {
      doc.body.classList.toggle("panel-open", open);
      if (panelBtn) panelBtn.setAttribute("aria-pressed", String(open));
    }
    function setPanelOpen(open) {
      open = !!open;
      applyPanel(open);
      write(storage, PANEL_KEY, open ? "open" : "closed");
      fit();
      emit();
    }

    // ---- Phone tabs

    function buildTabs() {
      if (!tabbar) return;
      if (opts.tabs) {
        tabs = opts.tabs.map(function (t) { return { id: t.id, label: t.label, target: t.target }; });
        while (tabbar.firstChild) tabbar.removeChild(tabbar.firstChild);
        tabs.forEach(function (t) {
          var b = doc.createElement("button");
          b.setAttribute("type", "button");
          b.setAttribute("data-tab", t.id);
          if (t.target) b.setAttribute("data-target", t.target);
          b.textContent = t.label;
          tabbar.appendChild(b);
        });
      } else {
        tabs = [];
        tabbar.querySelectorAll("[data-tab]").forEach(function (b) {
          tabs.push({ id: b.getAttribute("data-tab"), label: b.textContent, target: b.getAttribute("data-target") });
        });
      }
      tabbar.setAttribute("role", "tablist");
      tabbar.querySelectorAll("[data-tab]").forEach(function (b) { b.setAttribute("role", "tab"); });
      active = tabs.length ? tabs[0].id : null;
    }
    function applyTabs() {
      if (!tabbar) return;
      tabbar.querySelectorAll("[data-tab]").forEach(function (b) {
        b.setAttribute("aria-selected", String(b.getAttribute("data-tab") === active));
      });
      var narrow = isNarrow();
      tabs.forEach(function (t) {
        var el = t.target ? doc.querySelector(t.target) : null;
        if (el) el.style.display = narrow && t.id !== active ? "none" : "";
      });
    }
    function activateTab(id) {
      if (!tabs.some(function (t) { return t.id === id; })) return;
      active = id;
      applyTabs();
      emit();
    }

    // ---- Columns

    // The grid's resolved column widths in px ([rail, view, insp, chat], or
    // [insp, view, chat] with the inspector on the left), or null where there is
    // no layout to read.
    function measure() {
      var cs = win.getComputedStyle ? win.getComputedStyle(root) : null;
      var t = cs && cs.gridTemplateColumns;
      if (!t) return null;
      var p = t.split(/\s+/).map(parseFloat);
      return p.length === (left ? 3 : 4) && p.every(isFinite) ? p : null;
    }
    function clearUser() {
      cols.forEach(function (c) { root.style.removeProperty(c.prop); });
      root.style.removeProperty(SPLIT.prop);
    }

    // Apply the stored widths to the columns that are open now, narrowing them
    // (never below the default, or the limit) so the view keeps MIN_VIEW. A
    // column with nothing stored is left to the theme.
    function fit() {
      clearUser();
      if (isNarrow() || layout() === "agent") { syncGrips(null); syncSplit(); return; }
      var px = measure();
      var want = {};
      cols.forEach(function (c) {
        if (pref[c.key] == null) return;
        if (px && !(px[at(c)] > 0)) return; // closed: the theme holds it at 0
        want[c.key] = clampCol(c, pref[c.key]);
      });
      if (px) {
        var total = px.reduce(function (a, b) { return a + b; }, 0) - (left ? 0 : px[0]); // the rail is not shared
        var excess = MIN_VIEW - total;
        COLUMNS.forEach(function (c) { excess += want[c.key] != null ? want[c.key] : px[at(c)]; });
        cols.slice().reverse().forEach(function (c) {
          if (want[c.key] == null || excess <= 0) return;
          var floor = Math.max(c.min, Math.min(want[c.key], px[at(c)]));
          var cut = Math.min(excess, want[c.key] - floor);
          if (cut > 0) { want[c.key] -= cut; excess -= cut; }
        });
      }
      cols.forEach(function (c) {
        if (want[c.key] != null) root.style.setProperty(c.prop, Math.round(want[c.key]) + "px");
      });
      syncGrips(px && measure());
      fitSplit();
    }
    function syncGrips(px) {
      grips.forEach(function (g) {
        var c = g.col;
        var now = px ? px[at(c)] : pref[c.key] != null ? pref[c.key] : c.def;
        g.el.setAttribute("aria-valuemin", String(c.min));
        g.el.setAttribute("aria-valuemax", String(c.max));
        g.el.setAttribute("aria-valuenow", String(Math.round(now)));
      });
    }
    function setWidths(w) {
      w = w || {};
      COLUMNS.forEach(function (c) {
        if (!(c.key in w)) return;
        var v = w[c.key];
        pref[c.key] = typeof v === "number" && isFinite(v) ? clampCol(c, v) : null;
      });
      persist();
      fit();
      emit();
    }

    // The most a column may take now: its own limit, and the view keeps MIN_VIEW.
    function limitOf(c, px) {
      return px ? Math.max(c.min, Math.min(c.max, px[1] + px[at(c)] - MIN_VIEW)) : c.max;
    }
    function widthNow(c, px) {
      return px && px[at(c)] > 0 ? px[at(c)] : pref[c.key] != null ? pref[c.key] : c.def;
    }

    // ---- The split

    function hostWidth() {
      var r = host && host.getBoundingClientRect ? host.getBoundingClientRect() : null;
      return r && r.width > 0 ? r.width : 0;
    }
    // The share the first half may take now: SPLIT's own limits, and each half keeps minPx.
    function shareBounds() {
      var lo = SPLIT.min;
      var hi = SPLIT.max;
      var w = hostWidth();
      if (w > 0) {
        lo = Math.max(lo, SPLIT.minPx / w);
        hi = Math.min(hi, 1 - SPLIT.minPx / w);
        if (lo > hi) lo = hi = 0.5;
      }
      return [lo, hi];
    }
    // The first half's share as laid out now, else what is stored, else the default.
    function shareNow() {
      var w = hostWidth();
      var r = w > 0 && halves[0].getBoundingClientRect ? halves[0].getBoundingClientRect() : null;
      if (r && r.width > 0) return r.width / w;
      return pref.split != null ? pref.split : SPLIT.def;
    }
    function percent(v) { return Math.round(v * 10000) / 100 + "%"; }
    function fitSplit() {
      if (!hasSplit || pref.split == null) return syncSplit();
      var b = shareBounds();
      root.style.setProperty(SPLIT.prop, percent(between(pref.split, b[0], b[1])));
      syncSplit();
    }
    function syncSplit() {
      if (!splitGrip) return;
      var b = shareBounds();
      var now = pref.split != null ? between(pref.split, b[0], b[1]) : SPLIT.def;
      splitGrip.setAttribute("aria-valuemin", String(Math.round(b[0] * 100)));
      splitGrip.setAttribute("aria-valuemax", String(Math.round(b[1] * 100)));
      splitGrip.setAttribute("aria-valuenow", String(Math.round(now * 100)));
    }
    function setSplit(v) {
      if (!hasSplit) return;
      pref.split = typeof v === "number" && isFinite(v) ? clampShare(v) : null;
      persist();
      fit();
      emit();
    }

    function schedule() {
      if (pending !== null) return;
      var run = function () { pending = null; if (!gone) { fit(); emit(); } };
      pending = (win.requestAnimationFrame || function (f) { return win.setTimeout(f, 16); }).call(win, run);
    }

    // The behaviour every handle shares. o.begin(e) -> the drag's state, o.drag(state, e) moves
    // the setting, o.key(e) -> true when it moved it, o.reset() forgets it.
    function bindGrip(el, o) {
      var mine = function () { return drag && drag.el === el; };
      var done = function () { persist(); fit(); emit(); };
      el.addEventListener("pointerdown", function (e) {
        if (e.button) return;
        drag = o.begin(e);
        drag.el = el;
        el.setAttribute("data-dragging", "");
        try { el.setPointerCapture(e.pointerId); } catch (err) { /* no capture, moves still arrive */ }
        e.preventDefault();
      });
      el.addEventListener("pointermove", function (e) {
        if (!mine()) return;
        o.drag(drag, e);
        schedule();
      });
      var end = function () {
        if (!mine()) return;
        drag = null;
        el.removeAttribute("data-dragging");
        done();
      };
      el.addEventListener("pointerup", end);
      el.addEventListener("pointercancel", end);
      el.addEventListener("lostpointercapture", end);
      el.addEventListener("keydown", function (e) {
        if (!o.key(e)) return;
        e.preventDefault();
        done();
      });
      el.addEventListener("dblclick", function () { o.reset(); done(); });
    }
    function newGrip(key, label) {
      var el = doc.createElement("div");
      el.className = "colgrip";
      el.setAttribute("data-grip", key);
      el.setAttribute("role", "separator");
      el.setAttribute("aria-orientation", "vertical");
      el.setAttribute("aria-label", label);
      el.setAttribute("tabindex", "0");
      return el;
    }

    function makeGrip(c) {
      var pane = root.querySelector(c.pane);
      var dir = dirOf(c);
      var el = newGrip(c.key, left && c.key === "insp" ? c.leftLabel : c.label);
      // Next to its pane, as a child of .wb (the grid places it by name): before it, on its
      // left edge, or after it, on its right edge for the left inspector.
      if (pane.parentNode === root) root.insertBefore(el, dir > 0 ? pane.nextSibling : pane); else root.appendChild(el);
      var grow = dir > 0 ? "ArrowRight" : "ArrowLeft"; // the handle moves towards the pane's growing side
      var shrink = dir > 0 ? "ArrowLeft" : "ArrowRight";

      bindGrip(el, {
        begin: function (e) {
          var px = measure();
          return { col: c, x: e.clientX, start: widthNow(c, px), limit: limitOf(c, px) };
        },
        drag: function (d, e) {
          pref[c.key] = Math.round(between(d.start + dir * (e.clientX - d.x), c.min, d.limit));
        },
        key: function (e) {
          var px = measure();
          var limit = limitOf(c, px);
          var w;
          if (e.key === grow) w = widthNow(c, px) + STEP;
          else if (e.key === shrink) w = widthNow(c, px) - STEP;
          else if (e.key === "Home") w = c.min;
          else if (e.key === "End") w = limit;
          else return false;
          pref[c.key] = Math.round(between(w, c.min, limit));
          return true;
        },
        reset: function () { pref[c.key] = null; },
      });
      grips.push({ el: el, col: c });
    }

    function makeSplitGrip() {
      var el = newGrip("split", SPLIT.label);
      // Between the two halves, in their parent (theme.css places it on the seam).
      halves[1].parentNode.insertBefore(el, halves[1]);
      var share = function (v) { var b = shareBounds(); return Math.round(between(v, b[0], b[1]) * 10000) / 10000; };

      bindGrip(el, {
        begin: function (e) { return { x: e.clientX, start: shareNow(), width: hostWidth() || 1000 }; },
        drag: function (d, e) { pref.split = share(d.start + (e.clientX - d.x) / d.width); },
        key: function (e) {
          var b = shareBounds();
          var w = hostWidth() || 1000;
          var v;
          if (e.key === "ArrowRight") v = shareNow() + STEP / w; // the handle moves right, the first half grows
          else if (e.key === "ArrowLeft") v = shareNow() - STEP / w;
          else if (e.key === "Home") v = b[0];
          else if (e.key === "End") v = b[1];
          else return false;
          pref.split = share(v);
          return true;
        },
        reset: function () { pref.split = null; },
      });
      splitGrip = el;
    }

    // ---- Reset and teardown

    function reset() {
      drop(storage, KEY);
      drop(storage, PANEL_KEY);
      COLUMNS.forEach(function (c) { pref[c.key] = null; });
      pref.split = null;
      if (homeLayout === null) root.removeAttribute("data-layout"); else root.setAttribute("data-layout", homeLayout);
      syncLayoutButtons();
      if (managePanel) applyPanel(true);
      fit();
      emit();
    }

    function onClick(e) {
      var t = e.target;
      if (!t || !t.closest) return;
      var set = t.closest("[data-layout-set]");
      if (set) return set.getAttribute("aria-disabled") === "true" ? undefined : setLayout(set.getAttribute("data-layout-set"));
      if (t.closest("#panelBtn")) return setPanelOpen(!panelOpen());
      if (t.closest("[data-panel-close]")) return setPanelOpen(false);
      var tab = t.closest("[data-tab]");
      if (tab && tabbar && tabbar.contains(tab)) activateTab(tab.getAttribute("data-tab"));
      // The status bar's approval alert, where one pane shows at a time: show the Agent pane.
      if (t.closest(".alert") && isNarrow()) {
        var chat = doc.querySelector("#chat");
        var agent = tabs.filter(function (x) { return x.target && chat && doc.querySelector(x.target) === chat; })[0];
        if (agent) activateTab(agent.id);
      }
    }
    var onResize = function () { schedule(); };
    var onNarrow = function () { applyTabs(); fit(); emit(); };

    function destroy() {
      gone = true;
      root.removeEventListener("click", onClick);
      win.removeEventListener("resize", onResize);
      mq.removeEventListener("change", onNarrow);
      grips.forEach(function (g) { g.el.remove(); });
      grips = [];
      if (splitGrip) splitGrip.remove();
      splitGrip = null;
      clearUser();
      tabs.forEach(function (t) {
        var el = t.target ? doc.querySelector(t.target) : null;
        if (el) el.style.display = "";
      });
      if (initFrame.current === api) initFrame.current = null;
    }

    // ---- Start

    var saved = null;
    try { saved = JSON.parse(read(storage, KEY)); } catch (e) { saved = null; }
    if (saved && typeof saved === "object") {
      if (LAYOUTS.indexOf(saved.layout) >= 0) root.setAttribute("data-layout", saved.layout);
      COLUMNS.forEach(function (c) {
        if (typeof saved[c.key] === "number" && isFinite(saved[c.key])) pref[c.key] = clampCol(c, saved[c.key]);
      });
      if (hasSplit && typeof saved.split === "number" && isFinite(saved.split)) pref.split = clampShare(saved.split);
    }
    var storedPanel = read(storage, PANEL_KEY);
    // A page with no inspector and no Panel button keeps whatever body.panel-open it has.
    var managePanel = !!(panelBtn || root.querySelector(".inspector") || storedPanel);
    if (managePanel) applyPanel(storedPanel !== "closed");
    if (root.querySelector("[data-layout-set]")) syncLayoutButtons();
    buildTabs();
    applyTabs();
    cols.forEach(makeGrip);
    if (hasSplit) makeSplitGrip();

    root.addEventListener("click", onClick);
    win.addEventListener("resize", onResize);
    mq.addEventListener("change", onNarrow);
    fit();

    var api = {
      root: root,
      layout: layout, setLayout: setLayout,
      widths: widths, setWidths: setWidths,
      split: function () { return hasSplit ? pref.split : null; }, setSplit: setSplit,
      panelOpen: panelOpen, setPanelOpen: setPanelOpen,
      isNarrow: isNarrow,
      activeTab: function () { return active; }, activateTab: activateTab,
      reset: reset, destroy: destroy,
    };
    initFrame.current = api;
    return api;
  }

  initFrame.current = null;
  window.annealageFrame = initFrame;

  if (auto) {
    var start = function () { initFrame(); };
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
    else start();
  }
})();
