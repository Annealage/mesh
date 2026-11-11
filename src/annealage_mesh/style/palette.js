// The command palette: the panel behind the title bar's command field (.cmd).
// A classic script with no dependencies and one global, window.annealagePalette.
// It draws .palette, .palette-box and their children (theme.css), builds them on
// first open, and touches nothing else on the page except the .cmd buttons'
// key label and aria attributes.
//
//   <script src="/style/palette.js"></script>
//
//   var off = annealagePalette.register([
//     { id: "sheet.4", title: "Go to sheet 4", group: "Sheets", keywords: ["page"],
//       hint: "Power", shortcut: "G 4",
//       enabled: function () { return hasDesign; },   // asked each time the palette opens
//       disabledReason: "Open a design first",         // shown as the hint while disabled
//       run: function () { openSheet(4); } },          // may return a promise
//   ]);
//   off();                                             // unregister those commands
//
// A command: id (unique per page; registering an id again replaces the earlier
// command, and the earlier one's unregister then leaves it alone), title,
// run, and optional group, keywords (array or string), hint, shortcut (one
// string, drawn as a kbd), icon (the href of an <use>, e.g. "/style/icons.svg#i-sheet"),
// enabled (function or boolean) and disabledReason. Titles, hints and groups are
// set as text, never as HTML. A command without id, title or run is skipped
// with a console warning.
//
// annealagePalette.register(commands)  array or one command; returns unregister()
// annealagePalette.open([{query, opener}])  show the palette (query pre-fills the input)
// annealagePalette.close()             hide it and return focus to the opener
// annealagePalette.isOpen()
// A "palettechange" event fires on document when the registry changes while the
// palette is open; the list is redrawn by itself.
//
// Keys: Ctrl K and Cmd K open it (and close it when open). Inside, Up and Down
// move (wrapping), Home and End jump, Enter runs (on a disabled row it says why it cannot), Escape closes, Tab closes. The
// chord is left alone when focus is inside [data-palette-ignore] (an editor or
// the agent composer that uses Ctrl K itself) and when an earlier handler has
// already called preventDefault on it. Clicking a .cmd button opens it too.
//
// run is called after the palette has closed and focus is back on the element
// that had it, so a command can open a menu or a confirm row anchored there. A
// run that throws (or rejects) is logged with console.error and nothing else
// happens; showing the failure is the product's job.
//
// Matching: case-insensitive; every whitespace-separated term of the query must
// match. A term ranks best as the start of the title, then the start of a word in
// the title, then a substring of the title, then a subsequence of the title, then
// a hit in keywords or group. Ties keep registration order. An empty query lists
// everything in registration order. Groups appear in the order of their first row.
(function () {
  "use strict";
  if (typeof window === "undefined" || !window.document || window.annealagePalette) return;

  var doc = window.document;
  var PLACEHOLDER = "Search or run a command";
  var MAX_RANK = 4;

  var entries = new Map(); // id -> { cmd, token }
  var layer = null, box = null, input = null, list = null, live = null;
  var st = null; // while open: { opener, items, shown, rows, active }
  var seq = 0;

  function warn(msg, err) {
    if (window.console) {
      if (err === undefined) window.console.warn("annealagePalette: " + msg);
      else window.console.error("annealagePalette: " + msg, err);
    }
  }

  // ---- The registry.
  function str(v) { return typeof v === "string" ? v : ""; }
  function normalise(c) {
    if (!c || typeof c.id !== "string" || !c.id || typeof c.title !== "string" || typeof c.run !== "function") return null;
    var kw = Array.isArray(c.keywords) ? c.keywords : (typeof c.keywords === "string" ? [c.keywords] : []);
    return {
      id: c.id, title: c.title, group: str(c.group), hint: str(c.hint), shortcut: str(c.shortcut), icon: str(c.icon),
      keywords: kw.filter(function (k) { return typeof k === "string"; }).join(" ").toLowerCase(),
      enabled: c.enabled, disabledReason: str(c.disabledReason), run: c.run,
    };
  }
  function register(commands) {
    var list_ = Array.isArray(commands) ? commands : [commands];
    var mine = [];
    list_.forEach(function (c) {
      var cmd = normalise(c);
      if (!cmd) { warn("skipped a command without id, title and run"); return; }
      var entry = { cmd: cmd, token: ++seq };
      entries.set(cmd.id, entry);
      mine.push(entry);
    });
    changed();
    return function unregister() {
      var any = false;
      mine.forEach(function (e) {
        if (entries.get(e.cmd.id) === e) { entries.delete(e.cmd.id); any = true; }
      });
      mine = [];
      if (any) changed();
    };
  }
  function changed() {
    if (!st) return;
    refresh(true);
    try { doc.dispatchEvent(new window.CustomEvent("palettechange")); } catch (e) { /* no CustomEvent */ }
  }

  // ---- Matching.
  var WORDCH = /[\p{L}\p{N}]/u;
  function subsequence(term, text) {
    var j = 0;
    for (var i = 0; i < text.length && j < term.length; i++) if (text.charAt(i) === term.charAt(j)) j++;
    return j === term.length;
  }
  // 0 prefix, 1 word start, 2 substring, 3 subsequence, 4 keyword or group, -1 no match.
  function termRank(term, title, extra) {
    var i = title.indexOf(term);
    if (i === 0) return 0;
    if (i > 0) {
      for (var at = i; at > 0; at = title.indexOf(term, at + 1)) if (!WORDCH.test(title.charAt(at - 1))) return 1;
      return 2;
    }
    if (subsequence(term, title)) return 3;
    if (extra.indexOf(term) >= 0) return MAX_RANK;
    return -1;
  }
  function score(terms, item) {
    var title = item.cmd.title.toLowerCase();
    var extra = item.cmd.keywords + " " + item.cmd.group.toLowerCase();
    var total = 0;
    for (var i = 0; i < terms.length; i++) {
      var r = termRank(terms[i], title, extra);
      if (r < 0) return -1;
      total += r;
    }
    return total;
  }

  // ---- State at open time.
  function snapshot() {
    var items = [];
    entries.forEach(function (e) {
      var cmd = e.cmd, ok = true;
      try {
        ok = typeof cmd.enabled === "function" ? !!cmd.enabled() : cmd.enabled !== false;
      } catch (err) { warn("enabled() of " + cmd.id + " threw", err); ok = false; }
      items.push({ cmd: cmd, order: items.length, disabled: !ok });
    });
    return items;
  }
  function filtered(items, query) {
    var terms = query.toLowerCase().split(/\s+/).filter(Boolean);
    var out = [];
    items.forEach(function (it) {
      var s = terms.length ? score(terms, it) : 0;
      if (s >= 0) out.push({ item: it, score: s });
    });
    out.sort(function (a, b) { return a.score - b.score || a.item.order - b.item.order; });
    // Group by label, in the order each group first appears.
    var groups = [], byName = {};
    out.forEach(function (r) {
      var g = r.item.cmd.group, bucket = Object.prototype.hasOwnProperty.call(byName, g) ? byName[g] : null;
      if (!bucket) { bucket = byName[g] = { name: g, items: [] }; groups.push(bucket); }
      bucket.items.push(r.item);
    });
    return groups;
  }

  // ---- The panel.
  function el(tag, cls, text) {
    var e = doc.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }
  function build() {
    if (layer) return;
    layer = el("div", "palette");
    layer.hidden = true;
    box = el("div", "palette-box");
    box.setAttribute("role", "dialog");
    box.setAttribute("aria-label", "Command palette");
    input = el("input", "input palette-input");
    input.type = "text";
    input.setAttribute("role", "combobox");
    input.setAttribute("aria-label", PLACEHOLDER);
    input.setAttribute("aria-autocomplete", "list");
    input.setAttribute("aria-expanded", "false");
    input.setAttribute("aria-controls", "annealage-palette-list");
    input.setAttribute("autocomplete", "off");
    input.setAttribute("spellcheck", "false");
    input.setAttribute("placeholder", PLACEHOLDER);
    list = el("div", "palette-list");
    list.id = "annealage-palette-list";
    list.setAttribute("role", "listbox");
    list.setAttribute("aria-label", "Commands");
    live = el("div", "sr");
    live.setAttribute("aria-live", "polite");
    box.appendChild(input);
    box.appendChild(list);
    box.appendChild(live);
    layer.appendChild(box);
    doc.body.appendChild(layer);

    input.addEventListener("input", function () { if (st) refresh(false); });
    layer.addEventListener("mousedown", function (e) {
      // Keep focus in the input while a row is pressed.
      if (e.target !== input && e.preventDefault) e.preventDefault();
    });
    layer.addEventListener("click", function (e) {
      if (!st) return;
      if (e.target === layer) { close(); return; }
      var row = e.target.closest ? e.target.closest(".palette-row") : null;
      if (!row) return;
      var i = Number(row.getAttribute("data-i"));
      if (i >= 0 && i < st.rows.length) { setActive(i); runActive(); }
    });
    layer.addEventListener("mousemove", function (e) {
      var row = st && e.target.closest ? e.target.closest(".palette-row") : null;
      if (!row) return;
      var i = Number(row.getAttribute("data-i"));
      if (i !== st.active) setActive(i);
    });
  }

  function row(item, i) {
    var cmd = item.cmd;
    var r = el("div", "palette-row");
    r.id = "annealage-palette-opt-" + i;
    r.setAttribute("role", "option");
    r.setAttribute("data-i", String(i));
    r.setAttribute("aria-selected", "false");
    if (item.disabled) r.setAttribute("aria-disabled", "true");
    if (cmd.icon && doc.createElementNS) {
      var NS = "http://www.w3.org/2000/svg";
      var svg = doc.createElementNS(NS, "svg");
      svg.setAttribute("class", "ico");
      svg.setAttribute("aria-hidden", "true");
      var use = doc.createElementNS(NS, "use");
      use.setAttribute("href", cmd.icon);
      svg.appendChild(use);
      r.appendChild(svg);
    }
    r.appendChild(el("span", "palette-title", cmd.title));
    var hint = item.disabled ? cmd.disabledReason || cmd.hint : cmd.hint;
    if (hint) r.appendChild(el("span", "palette-hint", hint));
    if (cmd.shortcut && !item.disabled) r.appendChild(el("kbd", "", cmd.shortcut));
    return r;
  }

  function count(n) { return n === 0 ? "No commands" : n + (n === 1 ? " command" : " commands"); }

  // Rebuild the list from the query. keepActive keeps the selected command when it is still shown.
  function refresh(reload) {
    if (reload) st.items = snapshot();
    var keepId = reload && st.active >= 0 && st.rows[st.active] ? st.rows[st.active].item.cmd.id : null;
    var groups = filtered(st.items, input.value);
    while (list.firstChild) list.removeChild(list.firstChild);
    st.rows = [];
    st.active = -1;
    groups.forEach(function (g) {
      if (g.name) {
        var label = el("div", "palette-group", g.name);
        label.setAttribute("role", "presentation");
        list.appendChild(label);
      }
      g.items.forEach(function (item) {
        var r = row(item, st.rows.length);
        st.rows.push({ item: item, el: r });
        list.appendChild(r);
      });
    });
    if (!st.rows.length) {
      var none = el("div", "palette-empty", st.items.length ? "No commands match" : "No commands here");
      none.setAttribute("role", "presentation");
      list.appendChild(none);
    }
    live.textContent = count(st.rows.length);
    var first = -1, keep = -1;
    st.rows.forEach(function (r, i) {
      if (first < 0 && !r.item.disabled) first = i;
      if (keepId && r.item.cmd.id === keepId) keep = i;
    });
    setActive(keep >= 0 ? keep : first >= 0 ? first : st.rows.length ? 0 : -1);
  }

  function setActive(i) {
    if (st.active >= 0 && st.rows[st.active]) st.rows[st.active].el.setAttribute("aria-selected", "false");
    st.active = i;
    var cur = i >= 0 ? st.rows[i] : null;
    if (cur) {
      cur.el.setAttribute("aria-selected", "true");
      input.setAttribute("aria-activedescendant", cur.el.id);
      if (cur.el.scrollIntoView) cur.el.scrollIntoView({ block: "nearest" });
    } else {
      input.removeAttribute("aria-activedescendant");
    }
  }
  function move(delta) {
    var n = st.rows.length;
    if (!n) return;
    setActive(st.active < 0 ? (delta > 0 ? 0 : n - 1) : (st.active + delta + n) % n);
  }

  function fail(id, err) { warn("command " + id + " failed", err); }
  function runActive() {
    var cur = st && st.active >= 0 ? st.rows[st.active] : null;
    if (!cur) return;
    if (cur.item.disabled) {
      // Say why it did not run, to everyone: the reason is the row's hint, and the live region reads it out.
      live.textContent = cur.item.cmd.title + ": " + (cur.item.cmd.disabledReason || cur.item.cmd.hint || "Not available now");
      return;
    }
    var cmd = cur.item.cmd;
    close();
    try {
      var result = cmd.run();
      if (result && typeof result.then === "function") result.then(null, function (err) { fail(cmd.id, err); });
    } catch (err) { fail(cmd.id, err); }
  }

  // ---- Open and close.
  function modifierLabel() {
    var nav = window.navigator || {};
    var p = (nav.userAgentData && nav.userAgentData.platform) || nav.platform || "";
    return /mac|iphone|ipad|ipod/i.test(p) ? "Cmd" : "Ctrl";
  }
  function triggers() { return doc.querySelectorAll ? doc.querySelectorAll(".cmd") : []; }
  // The field's key label comes from here so every page shows the right modifier.
  function syncTriggers() {
    var label = modifierLabel() + " K";
    Array.prototype.forEach.call(triggers(), function (t) {
      t.setAttribute("aria-haspopup", "dialog");
      t.setAttribute("aria-expanded", st ? "true" : "false");
      var k = t.querySelector("kbd");
      if (k) k.textContent = label;
    });
  }

  function open(options) {
    options = options || {};
    if (!doc.body) return false;
    var query = typeof options.query === "string" ? options.query : null;
    if (st) {
      if (query !== null) { input.value = query; refresh(false); }
      input.focus();
      return true;
    }
    build();
    var opener = options.opener || doc.activeElement || null;
    st = { opener: opener, items: snapshot(), rows: [], active: -1 };
    layer.hidden = false;
    input.value = query || "";
    input.setAttribute("aria-expanded", "true");
    refresh(false);
    syncTriggers();
    input.focus();
    return true;
  }
  function close() {
    if (!st) return;
    var opener = st.opener;
    st = null;
    layer.hidden = true;
    input.setAttribute("aria-expanded", "false");
    input.removeAttribute("aria-activedescendant");
    input.value = "";
    syncTriggers();
    if (opener && opener !== doc.body && opener !== doc.documentElement && opener.focus && opener.isConnected !== false) opener.focus();
  }

  // ---- Keys and clicks, on the document.
  doc.addEventListener("keydown", function (e) {
    var k = e.key;
    if ((e.ctrlKey || e.metaKey) && !e.altKey && !e.shiftKey && (k === "k" || k === "K")) {
      if (st) { e.preventDefault(); close(); return; }
      if (e.defaultPrevented) return;
      var t = e.target;
      if (t && t.closest && t.closest("[data-palette-ignore]")) return;
      if (open({ opener: t && t.focus ? t : null })) e.preventDefault();
      return;
    }
    if (!st) return;
    if (k === "Escape") { e.preventDefault(); close(); }
    else if (k === "Tab") close();
    else if (k === "ArrowDown") { e.preventDefault(); move(1); }
    else if (k === "ArrowUp") { e.preventDefault(); move(-1); }
    else if (k === "Home") { e.preventDefault(); if (st.rows.length) setActive(0); }
    else if (k === "End") { e.preventDefault(); if (st.rows.length) setActive(st.rows.length - 1); }
    else if (k === "Enter" && !e.isComposing) { e.preventDefault(); runActive(); }
  });
  doc.addEventListener("click", function (e) {
    var t = e.target && e.target.closest ? e.target.closest(".cmd") : null;
    if (!t) return;
    if (st) close(); else open({ opener: t });
  });

  if (doc.readyState === "loading") doc.addEventListener("DOMContentLoaded", syncTriggers);
  else syncTriggers();

  window.annealagePalette = {
    register: register,
    open: open,
    close: close,
    isOpen: function () { return !!st; },
  };
})();
