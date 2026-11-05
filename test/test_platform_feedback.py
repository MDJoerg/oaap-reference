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
8. RFC-0069 Stufe 2 (0.1.222): die Site eines `vorgaenge` traegt die Tuer
   des Knotens, /platform/cases/node, die nur den Schluessel von
   platform:<diese Instanz> annimmt; keine andere Site traegt sie. Der
   Schluessel je Posteingang wird einmal gepraegt und erneuert, und die
   Vorgaenge gehen in jeden Produktions-Eingang, einen je Mandant.
9. RFC-0069 Stufe 3 (0.1.223): die Tuer, die eine Instanz liest --
   /platform/care.json auf einem nie veroeffentlichten Port, die Datei
   neben den Site-Dateien; jede Instanz erfaehrt OAAP_CARE_URL, die
   Migration traegt es in jede instance.env.
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
print("7. Die Tuer fuer das Backend einer App (RFC-0061 §3.2)")
m.refresh_cases_sites()
body = site("hbvp-portal-test")
sb = block(body, "handle /platform/cases/submit {")
ok("die Site reserviert /platform/cases/submit", sb is not None, body[:300])
ok("/platform/cases/submit landet in genau diesem Block",
   route_for(body, "/platform/cases/submit") == "handle /platform/cases/submit {")
ok("… gefragt wird der Schluessel DIESER Instanz, keine Rolle",
   sb and f"uri /verify?roles=&tenant={tid}&instance=oaap.cases.submit:hbvp-portal-test" in sb, sb)
ok("… und das Gateway setzt die Herkunft",
   sb and "header_up X-OAAP-Source-Instance hbvp-portal-test" in sb
   and "reverse_proxy oaap-app-vorgaenge:8000 {" in sb, sb)
other = block(site("hbvp-portal"), "handle /platform/cases/submit {") or []
ok("die Nachbarinstanz fragt nach IHREM Schluessel",
   any(x.startswith("uri ") and x.endswith("&instance=oaap.cases.submit:hbvp-portal")
       for x in other), other)
ok("ohne vorgaenge im Mandanten: 404",
   "respond 404" in (block(site("fremd"), "handle /platform/cases/submit {") or []))
man =dict(base, routes=[{"path": "/", "roles": ["user"]}])


def man_errs(extra):
    out, err = _io.StringIO(), _io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            m.validate_manifest(dict(man, **extra))
    except SystemExit:
        pass
    return [ln for ln in (out.getvalue() + err.getvalue()).splitlines()
            if ln.strip().startswith("- ")]


ok("Manifest: cases.submit 'yes' wird abgelehnt",
   any("cases.submit" in e for e in man_errs({"cases": {"submit": "yes"}})))
ok("Manifest: cases als Liste wird abgelehnt",
   any("cases:" in e for e in man_errs({"cases": ["submit"]})))
ok("Manifest: cases.submit true geht durch", not man_errs({"cases": {"submit": True}}))
ok("nur ein woertliches true verlangt den Schluessel",
   [m.cases_submit_wanted(x) for x in ({"cases": {"submit": True}}, {"cases": {"submit": "yes"}},
                                        {"cases": []}, {})] == [True, False, False, False])
ok("OAAP_CASES_KEY und _URL sind Plattformvariablen",
   {"OAAP_CASES_KEY", "OAAP_CASES_URL"} <= m.RESERVED_ENV)

print("")
print("8. Die Tuer des Knotens auf der Site des Posteingangs (RFC-0069 Stufe 2)")
reg = m.load_registry()
m.write_app_caddy("vorgaenge", reg["instances"]["vorgaenge"])
m.refresh_cases_sites()
body = site("vorgaenge")
nb = block(body, "handle /platform/cases/node {")
ok("die Site des vorgaenge reserviert /platform/cases/node", nb is not None, body[:400])
ok("/platform/cases/node landet in genau diesem Block",
   route_for(body, "/platform/cases/node") == "handle /platform/cases/node {")
ok("… gefragt wird der Schluessel des Knotens FUER DIESEN Posteingang, keine Rolle",
   nb and f"uri /verify?roles=&tenant={tid}&instance=oaap.cases.node:vorgaenge" in nb, nb)
ok("… die Herkunft ist der Posteingang selbst, das Ziel er selbst",
   nb and "header_up X-OAAP-Source-Instance vorgaenge" in nb
   and "header_up X-OAAP-Source-App vorgaenge" in nb
   and "reverse_proxy oaap-app-vorgaenge:8000 {" in nb, nb)
ok("der Block steht vor /platform/* und vor der App-Route",
   body.index("handle /platform/cases/node {") < body.index("handle /platform/* {")
   and body.index("handle /platform/cases/node {") < body.index("\thandle {"))
ok("eine App-Site traegt die Tuer NICHT",
   block(site("hbvp-portal"), "handle /platform/cases/node {") is None
   and block(site("fremd"), "handle /platform/cases/node {") is None)
ok("der Marker zaehlt das Format 5, damit Sites auf der Platte nachgezogen werden",
   m.CASES_BLOCK_FORMAT == 5 and f"{m.CASES_MARKER}5 " in body)
ok("die Migration (Abschnitt 5) findet nichts mehr zu tun", m._cases_sites_behind() == [])

print("")
print("   der Schluessel je Posteingang")
MINTED = []


def fake_identity_exec(script, env=None):
    MINTED.append(dict(env or {}))
    n = len(MINTED)
    return json.dumps({"secret": f"oaapk_id{n}_s{n}", "id": f"id{n}",
                       "expires": "2027-11-05T10:00:00"}) + "\n"


m._identity_exec = fake_identity_exec
k1 = m._node_case_key("vorgaenge", tid)
ok("erster Gebrauch: gepraegt fuer platform:vorgaenge im Mandanten, Scope oaap.cases.node:vorgaenge",
   k1 == "oaapk_id1_s1" and MINTED[-1]["OAAP_T_NAME"] == "platform:vorgaenge"
   and MINTED[-1]["OAAP_T_TENANT"] == tid and MINTED[-1]["OAAP_T_SCOPE"] == "oaap.cases.node:vorgaenge"
   and MINTED[-1]["OAAP_T_OLD"] == "", MINTED)
ok("zweiter Gebrauch: derselbe Schluessel, nichts gepraegt",
   m._node_case_key("vorgaenge", tid) == k1 and len(MINTED) == 1)
with open(m.NODE_CASE_KEYS, encoding="utf-8") as f:
    keys = json.load(f)
ok("die Datei traegt Schluessel, Kennung, Ablauf, Prinzipal",
   keys["vorgaenge"]["id"] == "id1" and keys["vorgaenge"]["expires"].startswith("2027")
   and keys["vorgaenge"]["principal"] == "platform:vorgaenge", keys)
if os.name == "posix":
    ok("… nur fuer root lesbar", oct(os.stat(m.NODE_CASE_KEYS).st_mode & 0o777) == "0o600")
import datetime as _dt  # noqa: E402
late = _dt.datetime(2027, 10, 20, tzinfo=_dt.timezone.utc)
k2 = m._node_case_key("vorgaenge", tid, now=late)
ok("laeuft binnen 30 Tagen ab: neu gepraegt, der alte widerrufen",
   k2 == "oaapk_id2_s2" and MINTED[-1]["OAAP_T_OLD"] == "id1" and len(MINTED) == 2, MINTED)
m._node_case_key_forget("vorgaenge")
with open(m.NODE_CASE_KEYS, encoding="utf-8") as f:
    ok("ein entfernter Posteingang nimmt seinen Schluessel mit", "vorgaenge" not in json.load(f))

print("")
print("   die Vorgaenge gehen in jeden Produktions-Eingang")
reg = m.load_registry()
reg["instances"]["vorgaenge-test"] = record("vorgaenge-test", tid, "vorgaenge", "test", 8111)
reg["instances"]["fremd-vorgaenge"] = record("fremd-vorgaenge", OTHER, "vorgaenge", port=8112)
reg["instances"]["fremd-vorgaenge-b"] = record("fremd-vorgaenge-b", OTHER, "vorgaenge", port=8113)
reg["instances"]["probe-vorgaenge"] = dict(record("probe-vorgaenge", tid, "vorgaenge", port=8114),
                                           rehearsal={"of": "vorgaenge", "expires": "2099-01-01T00:00:00Z"})
reg["instances"]["schlaf-vorgaenge"] = dict(record("schlaf-vorgaenge", "t-sleep000000", "vorgaenge", port=8115),
                                            state="standby")
m.save_registry(reg)
inb = m._care_inboxes()
ok("Produktion, keine Generalprobe, nicht schlafend, einer je Mandant (der erste im Namen)",
   sorted(inb) == ["fremd-vorgaenge", "vorgaenge"], sorted(inb))

POSTS = []


def fake_post(name, inst, doc):
    POSTS.append((name, dict(doc)))
    if name == "fremd-vorgaenge" and FAIL_FREMD[0]:
        return 503, {"error": "schlaeft"}
    if doc["op"] == "open":
        return 201, {"ok": True, "number": f"V-{name}-{len(POSTS)}"}
    return 200, {"ok": True, "number": doc["number"]}


FAIL_FREMD = [True]
m._node_case_post = fake_post
import host_care as hc  # noqa: E402
plan = {"source": "kernel", "kernel": "6.12.115", "origin": "security", "urgency": "care",
        "reason": "r", "announced": "2026-11-06T10:00:00Z", "at": "2026-11-08T03:15:00Z",
        "at_local": "2026-11-08 04:15 CET", "held_by": [], "overridden": [], "by": "", "state": "planned"}
errs = _io.StringIO()
with contextlib.redirect_stderr(errs):
    m._care_cases([("reboot.planned", "kernel", "x")], plan, None)
ok("angekuendigt: ein open je Eingang; der Eingang, der nicht antwortet, fehlt im Plan und steht im Journal",
   [p[0] for p in POSTS] == ["fremd-vorgaenge", "vorgaenge"] and all(p[1]["op"] == "open" for p in POSTS)
   and plan["cases"] == {"vorgaenge": "V-vorgaenge-2"} and "fremd-vorgaenge: open answered 503" in errs.getvalue(),
   (POSTS, plan.get("cases"), errs.getvalue()))
ok("das open traegt Bezug, Knoten, Betreff, Text",
   POSTS[1][1]["ref"] == "kernel:6.12.115:2026-11-06T10:00:00Z" and POSTS[1][1]["node"]
   and POSTS[1][1]["title"].startswith("Neustart des Knotens") and "startet am" in POSTS[1][1]["body"], POSTS[1])
FAIL_FREMD[0] = False
del POSTS[:]
moved = dict(plan, at_local="2026-11-14 04:15 CET",
             held_by=[{"instance": "joerg-desk", "until": "2026-11-13", "reason": "Schulung"}])
m._care_cases([("reboot.moved", "kernel", "x")], moved, plan)
ok("verschoben: der fehlende Eingang bekommt sein open nachgeholt, dann die Notiz; der andere nur die Notiz",
   [(p[0], p[1]["op"]) for p in POSTS] == [("fremd-vorgaenge", "open"), ("fremd-vorgaenge", "note"),
                                           ("vorgaenge", "note")]
   and POSTS[2][1]["number"] == "V-vorgaenge-2" and "Verschoben auf 2026-11-14" in POSTS[2][1]["text"]
   and moved["cases"] == {"vorgaenge": "V-vorgaenge-2", "fremd-vorgaenge": "V-fremd-vorgaenge-1"}, (POSTS, moved["cases"]))
del POSTS[:]
m._care_cases([("reboot.done", "kernel", "rebooted after x; running 6.12.115")], None, moved)
ok("geschlossen aus dem Plan, der eben zu Ende ging (der neue ist None): ein close je Eingang mit seiner Nummer",
   sorted((p[0], p[1]["op"], p[1]["number"]) for p in POSTS)
   == [("fremd-vorgaenge", "close", "V-fremd-vorgaenge-1"), ("vorgaenge", "close", "V-vorgaenge-2")]
   and "läuft wieder" in POSTS[0][1]["text"], POSTS)
del POSTS[:]
m._care_cases([("instance.protected", "x", "y")], plan, None)
ok("ein Ereignis ohne Vorgangsbelang: kein Aufruf", POSTS == [])
del POSTS[:]
m._care_announce([("reboot.cancelled", "kernel", "x cancelled")], stored=moved)
ok("_care_announce mit stored: das Mandanten-Log UND die Eingaenge (close)",
   [p[1]["op"] for p in POSTS] == ["close", "close"], POSTS)

print("")
print("9. Die Tuer, die eine Instanz liest (RFC-0069 Stufe 3)")
import host_care as _hc  # noqa: E402
carep = os.path.join(m.CADDY_APPS_DIR, "_internal-care.caddy")
if os.path.exists(carep):
    os.remove(carep)
ok("der Listener wird geschrieben", m.write_internal_care_caddy() is True and os.path.exists(carep))
cbody = open(carep, encoding="utf-8").read()
ok("Port 8097 (nie veroeffentlicht), file_server aus dem Site-Verzeichnis, die Door-Datei, kein Cache",
   ":8097 {" in cbody and "root * /etc/caddy/apps" in cbody and f"rewrite * /{_hc.DOOR_FILE}" in cbody
   and "file_server" in cbody and "Cache-Control no-store" in cbody and "respond 404" in cbody, cbody)
ok("ein zweiter Lauf aendert nichts", m.write_internal_care_caddy() is False)
ok("die Adresse, die jede Instanz erfaehrt",
   m.care_url() == "http://oaap-gateway-1:8097/platform/care.json" and "OAAP_CARE_URL" in m.RESERVED_ENV)
reg = m.load_registry()
for n, inst in reg["instances"].items():
    e = m.load_env(n, inst)
    e.pop("OAAP_CARE_URL", None)
    m.save_env(n, e, inst)
os.remove(carep)
del RELOADS[:]
out = _io.StringIO()
with contextlib.redirect_stdout(out):
    m.cmd_migrate_care_url(None)
envs = {n: m.load_env(n, i).get("OAAP_CARE_URL") for n, i in reg["instances"].items()}
ok("die Migration schreibt den Listener, laedt das Gateway einmal und traegt OAAP_CARE_URL in jede instance.env",
   os.path.exists(carep) and RELOADS == [1] and set(envs.values()) == {m.care_url()}
   and f"told to {len(envs)} instance(s)" in out.getvalue(), (envs, out.getvalue()))
del RELOADS[:]
out = _io.StringIO()
with contextlib.redirect_stdout(out):
    m.cmd_migrate_care_url(None)
ok("ein zweiter Lauf schweigt und laedt nicht", out.getvalue() == "" and not RELOADS, out.getvalue())
facts = {"measured": "2026-11-06T10:00:00Z", "plan": {"at": "2026-11-08T03:15:00Z", "at_local": "x",
                                                    "urgency": "care", "held_by": [], "state": "planned",
                                                    "reason": "secret", "kernel": "6.12.120"}}
_hc.write_door(facts, m.CADDY_APPS_DIR)
door = json.load(open(os.path.join(m.CADDY_APPS_DIR, _hc.DOOR_FILE), encoding="utf-8"))
ok("die Door-Datei liegt neben den Sites und traegt weder Grund noch Kernel",
   door["reboot_at"] == "2026-11-08T03:15:00Z" and "secret" not in json.dumps(door)
   and "6.12" not in json.dumps(door), door)

print("")
print("ALLE PASS" if not fails else f"{fails} FAIL")
sys.exit(1 if fails else 0)
