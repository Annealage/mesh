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
//   [data-layout-set="drawing|split|agent"]  buttons that choose it (aria-pressed)
//   #panelBtn                                toggles body.panel-open (aria-pressed)
//   [data-panel-close]                       closes the panel
//   #tabbar                                  the phone tab bar, role="tablist"
//     button[data-tab="id"][data-target="selector"]   one tab per pane (aria-selected)
//
// Settings, kept in localStorage per origin and per product (<html data-product>,
// or "platform"), and written only when the user changes something:
//   annealage.frame.<product>         {layout, insp, chat}  layout and column widths
//   annealage.frame.<product>.panel   "open" | "closed"     the Panel button
//
// annealageFrame(options) returns (and keeps as annealageFrame.current):
//   layout() / setLayout(v)       "drawing" | "split" | "agent"
//   widths() / setWidths({insp, chat})   pixels or null (the theme's default); clamped
//   panelOpen() / setPanelOpen(b)
//   isNarrow()                    900px and below, where the tab bar governs the panes
//   activeTab() / activateTab(id) the tab bar's current pane (kept at any width,
//                                 shown only at 900px and below)
//   reset()                       forget the stored settings and go back to the markup's
//   destroy()
// A "framechange" event fires on .wb, detail {layout, insp, chat, panelOpen,
// activeTab}, after each change, so a canvas or WebGL view can resize.
//
// Options: root (default the first .wb), storage (default localStorage), product,
// tabs ([{id, label, target}]: the tab bar is rebuilt from it; without it the
// tabs already in #tabbar are used).
//
// The widths are written as --insp-w-user and --chat-w-user on .wb, which
// theme.css reads only while that column is open. With nothing stored no
// property is written, so a product's own --insp-w and --chat-w stay the
// defaults. At 900px and below nothing is applied.
(function () {
  "use strict";
  var NARROW = "(max-width: 900px)";
  var LAYOUTS = ["drawing", "split", "agent"];
  var MIN_VIEW = 320; // the view keeps at least this much while a column is dragged
  var STEP = 16;
  // The resizable columns. index is the column's place in .wb's grid-template-columns
  // (rail, view, then these), pane the element that fills it.
  var COLUMNS = [
    { key: "insp", pane: ".inspector", prop: "--insp-w-user", index: 2, min: 240, max: 560, def: 300, label: "Resize the inspector" },
    { key: "chat", pane: "#chat, .console", prop: "--chat-w-user", index: 3, min: 320, max: 720, def: 400, label: "Resize the agent pane" },
  ];
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

  function inert() {
    var noop = function () {};
    return {
      layout: function () { return "split"; }, setLayout: noop,
      widths: function () { return { insp: null, chat: null }; }, setWidths: noop,
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
    var pref = {}; // column key -> stored width, or null for the theme's default
    var cols = COLUMNS.filter(function (c) { return root.querySelector(c.pane); });
    COLUMNS.forEach(function (c) { pref[c.key] = null; });
    var grips = [];
    var drag = null;
    var pending = null;
    var gone = false;

    var tabbar = root.querySelector("#tabbar");
    var panelBtn = root.querySelector("#panelBtn");
    var tabs = [];
    var active = null;

    // ---- State

    function layout() { return root.getAttribute("data-layout") || "split"; }
    function panelOpen() { return !!(doc.body && doc.body.classList.contains("panel-open")); }
    function isNarrow() { return mq.matches; }
    function widths() { return { insp: pref.insp, chat: pref.chat }; }

    function snapshot() {
      return { layout: layout(), insp: pref.insp, chat: pref.chat, panelOpen: panelOpen(), activeTab: active };
    }
    function emit() {
      root.dispatchEvent(new win.CustomEvent("framechange", { bubbles: true, detail: snapshot() }));
    }
    function persist() {
      var rec = { layout: layout() };
      COLUMNS.forEach(function (c) { if (pref[c.key] != null) rec[c.key] = pref[c.key]; });
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

    // The grid's resolved column widths [rail, view, insp, chat] in px, or null
    // where there is no layout to read.
    function measure() {
      var cs = win.getComputedStyle ? win.getComputedStyle(root) : null;
      var t = cs && cs.gridTemplateColumns;
      if (!t) return null;
      var p = t.split(/\s+/).map(parseFloat);
      return p.length === 4 && p.every(isFinite) ? p : null;
    }
    function clearUser() { cols.forEach(function (c) { root.style.removeProperty(c.prop); }); }

    // Apply the stored widths to the columns that are open now, narrowing them
    // (never below the default, or the limit) so the view keeps MIN_VIEW. A
    // column with nothing stored is left to the theme.
    function fit() {
      clearUser();
      if (isNarrow() || layout() === "agent") return syncGrips(null);
      var px = measure();
      var want = {};
      cols.forEach(function (c) {
        if (pref[c.key] == null) return;
        if (px && !(px[c.index] > 0)) return; // closed: the theme holds it at 0
        want[c.key] = clampCol(c, pref[c.key]);
      });
      if (px) {
        var excess = -(px[1] + px[2] + px[3] - MIN_VIEW);
        COLUMNS.forEach(function (c) { excess += want[c.key] != null ? want[c.key] : px[c.index]; });
        cols.slice().reverse().forEach(function (c) {
          if (want[c.key] == null || excess <= 0) return;
          var floor = Math.max(c.min, Math.min(want[c.key], px[c.index]));
          var cut = Math.min(excess, want[c.key] - floor);
          if (cut > 0) { want[c.key] -= cut; excess -= cut; }
        });
      }
      cols.forEach(function (c) {
        if (want[c.key] != null) root.style.setProperty(c.prop, Math.round(want[c.key]) + "px");
      });
      syncGrips(px && measure());
    }
    function syncGrips(px) {
      grips.forEach(function (g) {
        var c = g.col;
        var now = px ? px[c.index] : pref[c.key] != null ? pref[c.key] : c.def;
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
      return px ? Math.max(c.min, Math.min(c.max, px[1] + px[c.index] - MIN_VIEW)) : c.max;
    }
    function widthNow(c, px) {
      return px && px[c.index] > 0 ? px[c.index] : pref[c.key] != null ? pref[c.key] : c.def;
    }
    function schedule() {
      if (pending !== null) return;
      var run = function () { pending = null; if (!gone) { fit(); emit(); } };
      pending = (win.requestAnimationFrame || function (f) { return win.setTimeout(f, 16); }).call(win, run);
    }

    function makeGrip(c) {
      var pane = root.querySelector(c.pane);
      var el = doc.createElement("div");
      el.className = "colgrip";
      el.setAttribute("data-grip", c.key);
      el.setAttribute("role", "separator");
      el.setAttribute("aria-orientation", "vertical");
      el.setAttribute("aria-label", c.label);
      el.setAttribute("tabindex", "0");
      // Just before its pane, as a child of .wb (the grid places it by name).
      if (pane.parentNode === root) root.insertBefore(el, pane); else root.appendChild(el);

      el.addEventListener("pointerdown", function (e) {
        if (e.button) return;
        var px = measure();
        drag = { col: c, x: e.clientX, start: widthNow(c, px), limit: limitOf(c, px), id: e.pointerId };
        el.setAttribute("data-dragging", "");
        try { el.setPointerCapture(e.pointerId); } catch (err) { /* no capture, moves still arrive */ }
        e.preventDefault();
      });
      el.addEventListener("pointermove", function (e) {
        if (!drag || drag.col !== c) return;
        pref[c.key] = Math.round(between(drag.start + drag.x - e.clientX, c.min, drag.limit));
        schedule();
      });
      var end = function () {
        if (!drag || drag.col !== c) return;
        drag = null;
        el.removeAttribute("data-dragging");
        persist();
        fit();
        emit();
      };
      el.addEventListener("pointerup", end);
      el.addEventListener("pointercancel", end);
      el.addEventListener("lostpointercapture", end);
      el.addEventListener("keydown", function (e) {
        var px = measure();
        var limit = limitOf(c, px);
        var w;
        if (e.key === "ArrowLeft") w = widthNow(c, px) + STEP; // the handle moves left, the pane grows
        else if (e.key === "ArrowRight") w = widthNow(c, px) - STEP;
        else if (e.key === "Home") w = c.min;
        else if (e.key === "End") w = limit;
        else return;
        e.preventDefault();
        pref[c.key] = Math.round(between(w, c.min, limit));
        persist();
        fit();
        emit();
      });
      el.addEventListener("dblclick", function () {
        pref[c.key] = null;
        persist();
        fit();
        emit();
      });
      grips.push({ el: el, col: c });
    }

    // ---- Reset and teardown

    function reset() {
      drop(storage, KEY);
      drop(storage, PANEL_KEY);
      COLUMNS.forEach(function (c) { pref[c.key] = null; });
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
      if (set) return setLayout(set.getAttribute("data-layout-set"));
      if (t.closest("#panelBtn")) return setPanelOpen(!panelOpen());
      if (t.closest("[data-panel-close]")) return setPanelOpen(false);
      var tab = t.closest("[data-tab]");
      if (tab && tabbar && tabbar.contains(tab)) activateTab(tab.getAttribute("data-tab"));
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
    }
    var storedPanel = read(storage, PANEL_KEY);
    // A page with no inspector and no Panel button keeps whatever body.panel-open it has.
    var managePanel = !!(panelBtn || root.querySelector(".inspector") || storedPanel);
    if (managePanel) applyPanel(storedPanel !== "closed");
    if (root.querySelector("[data-layout-set]")) syncLayoutButtons();
    buildTabs();
    applyTabs();
    cols.forEach(makeGrip);

    root.addEventListener("click", onClick);
    win.addEventListener("resize", onResize);
    mq.addEventListener("change", onNarrow);
    fit();

    var api = {
      root: root,
      layout: layout, setLayout: setLayout,
      widths: widths, setWidths: setWidths,
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
