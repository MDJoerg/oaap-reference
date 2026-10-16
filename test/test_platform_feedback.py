"""/platform/* auf jeder Instanz-Site und der beglaubigte Feedback-Weg
(RFC-0061 §3.0/§3.1, Stufe 1, 0.1.199).

    python3 test/test_platform_feedback.py

Was hier gehalten wird:

1. Jede Instanz-Site reserviert /platform/feedback* (mit Anmeldung, zum
   `vorgaenge` des EIGENEN Mandanten) und /platform/* (oeffentlich,
   statisch) -- vor jeder App-Route, damit /platform/feedback.js nicht
   mehr bei der App als 404 landet.
2. Die Herkunftszeilen setzt nur das Gateway, und nur auf diesem Weg;
   auf jeder Anfrage jeder Instanz-Site streicht es sie zuerst.
3. Ohne `vorgaenge` im Mandanten: 404, und das Script zeigt nichts.
4. Das Ziel folgt der Registry: kommt, geht oder wechselt das
   `vorgaenge`, werden die LAN-Sites des Mandanten neu geschrieben.
5. Das Manifest lehnt App-Routen unter /platform/ und /auth/ ab.
6. Die Migration bringt den Block in Sites, die schon auf der Platte
   liegen, und schweigt beim zweiten Lauf.
"""
import contextlib
import io as _io
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-feedback-test-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, os.path.join(HERE, "..", "platform"))

import appctl as m                                            # noqa: E402

RELOADS = []
m.reload_gateway = lambda: RELOADS.append(1)
os.makedirs(m.CADDY_APPS_DIR, exist_ok=True)

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:600]}")


def record(name, tenant, app_id="crm", channel="production", port=8101,
           version="1.0.0", routes=None):
    c = f"oaap-app-{name}"
    return {"app_id": app_id, "app_name": app_id, "version": version,
            "channel": channel, "port": port, "container": c, "svc_port": 8000,
            "image": f"oaap-app/{app_id}:{version}", "tenant": tenant,
            "id": name.replace("-", "")[:12].ljust(12, "x"), "name": name,
            "services": [{"service": "", "container": c, "image": "",
                          "build": "", "port": 8000}],
            "routes": routes or [{"path": "/", "roles": ["user", "admin"]},
                                 {"path": "/display", "roles": ["public"]}],
            "visibility": {}}


def site(name):
    with open(os.path.join(m.CADDY_APPS_DIR, f"{name}.caddy"), encoding="utf-8") as f:
        return f.read()


def block(body, opener):
    """Die Zeilen eines handle-Blocks, ab seiner Kopfzeile bis zur schliessenden."""
    lines = body.splitlines()
    for i, ln in enumerate(lines):
        if ln.strip() == opener:
            out = []
            for x in lines[i + 1:]:
                if x == "\t}":
                    return out
                out.append(x.strip())
    return None


def route_for(body, path):
    """Welcher handle-Block bekommt `path`? Caddys Regel fuer Bloecke mit
    einem Pfad-Matcher: der laengste Matcher zuerst; `x*` ist ein Praefix,
    alles andere ein exakter Pfad. (Bloecke mit benannten Matchern --
    @preflight -- greifen nur bei OPTIONS und sind hier nicht gemeint.)"""
    cands = []
    for ln in body.splitlines():
        if ln.startswith("	handle /") and ln.endswith(" {"):
            mt = ln.strip()[len("handle "):-2]
            hit = path.startswith(mt[:-1]) if mt.endswith("*") else path == mt
            if hit:
                cands.append((len(mt), ln.strip()))
    return max(cands)[1] if cands else "handle {"


tid = m.ensure_default_tenant()
OTHER = "t-other00000"
reg = m.load_registry()
reg["instances"]["hbvp-portal"] = record("hbvp-portal", tid, "hbsha", port=8101, version="2.4.0")
reg["instances"]["hbvp-portal-test"] = record("hbvp-portal-test", tid, "hbsha", "test", 8102, "2.5.0")
reg["instances"]["fremd"] = record("fremd", OTHER, "crm", port=8103)
m.save_registry(reg)
for n in reg["instances"]:
    m.write_app_caddy(n, m.load_registry()["instances"][n])

print("")
print("1. Ohne `vorgaenge` im Mandanten")
body = site("hbvp-portal-test")
fb = block(body, "handle /platform/feedback {")
ok("die Site reserviert /platform/feedback*", fb is not None, body)
ok("… und antwortet dort 404", fb and "respond 404" in fb, fb)
st = block(body, "handle /platform/* {")
ok("die Site reserviert /platform/* statisch",
   st and "root * /etc/caddy/static" in st and "file_server" in st, st)
ok("/platform/* streicht die Identitaetszeilen",
   st and all(f"request_header -{h}" in st for h in m.IDENTITY_HEADERS), st)
ok("die Bloecke stehen vor jeder App-Route",
   body.index("handle /platform/feedback {") < body.index("\thandle {")
   and body.index("handle /platform/* {") < body.index("handle /display* {"))
ok("die Herkunftszeilen werden auf Site-Ebene gestrichen",
   all(f"\trequest_header -{h}" in body.splitlines() for h in m.SOURCE_HEADERS))
ok("die Site gilt NICHT als veraltet fuer die Identitaetszeilen-Migration",
   not m._site_identity_stale(body))
# Gemessen auf oaap-test (16.10.): /platform/feedback* nahm auch das
# Script selbst und antwortete 404. Welcher Block welche URL bekommt:
for path, want in (("/platform/feedback.js", "handle /platform/* {"),
                   ("/platform/theme.css", "handle /platform/* {"),
                   ("/platform/feedback", "handle /platform/feedback {"),
                   ("/platform/feedback/session", "handle /platform/feedback/* {"),
                   ("/platform/feedbackx", "handle /platform/* {"),
                   ("/display/1", "handle /display* {")):
    got = route_for(body, path)
    ok(f"{path} -> {want}", got == want, got)
ok("beide Feedback-Bloecke sind gleich",
   block(body, "handle /platform/feedback {") == block(body, "handle /platform/feedback/* {"))

print("")
print("2. Mit `vorgaenge` im Mandanten (Produktion und Test)")
reg = m.load_registry()
reg["instances"]["vorgaenge"] = record("vorgaenge", tid, "vorgaenge", port=8110,
                                       routes=[{"path": "/", "roles": ["user"]}])
reg["instances"]["vorgaenge-test"] = record("vorgaenge-test", tid, "vorgaenge", "test", 8111,
                                            routes=[{"path": "/", "roles": ["user"]}])
m.save_registry(reg)
del RELOADS[:]
m.refresh_generated_sites()
body = site("hbvp-portal-test")
fb = block(body, "handle /platform/feedback {")
ok("eine Test-App meldet in den Test-Eingang",
   fb and "reverse_proxy oaap-app-vorgaenge-test:8000 {" in fb, fb)
ok("… mit Anmeldung, Mandant und Instanz",
   fb and any(x.startswith("uri /verify?roles=admin,user&tenant=" + tid + "&instance=hbvp-portal-test")
              for x in fb), fb)
ok("… die Identitaet kommt aus copy_headers", fb and m._COPY_IDENTITY in fb, fb)
ok("… der Pfad bleibt, wie er ist (sonst waere er das Ruecksprungziel der Anmeldung)",
   fb and not any(x.startswith("uri strip_prefix") for x in fb), fb)
want = {"header_up X-OAAP-Source-Instance hbvp-portal-test",
        "header_up X-OAAP-Source-App hbsha",
        "header_up X-OAAP-Source-Version 2.5.0",
        f"header_up X-OAAP-Source-Tenant {tid}"}
ok("… und das Gateway setzt die Herkunft", fb and want <= set(fb), fb)
ok("die Produktions-App meldet in den Produktions-Eingang",
   "reverse_proxy oaap-app-vorgaenge:8000 {" in (block(site("hbvp-portal"), "handle /platform/feedback {") or []))
ok("ein anderer Mandant bekommt NICHT dieses `vorgaenge` (404)",
   "respond 404" in (block(site("fremd"), "handle /platform/feedback {") or []))

print("")
print("3. Das Ziel folgt der Registry")
reg = m.load_registry()
reg["instances"].pop("vorgaenge-test")
m.save_registry(reg)
done = m.refresh_cases_sites()
ok("Test-Eingang entfernt: die Test-App faellt auf Produktion zurueck",
   "reverse_proxy oaap-app-vorgaenge:8000 {" in (block(site("hbvp-portal-test"), "handle /platform/feedback {") or []))
ok("… und nur die betroffenen Sites wurden neu geschrieben",
   "hbvp-portal-test" in done and "fremd" not in done, done)
ok("ein zweiter Lauf schreibt nichts", m.refresh_cases_sites() == [])
reg = m.load_registry()
reg["instances"]["hbvp-portal"]["version"] = "2.4.1"
m.save_registry(reg)
ok("eine neue Version der Quelle wird nachgezogen",
   m.refresh_cases_sites() == ["hbvp-portal"]
   and "header_up X-OAAP-Source-Version 2.4.1" in site("hbvp-portal"))
reg = m.load_registry()
reg["instances"]["vorgaenge"]["rehearsal"] = {"of": "x", "expires": "2099-01-01T00:00:00Z"}
m.save_registry(reg)
m.refresh_cases_sites()
ok("eine Generalprobe nimmt keine Meldungen an",
   "respond 404" in (block(site("hbvp-portal"), "handle /platform/feedback {") or []))
reg = m.load_registry()
reg["instances"]["vorgaenge"].pop("rehearsal")
m.save_registry(reg)
m.refresh_cases_sites()
# Gemessen auf oaapx01 (16.10.): der Mandant hatte nur einen TEST-Eingang,
# und die Produktions-App zeigte darauf. Echte Meldungen gehoeren nicht
# in eine Testinstanz.
reg = m.load_registry()
reg["instances"]["vorgaenge"]["channel"] = "test"
m.save_registry(reg)
m.refresh_cases_sites()
ok("nur ein Test-Eingang: die Produktions-App meldet NICHT dorthin (404)",
   "respond 404" in (block(site("hbvp-portal"), "handle /platform/feedback {") or []))
ok("… die Test-App schon",
   "reverse_proxy oaap-app-vorgaenge:8000 {" in (block(site("hbvp-portal-test"), "handle /platform/feedback {") or []))
reg = m.load_registry()
reg["instances"]["vorgaenge"]["channel"] = "production"
m.save_registry(reg)
m.refresh_cases_sites()
# Eine Generalprobe als QUELLE: sie bedient keinen oeffentlichen Block
# (RFC-0030 2.15.2) -- auch die beiden /platform-Bloecke nicht.
lazy = "\n".join(m.site_body(reg["instances"]["fremd"]["routes"], "oaap-app-fremd",
                             8000, None, "fremd", login_only=True))
ok("Generalprobe: /platform/* verlangt eine Anmeldung",
   any(x.startswith("uri /verify?roles=") for x in (block(lazy, "handle /platform/* {") or [])), lazy)
ok("Generalprobe: auch der 404-Block verlangt eine Anmeldung",
   any(x.startswith("uri /verify?roles=") for x in (block(lazy, "handle /platform/feedback {") or [])), lazy)

print("")
print("4. Das Manifest")
base = {"oaap_manifest": "0.6",
        "app": {"id": "probe", "version": "1.0.0", "name": "Probe", "type": "native"},
        "services": {"web": {"build": ".", "port": 8000}},
        "health": {"path": "/healthz"}}


def errs(path):
    """Die Fehlerzeilen der Pruefung ([] wenn sie durchgeht)."""
    out, err = _io.StringIO(), _io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            m.validate_manifest(dict(base, routes=[{"path": path, "roles": ["user"]}]))
    except SystemExit:
        pass
    return [ln for ln in (out.getvalue() + err.getvalue()).splitlines()
            if ln.strip().startswith("- ")]


ok("/platform/x abgelehnt", any("reserved" in e for e in errs("/platform/x")), errs("/platform/x"))
ok("/platform abgelehnt", any("reserved" in e for e in errs("/platform")))
ok("/platform/ abgelehnt", any("reserved" in e for e in errs("/platform/")))
ok("/auth/x abgelehnt", any("reserved" in e for e in errs("/auth/x")))
ok("/authors erlaubt", not any("reserved" in e for e in errs("/authors")), errs("/authors"))
ok("/platformx erlaubt", not any("reserved" in e for e in errs("/platformx")))
ok("/ erlaubt", not errs("/"), errs("/"))

print("")
print("5. Die Migration")
# eine Site, wie sie vor 0.1.199 auf der Platte lag: ohne Marker
old = site("fremd").splitlines()
old = [ln for ln in old if m.CASES_MARKER not in ln]
with open(os.path.join(m.CADDY_APPS_DIR, "fremd.caddy"), "w", encoding="utf-8") as f:
    f.write("\n".join(old) + "\n")
del RELOADS[:]
out = _io.StringIO()
with contextlib.redirect_stdout(out):
    m.cmd_migrate_platform_routes(None)
ok("die Migration schreibt die alte Site neu und laedt das Gateway",
   m.CASES_MARKER in site("fremd") and RELOADS, out.getvalue())
out = _io.StringIO()
del RELOADS[:]
with contextlib.redirect_stdout(out):
    m.cmd_migrate_platform_routes(None)
ok("ein zweiter Lauf schweigt und laedt nicht", out.getvalue() == "" and not RELOADS,
   out.getvalue())

print("")
print("6. Eigene Namen gehen ueber denselben Koerper")
reg = m.load_registry()
reg["instances"]["hbvp-portal-test"]["address"] = "hbvp-test.example.org"
m.save_registry(reg)
with open(m.EXTERNAL_FILE, "w", encoding="utf-8") as f:
    json.dump({"host": "node.example.org"}, f)
m.refresh_generated_sites()
ext = "".join(open(os.path.join(m.CADDY_APPS_DIR, x), encoding="utf-8").read()
              for x in ("external.caddy", "instance-addresses.caddy")
              if os.path.exists(os.path.join(m.CADDY_APPS_DIR, x)))
ok("die Site des eigenen Namens reserviert /platform/feedback*",
   "hbvp-test.example.org" in ext and "header_up X-OAAP-Source-Instance hbvp-portal-test" in ext,
   ext[:400])

print("")
print("ALLE PASS" if not fails else f"{fails} FAIL")
sys.exit(1 if fails else 0)
