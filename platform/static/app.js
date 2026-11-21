/* OAAP app frame (RFC-0118 P2, design guidelines v0.3 §8).
 *
 * Served by the gateway at /platform/app.js on every instance site, so an
 * app includes it from its OWN origin, next to the kit:
 *
 *   <link rel="stylesheet" href="/platform/theme.css">
 *   <script src="/platform/app.js" defer></script>
 *
 * The app renders its header itself, with its name and a way back -- the
 * page must be whole without this script (UI principle 1):
 *
 *   <header class="oaap-header" data-oaap-product="vorgaenge"
 *           data-oaap-object="Vorgang:V-2026-0024" data-oaap-object-title="…">
 *     <a class="oaap-brand" href="…portal…">
 *       <span class="oaap-logo">…hexagon svg…</span>
 *       <span class="oaap-tenant"></span>
 *       <span class="oaap-appname">Vorgänge</span>
 *     </a>
 *     <span class="oaap-actions"></span>
 *   </header>
 *   …
 *   <footer class="oaap-footer"><span>Vorgänge 0.16.0</span>
 *     <span class="oaap-platform"></span></footer>
 *
 * What this script adds, each piece only when it exists:
 *  - the tenant's title and logo (from /platform/context.json),
 *  - "Hilfe": the app's product page in the tenant's help app (P4),
 *  - "Fragen": the tenant's chat, opened with where the person is (P5),
 *  - the user menu from /auth/whoami (guidelines §5.1 -- display name,
 *    Portal, Passwort ändern, Abmelden as a POST; never the roles),
 *  - "Feedback" in the footer through feedback.js (RFC-0061 §3.1),
 *  - "läuft auf OAAP <version>" in the footer.
 * It never invents: no display name made up from a login, no link to an
 * app the tenant does not have, no menu without a session.
 */
(function () {
  "use strict";
  if (window.oaapFrame) return;
  window.oaapFrame = { version: 1 };

  function el(tag, attrs, text) {
    var e = document.createElement(tag);
    for (var k in (attrs || {})) e.setAttribute(k, attrs[k]);
    if (text != null) e.textContent = text;
    return e;
  }
  function getJSON(url) {
    return fetch(url, { headers: { "Accept": "application/json" }, credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .catch(function () { return null; });
  }
  function trimSlash(u) { return String(u || "").replace(/\/+$/, ""); }

  function start() {
    var head = document.querySelector("header.oaap-header");
    var foot = document.querySelector("footer.oaap-footer");
    if (!head && !foot) return;
    Promise.all([getJSON("/platform/context.json"), getJSON("/auth/whoami")])
      .then(function (res) { fill(head, foot, res[0] || {}, res[1]); });
  }

  function fill(head, foot, ctx, me) {
    var app = ctx.app || {};
    var tenant = ctx.tenant || {};
    if (head) {
      var t = head.querySelector(".oaap-tenant");
      if (t && tenant.title && !t.textContent) t.textContent = tenant.title;
      var logo = head.querySelector(".oaap-logo");
      if (logo && tenant.logo) {
        var img = el("img", { src: tenant.logo, alt: "" });
        img.onerror = function () { img.remove(); };   // keep the hexagon
        img.onload = function () {
          Array.prototype.forEach.call(logo.querySelectorAll("svg"), function (s) { s.remove(); });
        };
        logo.appendChild(img);
      }
      var brand = head.querySelector("a.oaap-brand");
      if (brand && ctx.portal && (!brand.getAttribute("href") || brand.getAttribute("href") === "/" ||
                                  brand.hasAttribute("data-oaap-portal"))) {
        brand.setAttribute("href", ctx.portal);
        brand.setAttribute("title", "Zum Portal");
      }
      var acts = head.querySelector(".oaap-actions");
      if (!acts) { acts = el("span", { "class": "oaap-actions" }); head.appendChild(acts); }
      var product = head.getAttribute("data-oaap-product") || app.id || "";
      if (ctx.help && product && !acts.querySelector("[data-oaap-help]")) {
        acts.appendChild(el("a", { href: trimSlash(ctx.help) + "/p/" + encodeURIComponent(product),
                                   "data-oaap-help": "", target: "_blank", rel: "noopener" }, "Hilfe"));
      }
      if (ctx.chat && me && !acts.querySelector("[data-oaap-ask]")) {
        var a = el("a", { href: "#", "data-oaap-ask": "", target: "_blank", rel: "noopener" }, "Fragen");
        a.addEventListener("click", function () {
          // read when clicked: a page may change its object without reloading
          var obj = head.getAttribute("data-oaap-object") || "";
          var kontext = "app:" + (app.id || product) + (obj ? "/" + obj : "");
          var name = (head.querySelector(".oaap-appname") || {}).textContent || app.name || "";
          var otitle = head.getAttribute("data-oaap-object-title") || "";
          var frage = "Zu „" + name.trim() + "“" + (otitle ? " (" + otitle + ")" : "") + ": ";
          a.href = trimSlash(ctx.chat) + "/?frage=" + encodeURIComponent(frage) +
                   "&kontext=" + encodeURIComponent(kontext);
          // the default action follows the href set here, in a new tab
        });
        acts.appendChild(a);
      }
      if (me && me.username && !acts.querySelector(".oaap-user")) acts.appendChild(userMenu(me, ctx));
    }
    if (foot) {
      var p = foot.querySelector(".oaap-platform");
      if (p && ctx.platform && !p.textContent) p.textContent = "läuft auf OAAP " + ctx.platform;
      if (me && !foot.hasAttribute("data-oaap-no-feedback")) feedback(foot);
    }
  }

  function userMenu(me, ctx) {
    var d = el("details", { "class": "oaap-user" });
    d.appendChild(el("summary", null, me.display_name || me.username));
    var m = el("div", { "class": "oaap-menu" });
    m.appendChild(el("div", { "class": "oaap-menu-who" }, me.username));
    if (ctx.portal) m.appendChild(el("a", { href: ctx.portal }, "Portal"));
    if (me.links && me.links.password) m.appendChild(el("a", { href: me.links.password }, "Passwort ändern"));
    // Abmelden is a POST, never a link: an embedded image on any page
    // would otherwise log the person out (guidelines §5.1).
    var f = el("form", { method: "post", action: (me.links && me.links.logout) || "/auth/logout" });
    var b = el("button", { type: "submit", "class": "oaap-plain" }, "Abmelden");
    f.appendChild(b);
    m.appendChild(f);
    d.appendChild(m);
    document.addEventListener("click", function (ev) {
      if (d.open && !d.contains(ev.target)) d.open = false;
    });
    return d;
  }

  function feedback(foot) {
    getJSON("/platform/feedback/session").then(function (s) {
      if (!s || !s.ok) return;                  // no inbox for this tenant: nothing
      var go = function () {
        var b = el("button", { type: "button", "class": "oaap-plain oaap-feedback" }, "Feedback");
        b.style.cssText = "background:none;border:0;padding:0;color:inherit;cursor:pointer;text-decoration:underline;font:inherit";
        b.addEventListener("click", function () {
          var head = document.querySelector("header.oaap-header");
          var obj = head && head.getAttribute("data-oaap-object");
          var arg = {};
          if (obj && obj.indexOf(":") > 0) arg.object = { kind: obj.split(":")[0], id: obj.slice(obj.indexOf(":") + 1) };
          window.oaapFeedback.open(arg);
        });
        foot.appendChild(b);
      };
      if (window.oaapFeedback) return go();
      var sc = el("script", { src: "/platform/feedback.js", "data-oaap-feedback": "api-only" });
      sc.onload = function () { if (window.oaapFeedback) go(); };
      document.head.appendChild(sc);
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
