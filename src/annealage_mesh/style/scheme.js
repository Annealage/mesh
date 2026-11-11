// Light and dark: follow the OS unless the user picked one.
//
// Load it as a classic, blocking script in <head>, before the stylesheets:
//   <script src="/style/scheme.js"></script>
// so a saved choice is on <html> before the first paint. No inline script
// is needed, so script-src 'self' covers it.
//
// The choice is kept in localStorage under "annealage.scheme" ("light" or
// "dark"; absent means follow the OS), so it's per origin: two products on
// different ports remember separately. It sets <html data-theme>, which
// tokens.css turns into color-scheme, so every light-dark() token follows.
//
// window.annealageScheme:
//   choice()          "system" | "light" | "dark"
//   get()             the effective scheme, "light" | "dark"
//   set(v)            "light" | "dark" | null (back to the OS)
// A "schemechange" event fires on document.documentElement, detail
// {scheme, choice}, whenever the effective scheme changes: a set(), another
// tab changing it, or the OS changing while on system. Canvas and WebGL
// code redraws on it instead of watching matchMedia itself.
//
// Any <button data-scheme-toggle> cycles System, Light, Dark. It holds one
// <svg class="ico"><use href=".../icons.svg#i-auto"/></svg>, and the icon is
// swapped to match the choice (i-auto, i-sun, i-moon; the sprite path in the
// href is kept). The earlier markup still works: one
// <svg data-for="system|light|dark"> per state, of which theme.css shows the
// one matching the button's data-scheme.
//
// The marks' <picture><source media="(prefers-color-scheme: dark)"> would
// follow the OS and ignore a forced scheme, so those sources get their media
// rewritten to match, as they're parsed and on every change.
(function () {
  "use strict";
  var KEY = "annealage.scheme";
  var root = document.documentElement;
  var mq = window.matchMedia("(prefers-color-scheme: dark)");
  var ORDER = ["system", "light", "dark"];
  // A mark's dark source: its original media, or the one kept once rewritten.
  var SOURCES = "picture > source[media*='prefers-color-scheme'], picture > source[data-scheme-media]";
  var LABEL = { system: "Colours: follow the system", light: "Colours: light", dark: "Colours: dark" };
  var ICON = { system: "i-auto", light: "i-sun", dark: "i-moon" };

  function stored() {
    try {
      var v = localStorage.getItem(KEY);
      return v === "light" || v === "dark" ? v : null;
    } catch (e) {
      return null; // storage blocked: follow the OS
    }
  }
  function choice() { return stored() || "system"; }
  function effective() { return stored() || (mq.matches ? "dark" : "light"); }

  var last = null;

  function fixSources(scope) {
    var forced = stored();
    var list = (scope || document).querySelectorAll(SOURCES);
    for (var i = 0; i < list.length; i++) fixSource(list[i], forced);
  }
  function fixSource(s, forced) {
    if (!s.hasAttribute("data-scheme-media")) s.setAttribute("data-scheme-media", s.media);
    var orig = s.getAttribute("data-scheme-media");
    if (!forced) { s.media = orig; return; }
    var wantsDark = /prefers-color-scheme:\s*dark/.test(orig);
    s.media = (forced === "dark") === wantsDark ? "all" : "not all";
  }

  function syncButtons() {
    var c = choice();
    var next = ORDER[(ORDER.indexOf(c) + 1) % ORDER.length];
    var btns = document.querySelectorAll("[data-scheme-toggle]");
    for (var i = 0; i < btns.length; i++) {
      btns[i].setAttribute("data-scheme", c);
      btns[i].setAttribute("aria-label", LABEL[c] + ". Switch to " + next + ".");
      btns[i].title = LABEL[c];
      var use = btns[i].querySelector("svg:not([data-for]) > use");
      if (use) {
        var href = use.getAttribute("href") || "";
        use.setAttribute("href", href.slice(0, href.indexOf("#") + 1) + ICON[c]);
      }
    }
  }

  function apply() {
    var forced = stored();
    if (forced) root.setAttribute("data-theme", forced);
    else root.removeAttribute("data-theme");
    fixSources();
    syncButtons();
    var now = effective();
    if (last !== null && now !== last) {
      root.dispatchEvent(new CustomEvent("schemechange", { detail: { scheme: now, choice: choice() } }));
    }
    last = now;
  }

  function set(v) {
    try {
      if (v === "light" || v === "dark") localStorage.setItem(KEY, v);
      else localStorage.removeItem(KEY);
    } catch (e) { /* storage blocked: the choice lasts for this page only */ }
    apply();
    syncButtons(); // the choice can change without the effective scheme changing
  }

  window.annealageScheme = { choice: choice, get: effective, set: set };

  mq.addEventListener("change", apply);
  window.addEventListener("storage", function (e) { if (e.key === KEY || e.key === null) apply(); });

  document.addEventListener("click", function (e) {
    var b = e.target.closest && e.target.closest("[data-scheme-toggle]");
    if (!b) return;
    var next = ORDER[(ORDER.indexOf(choice()) + 1) % ORDER.length];
    set(next === "system" ? null : next);
  });

  // Now, before <body> is parsed: <html data-theme> for the first paint.
  apply();

  // While the page parses, fix each mark's <source> as it arrives, before its
  // <img> picks a file, so a forced scheme never shows the other mark.
  var mo = new MutationObserver(function (records) {
    var forced = stored();
    for (var i = 0; i < records.length; i++) {
      var added = records[i].addedNodes;
      for (var j = 0; j < added.length; j++) {
        var n = added[j];
        if (n.nodeType !== 1) continue;
        if (n.matches(SOURCES)) fixSource(n, forced);
        else if (n.querySelector) fixSources(n);
      }
    }
  });
  mo.observe(root, { childList: true, subtree: true });
  document.addEventListener("DOMContentLoaded", function () {
    mo.disconnect();
    fixSources();
    syncButtons();
  });
})();
