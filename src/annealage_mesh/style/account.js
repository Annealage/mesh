// The account menu and the product switch of a title bar, for a page served by Annealage hosted.
//
// Load it as a classic script at the end of <body>; it draws by itself once the page has
// parsed:
//   <script src="/style/account.js"></script>
// and give the title bar its places. Both are optional, and a page with neither makes no request:
//   <span class="menuwrap" data-account-menu></span>      last item of .tb-right: the account menu
//   <nav class="prodtabs" data-product-switch></nav>       the product tabs, for pages outside a workbench
// A placeholder may carry `hidden`; it is shown once it is filled. The script adds the missing
// class and aria-label itself.
//
// It makes one GET of /_front/me (hosted answers it on every product host; `data-src` on the
// first placeholder names another URL) with the page's own cookie, and fills the places from
// the answer: the signed-in name, the email, Account, Security, a "Products" group of links and
// Sign out. Where there is no answer (a standalone run, a public or capability visitor, a
// failed or slow request, scripts off) nothing is drawn and nothing is reported: the places stay
// empty. The page's title bar does not shift until the answer is in.
//
// The menu is the one of demo/workbench.html and demo/list.html: a .menuwrap with a
// [data-menu] button and a .menu. This script toggles data-open on the .menuwrap and
// aria-expanded on the button itself, and closes it on a click outside and on Escape, so a
// page that has its own generic [data-menu] handler (demo/demo.js has) must not load it:
// the two would toggle the same menu twice. The "Products" group is left out when the page has
// a [data-product-switch] place, and when the deployment serves one product only.
//
// Sign out posts to the URL the answer names (this host's /auth/logout), then goes to "/".
// A failure shows "Couldn't sign out. Try again." in the menu and leaves the page as it was.
//
// A page that draws its title bar later (a component that renders after the page has parsed)
// calls annealageAccount.draw(root) once the places exist; it fills the empty ones under root
// (default the document) from the same one answer.
//
// The icons come from icons.svg beside this script (its own src, with account.js swapped for
// icons.svg); `data-icons` on the place names another sprite.
//
// Names and emails are put in with textContent, links only when their URL is http, https or
// relative; there is no inline script or style, so script-src 'self' and connect-src 'self'
// are enough.
(function () {
  "use strict";
  var ME = "/_front/me";
  var SVG = "http://www.w3.org/2000/svg";
  var own = document.currentScript && document.currentScript.getAttribute("src");
  var SPRITE = own && /account\.js(\?.*)?$/.test(own) ? own.replace(/account\.js(\?.*)?$/, "icons.svg") : "/style/icons.svg";

  function el(tag, attrs, text) {
    var e = document.createElement(tag);
    for (var k in attrs) e.setAttribute(k, attrs[k]);
    if (text != null) e.textContent = text;
    return e;
  }
  function icon(sprite, name, cls) {
    var svg = document.createElementNS(SVG, "svg");
    svg.setAttribute("class", cls);
    var use = document.createElementNS(SVG, "use");
    use.setAttribute("href", sprite + "#" + name);
    svg.appendChild(use);
    return svg;
  }
  // An http, https or relative URL, else null: a page's own script cannot reach here with
  // a javascript: link, but the answer is data from the network all the same.
  function safe(url) {
    if (typeof url !== "string" || !url) return null;
    try {
      var p = new URL(url, window.location.href).protocol;
      return p === "http:" || p === "https:" ? url : null;
    } catch (e) {
      return null;
    }
  }

  function valid(me) {
    return !!me && typeof me === "object" && me.v === 1 && typeof me.display === "string";
  }

  function fillSwitch(nav, me) {
    if (!me.products || !me.products.length) return;
    var any = false;
    nav.classList.add("prodtabs");
    if (!nav.getAttribute("aria-label")) nav.setAttribute("aria-label", "Product");
    me.products.forEach(function (p) {
      var url = safe(p && p.url);
      if (!url || typeof p.name !== "string") return;
      var a = el("a", { href: url }, p.name);
      if (p.current === true) a.setAttribute("aria-current", "page");
      nav.appendChild(a);
      any = true;
    });
    if (any) nav.removeAttribute("hidden");
  }

  function fillMenu(wrap, me, withProducts) {
    var sprite = wrap.getAttribute("data-icons") || SPRITE;
    var name = me.display || me.email || "Account";
    wrap.classList.add("menuwrap");
    var button = el("button", {
      "class": "btn", type: "button", "data-menu": "", "aria-haspopup": "menu", "aria-expanded": "false",
      "aria-label": "Account menu, " + name,
    });
    button.appendChild(icon(sprite, "i-user", "ico"));
    button.appendChild(el("span", { "class": "wide" }, name));
    button.appendChild(icon(sprite, "i-chev-d", "ico ico--sm"));
    var menu = el("span", { "class": "menu", role: "menu" });
    if (typeof me.email === "string" && me.email) menu.appendChild(el("span", { "class": "menu-note" }, me.email));
    var account = safe(me.account_url), security = safe(me.security_url);
    if (account) menu.appendChild(el("a", { role: "menuitem", href: account }, "Account"));
    if (security) menu.appendChild(el("a", { role: "menuitem", href: security }, "Security"));
    var products = (withProducts && Array.isArray(me.products) ? me.products : []).filter(function (p) {
      return p && typeof p.name === "string" && safe(p.url);
    });
    if (products.length > 1) {
      menu.appendChild(el("hr", {}));
      menu.appendChild(el("span", { "class": "menu-note" }, "Products"));
      products.forEach(function (p) {
        var a = el("a", { role: "menuitem", href: p.url }, p.name);
        if (p.current === true) a.setAttribute("aria-current", "page");
        menu.appendChild(a);
      });
    }
    var out = me.signout;
    var signout = null;
    if (out && out.method === "POST" && typeof out.url === "string" && out.url.charAt(0) === "/" && out.url.charAt(1) !== "/") {
      menu.appendChild(el("hr", {}));
      signout = el("button", { type: "button", role: "menuitem" }, "Sign out");
      menu.appendChild(signout);
    }
    wrap.appendChild(button);
    wrap.appendChild(menu);
    wrap.removeAttribute("hidden");

    function close() {
      wrap.removeAttribute("data-open");
      button.setAttribute("aria-expanded", "false");
    }
    button.addEventListener("click", function () {
      var open = !wrap.hasAttribute("data-open");
      if (open) wrap.setAttribute("data-open", "");
      else wrap.removeAttribute("data-open");
      button.setAttribute("aria-expanded", String(open));
    });
    document.addEventListener("click", function (e) {
      if (wrap.hasAttribute("data-open") && !wrap.contains(e.target)) close();
    });
    document.addEventListener("keydown", function (e) {
      if (e.key !== "Escape" || !wrap.hasAttribute("data-open")) return;
      close();
      button.focus();
    });

    if (!signout) return;
    var failed = null;
    signout.addEventListener("click", function () {
      if (signout.disabled) return;
      signout.disabled = true;
      if (failed) { menu.removeChild(failed); failed = null; }
      fetch(out.url, { method: "POST", credentials: "same-origin", headers: { Accept: "application/json" } })
        .then(function (r) {
          if (!r.ok) throw new Error("sign out refused");
          window.location.assign("/");
        })
        .catch(function () {
          signout.disabled = false;
          failed = el("span", { "class": "menu-note", role: "alert" }, "Couldn't sign out. Try again.");
          menu.appendChild(failed);
        });
    });
  }

  var answers = {};
  function load(url) {
    if (!answers[url]) {
      answers[url] = fetch(url, { credentials: "same-origin", cache: "no-store", headers: { Accept: "application/json" } })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (me) { return valid(me) ? me : null; })
        .catch(function () { return null; });
    }
    return answers[url];
  }

  function empty(list) {
    return Array.prototype.filter.call(list, function (e) { return !e.children.length; });
  }

  // Fill the empty places under `root` (default the document). One request serves them all, and
  // a later call reuses its answer.
  function draw(root) {
    if (typeof fetch !== "function") return;
    root = root || document;
    var menus = empty(root.querySelectorAll("[data-account-menu]"));
    var tabs = empty(root.querySelectorAll("[data-product-switch]"));
    var first = menus[0] || tabs[0];
    if (!first) return;
    load(first.getAttribute("data-src") || ME).then(function (me) {
      if (!me) return;
      var hasSwitch = !!document.querySelector("[data-product-switch]");
      menus.forEach(function (m) { if (!m.children.length) fillMenu(m, me, !hasSwitch); });
      tabs.forEach(function (n) { if (!n.children.length) fillSwitch(n, me); });
    });
  }

  window.annealageAccount = { draw: draw };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", function () { draw(); });
  else draw();
})();
