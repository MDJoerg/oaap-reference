/* OAAP feedback button (RFC-0061 §3.1, stage 1).
 *
 * Served by the gateway at /platform/feedback.js on every instance site,
 * so an app includes it from its OWN origin:
 *
 *   <script src="/platform/feedback.js" defer></script>
 *   <script src="/platform/feedback.js" data-oaap-feedback="api-only" defer></script>
 *
 * The form posts to /platform/feedback on the same origin. The gateway
 * routes that to the `vorgaenge` instance of the app's tenant and adds
 * who is asking and which app, instance and version this is -- the app
 * never sees the request, so it cannot forge any of that.
 *
 * Rules this file keeps:
 *  - Without a session it shows nothing. Before rendering it asks
 *    /platform/feedback/session; anything but a 200 with {ok: true}
 *    (no session, public route, no `vorgaenge` instance) leaves the page
 *    untouched. A rule about the SESSION, not the route.
 *  - The object reference {kind, id} is read when the dialog OPENS,
 *    never at page load -- single-page apps change the current object
 *    without reloading.
 *  - window.oaapFeedback.open({object: {kind, id}}) is the same dialog,
 *    not a second door.
 *  - It never sends cookies' contents, storage, request or response
 *    bodies. The browser part (page path, user agent, screen, language,
 *    last uncaught error, last failed fetch) is shown in the form and
 *    can be left out.
 */
(function () {
  "use strict";
  if (window.oaapFeedback) return;            // included twice: once is enough

  var script = document.currentScript;
  var apiOnly = !!(script && script.getAttribute("data-oaap-feedback") === "api-only");
  var BASE = "/platform/feedback";
  var KINDS = [["bug", "Fehler"], ["question", "Frage"],
               ["idea", "Idee"], ["change", "Änderungswunsch"]];

  // ---------------------------------------------------- what the browser knows
  var last = { error: "", fetch: "" };

  function clip(s, n) { s = String(s == null ? "" : s); return s.length > n ? s.slice(0, n) + "…" : s; }
  function pathOnly(u) {
    try { var x = new URL(u, location.href); return x.origin === location.origin ? x.pathname : x.origin + x.pathname; }
    catch (e) { return clip(String(u).split("?")[0], 200); }
  }

  window.addEventListener("error", function (ev) {
    if (!ev) return;
    last.error = clip((ev.message || "Error") +
      (ev.filename ? " @ " + pathOnly(ev.filename) + ":" + (ev.lineno || 0) : ""), 300);
  });
  window.addEventListener("unhandledrejection", function (ev) {
    var r = ev && ev.reason;
    last.error = clip("Unhandled rejection: " + (r && r.message ? r.message : r), 300);
  });
  if (window.fetch) {
    var origFetch = window.fetch;
    window.fetch = function (input, init) {
      var url = typeof input === "string" ? input : (input && input.url) || "";
      var method = (init && init.method) || (input && input.method) || "GET";
      var p = origFetch.apply(this, arguments);
      if (url.indexOf(BASE) === -1) {
        p.then(function (res) {
          if (res && !res.ok && res.type !== "opaqueredirect")
            last.fetch = clip(method.toUpperCase() + " " + pathOnly(url) + " → " + res.status, 300);
        }, function (err) {
          last.fetch = clip(method.toUpperCase() + " " + pathOnly(url) + " → " + (err && err.message || "network error"), 300);
        });
      }
      return p;
    };
  }

  function browserPart() {
    return {
      path: location.pathname,
      user_agent: clip(navigator.userAgent, 300),
      screen: (window.screen ? screen.width + "x" + screen.height : "") +
              " (" + window.innerWidth + "x" + window.innerHeight + ")",
      language: navigator.language || "",
      last_error: last.error,
      last_failed_fetch: last.fetch
    };
  }

  // ------------------------------------------------------- the object at hand
  function parseObject(v) {
    if (!v) return null;
    if (typeof v === "object") {
      return v.id ? { kind: clip(v.kind || "", 40), id: clip(v.id, 200) } : null;
    }
    var s = String(v), i = s.indexOf(":");
    if (i <= 0) return { kind: "", id: clip(s, 200) };
    return { kind: clip(s.slice(0, i), 40), id: clip(s.slice(i + 1), 200) };
  }

  function currentObject(explicit) {
    if (explicit) return parseObject(explicit);
    var marked = document.querySelectorAll("[data-oaap-object]");
    for (var i = 0; i < marked.length; i++) {
      if (marked[i] !== script && marked[i].getAttribute("data-oaap-object"))
        return parseObject(marked[i].getAttribute("data-oaap-object"));
    }
    return script ? parseObject(script.getAttribute("data-oaap-object")) : null;
  }

  // ---------------------------------------------------------- session check
  var ready = fetch(BASE + "/session", {
    credentials: "same-origin", redirect: "manual",
    headers: { "Accept": "application/json" }
  }).then(function (res) {
    if (res.status !== 200) return null;
    return res.json().then(function (d) { return d && d.ok === true ? d : null; },
                           function () { return null; });
  }, function () { return null; });

  // ------------------------------------------------------------------- UI
  var host, root;

  function el(tag, attrs, kids) {
    var e = document.createElement(tag);
    for (var k in (attrs || {})) {
      if (k === "text") e.textContent = attrs[k];
      else e.setAttribute(k, attrs[k]);
    }
    (kids || []).forEach(function (c) { if (c) e.appendChild(c); });
    return e;
  }

  var CSS =
    ":host{all:initial}" +
    "*{box-sizing:border-box;font-family:var(--oaap-font-family,system-ui,-apple-system,'Segoe UI',sans-serif);font-size:14px}" +
    ".fab{position:fixed;right:16px;bottom:16px;z-index:2147483000;border:0;border-radius:20px;padding:8px 14px;" +
    "background:var(--oaap-color-primary,#2f6fed);color:var(--oaap-color-primary-text,#fff);cursor:pointer;box-shadow:0 2px 8px rgba(0,0,0,.25)}" +
    ".back{position:fixed;inset:0;z-index:2147483001;background:rgba(0,0,0,.35);display:flex;align-items:center;justify-content:center;padding:16px}" +
    ".dlg{background:var(--oaap-color-bg,#fff);color:var(--oaap-color-text,#1a1d21);border-radius:8px;max-width:520px;width:100%;" +
    "max-height:calc(100vh - 32px);overflow:auto;padding:16px;box-shadow:0 8px 32px rgba(0,0,0,.3)}" +
    "h2{font-size:17px;margin:0 0 12px}label{display:block;margin:10px 0 4px;color:var(--oaap-color-text-muted,#5b616b)}" +
    "input,select,textarea{width:100%;padding:6px 8px;border:1px solid var(--oaap-color-border,#d8dbe0);border-radius:4px;background:#fff;color:#1a1d21}" +
    "textarea{min-height:110px;resize:vertical}" +
    "pre{white-space:pre-wrap;word-break:break-word;background:var(--oaap-color-surface,#f4f5f7);padding:8px;border-radius:4px;font-family:ui-monospace,monospace;font-size:12px;margin:4px 0}" +
    ".row{display:flex;gap:8px;justify-content:flex-end;margin-top:14px}" +
    "button.b{border:1px solid var(--oaap-color-border,#d8dbe0);background:#fff;border-radius:4px;padding:6px 12px;cursor:pointer}" +
    "button.p{background:var(--oaap-color-primary,#2f6fed);color:var(--oaap-color-primary-text,#fff);border-color:transparent}" +
    ".chk{display:flex;gap:6px;align-items:center;margin-top:10px}.chk input{width:auto}" +
    ".msg{margin-top:10px}.err{color:var(--oaap-color-danger,#c4342f)}.ok{color:var(--oaap-color-success,#1f8a4c)}" +
    ".obj{color:var(--oaap-color-text-muted,#5b616b);margin-top:6px}";

  function mount() {
    if (host) return;
    host = el("div", { "data-oaap-feedback-host": "" });
    root = host.attachShadow ? host.attachShadow({ mode: "open" }) : host;
    root.appendChild(el("style", { text: CSS }));
    document.body.appendChild(host);
  }

  function showButton() {
    mount();
    var b = el("button", { "class": "fab", type: "button", text: "Feedback",
                           "aria-label": "Feedback oder Frage an die Verantwortlichen" });
    b.addEventListener("click", function () { open(); });
    root.appendChild(b);
  }

  function openDialog(opts, session) {
    mount();
    var obj = currentObject(opts && opts.object);
    var tech = browserPart();
    var kinds = (session.kinds && session.kinds.length) ? session.kinds : KINDS.map(function (k) { return k[0]; });
    var sel = el("select", { id: "k" });
    KINDS.forEach(function (k) {
      if (kinds.indexOf(k[0]) !== -1) sel.appendChild(el("option", { value: k[0], text: k[1] }));
    });
    if (opts && opts.kind) sel.value = opts.kind;
    var title = el("input", { id: "t", maxlength: "200", autocomplete: "off" });
    var body = el("textarea", { id: "x", maxlength: "10000" });
    var withTech = el("input", { type: "checkbox", id: "w", checked: "" });
    var msg = el("div", { "class": "msg", role: "status" });
    var send = el("button", { "class": "b p", type: "button", text: "Senden" });
    var cancel = el("button", { "class": "b", type: "button", text: "Abbrechen" });
    var techText = Object.keys(tech).filter(function (k) { return tech[k]; })
      .map(function (k) { return k + ": " + tech[k]; }).join("\n");
    var dlg = el("div", { "class": "dlg", role: "dialog", "aria-modal": "true", "aria-labelledby": "h" }, [
      el("h2", { id: "h", text: "Feedback" + (session.tenant_title ? " an " + session.tenant_title : "") }),
      el("label", { "for": "k", text: "Art" }), sel,
      el("label", { "for": "t", text: "Betreff" }), title,
      el("label", { "for": "x", text: "Beschreibung" }), body,
      obj ? el("div", { "class": "obj", text: "Bezug: " + (obj.kind ? obj.kind + " " : "") + obj.id }) : null,
      el("div", { "class": "chk" }, [withTech, el("label", { "for": "w", text: "Technische Angaben aus dem Browser mitsenden:" })]),
      el("pre", { text: techText }),
      msg,
      el("div", { "class": "row" }, [cancel, send])
    ]);
    var back = el("div", { "class": "back" }, [dlg]);
    function close() { if (back.parentNode) back.parentNode.removeChild(back); document.removeEventListener("keydown", onKey, true); }
    function onKey(e) { if (e.key === "Escape") close(); }
    document.addEventListener("keydown", onKey, true);
    back.addEventListener("click", function (e) { if (e.target === back) close(); });
    cancel.addEventListener("click", close);
    var sent = false;
    send.addEventListener("click", function () {
      if (sent) { close(); return; }
      if (!title.value.trim()) { msg.className = "msg err"; msg.textContent = "Bitte einen Betreff angeben."; title.focus(); return; }
      send.disabled = true;
      msg.className = "msg"; msg.textContent = "Wird gesendet …";
      var doc = { kind: sel.value, title: title.value.trim(), body: body.value,
                  object: obj, browser: withTech.checked ? tech : null };
      fetch(BASE, { method: "POST", credentials: "same-origin", redirect: "manual",
                    headers: { "Content-Type": "application/json", "Accept": "application/json" },
                    body: JSON.stringify(doc) })
        .then(function (res) {
          return res.json().catch(function () { return {}; }).then(function (d) { return [res, d]; });
        })
        .then(function (rd) {
          var res = rd[0], d = rd[1];
          if (res.status === 201 && d.number) {
            msg.className = "msg ok";
            msg.textContent = "Danke. Vorgang " + d.number + " ist angelegt.";
            sent = true;
            send.textContent = "Schließen"; send.disabled = false;
          } else {
            send.disabled = false;
            msg.className = "msg err";
            msg.textContent = (d && d.error) ? d.error : "Senden fehlgeschlagen (" + res.status + ").";
          }
        }, function () {
          send.disabled = false;
          msg.className = "msg err"; msg.textContent = "Senden fehlgeschlagen (keine Verbindung).";
        });
    });
    root.appendChild(back);
    title.focus();
  }

  function open(opts) {
    return ready.then(function (session) {
      if (!session) return false;
      openDialog(opts || {}, session);
      return true;
    });
  }

  window.oaapFeedback = { open: open, ready: ready.then(function (s) { return !!s; }) };

  ready.then(function (session) {
    if (!session || apiOnly) return;
    if (document.body) showButton();
    else document.addEventListener("DOMContentLoaded", showButton);
  });
})();
