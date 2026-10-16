#!/usr/bin/env python3
"""RFC-0059 Stufe 1: Standby, Aufwecken, Wartungsmodus.

Gemessen am 15.10. auf oaap-test: ein von Hand gestoppter Container wird
von `oaap status` als DEGRADED gezaehlt, und `config set` startete ihn
still wieder. Beides folgte daraus, dass der Zustand nirgends stand. Hier
steht er am Instanzeintrag -- und diese Pruefung haelt die Stellen fest,
die ihn lesen muessen:

1. die EINE Stelle, die Container anlegt (`recreate_instance_containers`),
   legt eine schlafende Instanz an, ohne sie zu starten;
2. der EINE Seitenkoerper des Gateways zeigt statt der App die Weckseite
   (hinter derselben Anmeldung; eine oeffentliche Route weckt nie);
3. in Wartung fragt JEDE Route identity -- auch die oeffentliche;
4. identity antwortet in Wartung 503 an alle ausser Administratoren, auch an
   Anonyme (nicht Anmeldeformular) und an Maschinen (JSON);
5. die Weckseite der Plattform verlangt den Schluessel des Gateways;
6. Standby: erst die Weckseite, dann das Anhalten; Wecken: erst die App
   antwortet, dann gehoert ihr die Adresse wieder.

Aufruf: python3 test/test_standby.py
"""
import contextlib
import importlib
import io
import json
import os
import subprocess as _sp
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-standby-test-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, os.path.join(HERE, "..", "platform"))

import appctl as m                                            # noqa: E402

os.makedirs(m.CADDY_APPS_DIR, exist_ok=True)
fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:600]}")


CALLS = []          # was die Plattform getan hat, in Reihenfolge
m.reload_gateway = lambda: CALLS.append("gateway-reload")
m.refresh_generated_sites = lambda: None
m.state_view_write = lambda reg=None: True
m.audit_tenant = lambda *a, **k: CALLS.append("audit:" + a[0])
m.deployment_in_flight = lambda name, own_rid="": False
m._answers = lambda inst, seconds: True


def fake_run(argv, **kw):
    CALLS.append(" ".join(argv[:3]))
    return _sp.CompletedProcess(argv, 0, "", "")


m.subprocess.run = fake_run

tid = m.ensure_default_tenant()
ROUTES = [{"path": "/", "roles": ["keyuser"]},
          {"path": "/hook", "roles": ["public"]}]


def record(**over):
    base = {
        "app_id": "crm", "app_name": "CRM", "version": "1.4.0",
        "channel": "production", "port": 8101, "container": "oaap-app-crm",
        "image": "oaap-app/crm:1.4.0", "svc_port": 8000,
        "services": [{"service": "", "container": "oaap-app-crm",
                      "image": "oaap-app/crm:1.4.0", "build": "", "port": 8000}],
        "routes": [dict(r) for r in ROUTES], "storage": [], "config": [],
        "roles": ["keyuser"], "visibility": {}, "tenant": tid,
        "id": "aaaaaaaaaaaa", "name": "crm",
    }
    base.update(over)
    return base


reg = m.load_registry()
reg["instances"]["crm"] = record()
m.save_registry(reg)


def site(name="crm"):
    m.write_app_caddy(name, m.load_registry()["instances"][name])
    with open(os.path.join(m.CADDY_APPS_DIR, f"{name}.caddy"),
              encoding="utf-8") as f:
        return f.read()


print("")
print("Eine laufende Instanz bleibt, wie sie war")
run_site = site()
ok("sie geht zur App", "reverse_proxy oaap-app-crm:8000" in run_site, run_site)
ok("und keine Weckseite", "/wake/" not in run_site)

print("")
print("Ein Eintrag mit state: standby -- die Seite zeigt die Weckseite")
reg = m.load_registry()
reg["instances"]["crm"]["state"] = "standby"
m.save_registry(reg)
sl = site()
ok("kein Weg fuehrt mehr zur App", "oaap-app-crm:8000" not in sl, sl)
ok("jede Route geht zur Weckseite der Plattform",
   sl.count("rewrite * /wake/crm") == 2 and sl.count("reverse_proxy portal:8000") >= 2, sl)
ok("hinter der Anmeldung der Route weckt sie (auto)",
   "X-OAAP-Wake auto" in sl)
ok("die oeffentliche Route weckt NIE (D4)",
   sl.count("X-OAAP-Wake no") == 1, sl)
ok("der Schluessel des Gateways steht in der Seite",
   m.wake_key() in sl and len(m.wake_key()) >= 32)
ok("die Anmeldung der Route bleibt davor",
   "uri /verify?roles=keyuser" in sl)

print("")
print("Wartung -- JEDE Route fragt identity, auch die oeffentliche")
reg = m.load_registry()
reg["instances"]["crm"].pop("state", None)
reg["instances"]["crm"]["maintenance"] = {"since": "x", "by": "joerg",
                                          "message": "Update laeuft"}
m.save_registry(reg)
mt = site()
handles = [b for b in mt.split("\thandle") if "reverse_proxy oaap-app-crm" in b]
ok("die App ist erreichbar -- sie laeuft", len(handles) == 2, mt)
ok("beide Routen verlangen /verify", all("uri /verify" in b for b in handles), mt)
ok("keine haengt mehr an der Drossel statt an identity",
   "uri /throttle" not in mt, mt)

print("")
print("recreate_instance_containers: schlafend heisst anlegen, nicht starten")
m.ensure_app_network = lambda name: "net-" + name
m.connect_gateway = lambda net: None
m.restore_links = lambda name: None
m.image_uid = lambda image: None
m.env_path = lambda name, inst=None: os.path.join(DATA, "x.env")
ARGV = []


def capture_run(argv, **kw):
    ARGV.append(list(argv))
    return _sp.CompletedProcess(argv, 0, "", "")


real_run = m.run
m.run = capture_run
reg = m.load_registry()
inst = reg["instances"]["crm"]
inst.pop("maintenance", None)
m.save_registry(reg)
m.recreate_instance_containers("crm", m.instance_services(inst), [])
ok("laufend: docker run -d",
   any(a[:3] == ["docker", "run", "-d"] for a in ARGV), ARGV)
del ARGV[:]
reg = m.load_registry()
reg["instances"]["crm"]["state"] = "standby"
m.save_registry(reg)
m.recreate_instance_containers("crm", m.instance_services(inst), [])
ok("schlafend: docker create, KEIN run",
   any(a[:2] == ["docker", "create"] for a in ARGV)
   and not any(a[:3] == ["docker", "run", "-d"] for a in ARGV), ARGV)
m.run = real_run
reg = m.load_registry()
reg["instances"]["crm"].pop("state", None)
m.save_registry(reg)

print("")
print("standby: erst die Weckseite, dann das Anhalten")
del CALLS[:]
with contextlib.redirect_stdout(io.StringIO()):
    msg = m.standby_instance("crm", m.load_registry()["instances"]["crm"], who="joerg")
cur = m.load_registry()["instances"]["crm"]
stop_i = next((i for i, c in enumerate(CALLS) if c.startswith("docker stop")), -1)
ok("Zustand steht im Eintrag", cur.get("state") == "standby"
   and cur["standby"]["by"] == "joerg", cur)
ok("das Gateway wurde VOR dem Anhalten neu geladen",
   "gateway-reload" in CALLS and CALLS.index("gateway-reload") < stop_i, CALLS)
ok("der Container wurde angehalten", stop_i >= 0, CALLS)
modes = json.load(open(m.MODES_FILE, encoding="utf-8"))
ok("die Statusdatei nennt den schlafenden Container",
   modes["standby_containers"] == ["oaap-app-crm"], modes)
ok("ein zweites Mal ist eine Meldung, kein Fehler",
   "already" in m.standby_instance("crm", cur))
ok("ein Neustart (restart) laesst ihn schlafen: der Eintrag haelt den Zustand",
   m.load_registry()["instances"]["crm"].get("state") == "standby")

print("")
print("wake: die Adresse gehoert der App erst, wenn sie antwortet")
del CALLS[:]
with contextlib.redirect_stdout(io.StringIO()):
    msg = m.wake_instance("crm", m.load_registry()["instances"]["crm"], who="tn-01")
cur = m.load_registry()["instances"]["crm"]
ok("der Zustand ist weg", "state" not in cur and "standby" not in cur, cur)
ok("docker start lief", any(c.startswith("docker start") for c in CALLS), CALLS)
ok("die Statusdatei nennt keinen schlafenden Container mehr",
   json.load(open(m.MODES_FILE, encoding="utf-8"))["standby_containers"] == [])
ok("die Seite geht wieder zur App", "reverse_proxy oaap-app-crm:8000" in site())
ok("wecken ohne Standby ist eine Meldung",
   "not in standby" in m.wake_instance("crm", cur))

print("")
print("maintenance")
with contextlib.redirect_stdout(io.StringIO()):
    m.maintenance_set("crm", cur, True, "Wir sind um 14 Uhr wieder da.", who="joerg")
modes = json.load(open(m.MODES_FILE, encoding="utf-8"))
ok("Text und Mandant stehen in der Statusdatei, die identity liest",
   modes["instances"]["crm"]["maintenance"]["message"].startswith("Wir sind um 14")
   and modes["instances"]["crm"]["tenant"] == tid, modes)

print("")
print("identity: 503 fuer alle ausser Administratoren")
try:
    from werkzeug.security import generate_password_hash
    import flask  # noqa: F401
    have_flask = True
except ImportError:
    have_flask = False
if not have_flask:
    print("SKIP  flask/werkzeug fehlen")
else:
    IDENT = os.path.join(HERE, "..", "platform", "services", "identity")
    d = tempfile.mkdtemp(prefix="oaap-standby-id-")
    PW = "geheim123"

    def u(name, roles, tenant=""):
        return {"username": name, "display_name": "", "kind": "human",
                "password_hash": generate_password_hash(PW), "roles": roles,
                "groups": [], "tenant": tenant, "active": True,
                "session_epoch": 0}
    with open(os.path.join(d, "users.json"), "w", encoding="utf-8") as f:
        json.dump([u("root", ["server_admin"]), u("ta", ["tenant_admin", "user"]),
                   u("fremd", ["tenant_admin", "user"], tenant="other"),
                   u("anna", ["user", "keyuser"])], f)
    with open(os.path.join(d, "state.json"), "w", encoding="utf-8") as f:
        json.dump({"setup_done": True, "server_admin_migrated": True,
                   "support_migrated": True}, f)
    os.environ.update(SESSION_SECRET="s", SETUP_TOKEN="t", INTERNAL_API_KEY="k",
                      OAAP_IDENTITY_DATA_DIR=d, OAAP_MODES_FILE=m.MODES_FILE)
    sys.path.insert(0, IDENT)
    sys.modules.pop("app", None)
    idm = importlib.import_module("app")
    idm.USERS_FILE = os.path.join(d, "users.json")
    idm.STATE_FILE = os.path.join(d, "state.json")
    idm.MODES_FILE = m.MODES_FILE
    idm.TENANTS_FILE = os.path.join(m.APPS_DIR, "tenants.json")

    def verify(user=None, accept="", instance="crm", extra=""):
        c = idm.app.test_client()
        if user:
            c.post("/auth/login", data={"username": user, "password": PW})
        h = {"Accept": accept} if accept else {}
        return c.get(f"/verify?instance={instance}{extra}",
                     headers=h)

    r = verify()
    ok("anonym: 503 mit der Seite, nicht das Anmeldeformular",
       r.status_code == 503 and b"14 Uhr" in r.data
       and r.headers.get("Retry-After") == "300", (r.status_code, r.data[:120]))
    ok("ein Benutzer ohne Verwalterrolle: 503", verify("anna").status_code == 503)
    ok("ein Mandantenverwalter eines ANDEREN Mandanten: 503",
       verify("fremd").status_code == 503)
    ok("Maschine/JSON: 503 mit JSON-Koerper",
       (lambda r: r.status_code == 503 and r.is_json
        and r.get_json()["error"] == "maintenance")(verify(accept="application/json")))
    ok("der Serververwalter kommt durch", verify("root").status_code == 204)
    ok("der Mandantenverwalter des Mandanten kommt durch",
       verify("ta").status_code == 204)
    ok("eine ANDERE Instanz ist nicht betroffen",
       verify("anna", instance="andere").status_code == 204)
    ok("der Text wird maskiert, nicht eingesetzt",
       (lambda t: "<script>" not in t and "&lt;script&gt;" in t)(
           (lambda: (json.dump({"schema": "0.1", "instances": {"crm": {
               "state": "running", "tenant": tid,
               "maintenance": {"message": "<script>x</script>"}}},
               "standby_containers": []}, open(m.MODES_FILE, "w", encoding="utf-8")),
               verify().get_data(as_text=True))[1])()))

print("")
print("Weckseite der Plattform: nur mit dem Schluessel des Gateways")
PORTAL = os.path.join(HERE, "..", "platform", "services", "portal")
try:
    sys.path.insert(0, PORTAL)
    sys.modules.pop("app", None)
    pa = importlib.import_module("app")
except Exception as e:                                       # noqa: BLE001
    pa = None
    print(f"SKIP  Portal nicht ladbar: {e}")
if pa is not None:
    wk = os.path.join(DATA, "wake-key-copy")
    with open(wk, "w", encoding="utf-8") as f:
        f.write("sesam\n")
    pa.WAKE_KEY_FILE = wk
    regf = os.path.join(DATA, "portal-registry.json")
    with open(regf, "w", encoding="utf-8") as f:
        json.dump({"instances": {"crm": {"state": "standby"},
                                 "wach": {"app_id": "x"}}}, f)
    pa.REGISTRY = regf
    QUEUED = []
    pa._queue_with_id = lambda rid, name, payload, wait: QUEUED.append(
        (name, payload)) or None
    c = pa.app.test_client()
    ok("ohne Schluessel: 403", c.get("/wake/crm").status_code == 403)
    ok("mit falschem Schluessel: 403",
       c.post("/wake/crm", headers={"X-OAAP-Gateway-Key": "x",
                                    "X-OAAP-Wake": "auto"}).status_code == 403)
    H = {"X-OAAP-Gateway-Key": "sesam", "X-OAAP-Wake": "auto"}
    r = c.get("/wake/crm", headers=H)
    ok("die Seite: 503 mit Retry-After und deutschem Text",
       r.status_code == 503 and "Retry-After" in r.headers
       and "gestartet" in r.get_data(as_text=True), r.status_code)
    ok("GET weckt NICHT (Vorabruf, Suchmaschine, Link-Vorschau)", QUEUED == [])
    r = c.post("/wake/crm", headers=H)
    ok("POST mit Schluessel und auto: ein Weckauftrag",
       QUEUED == [("crm", {"action": "wake"})] and r.get_json() == {"asleep": True},
       (QUEUED, r.data))
    del QUEUED[:]
    c.post("/wake/crm", headers={**H, "X-OAAP-Wake": "no"})
    ok("oeffentliche Route (no): kein Auftrag", QUEUED == [])
    r = c.get("/wake/crm", headers={**H, "X-OAAP-Wake": "no"})
    ok("und die Seite sagt, dass es dort nicht geht",
       "ruht" in r.get_data(as_text=True))
    ok("eine wache Instanz: JSON asleep=false",
       c.get("/wake/wach", headers={**H, "Accept": "application/json"}
             ).get_json() == {"asleep": False})
    ok("eine unbekannte Instanz: 404", c.get("/wake/nix", headers=H).status_code == 404)

    # The page's own script, run with a stubbed browser: once the instance is
    # up, the address answers with the APP's page (HTML), not our JSON -- the
    # page must reload then, not poll forever (measured by Joerg, 2026-10-16).
    import shutil
    import subprocess as sp
    node = shutil.which("node") or shutil.which("nodejs")
    if node is None:
        print("SKIP  kein node: das Skript der Weckseite wurde nicht ausgefuehrt")
    else:
        page = c.get("/wake/crm", headers=H).get_data(as_text=True)
        script = page.split("<script>")[1].split("</script>")[0]
        harness = """
let reloads = 0, calls = 0;
global.location = {href: "http://x/", reload(){ reloads++; }};
const mode = process.argv[2];
global.fetch = (url, opt) => { calls++;
  if (opt && opt.method === "POST") return Promise.resolve({});
  const h = {get: () => mode === "html" ? "text/html" : "application/json"};
  return Promise.resolve({headers: h, json: async () => { if (mode === "html") throw new SyntaxError("Unexpected token <"); return {asleep: mode === "asleep"}; }}); };
global.setTimeout = (f, ms) => { setImmediate(f); return 0; };
""" + script.replace("setTimeout(", "setTimeout(") + """
setTimeout2 = null;
new Promise(r => setImmediate(() => setImmediate(() => setImmediate(() => r())))).then(() => {
  console.log(JSON.stringify({reloads: reloads, polled: calls > 1})); process.exit(0); });
"""
        def run_mode(mode):
            hf = os.path.join(DATA, "wakepage-harness.js")
            with open(hf, "w", encoding="utf-8") as f:
                f.write(harness)
            # Popen, not run: this test replaces subprocess.run with a fake
            pr = sp.Popen([node, hf, mode], stdout=sp.PIPE, stderr=sp.PIPE,
                          text=True, encoding="utf-8")
            out, err = pr.communicate(timeout=20)
            return (json.loads(out.strip().splitlines()[-1]) if out.strip()
                    else {"rc": pr.returncode, "err": err[-400:]})
        ok("Weckseite: antwortet die Adresse mit HTML (App ist da) -> neu laden",
           run_mode("html").get("reloads", 0) >= 1, run_mode("html"))
        ok("Weckseite: asleep=false -> neu laden",
           run_mode("awake").get("reloads", 0) >= 1, run_mode("awake"))
        ok("Weckseite: asleep=true -> weiter fragen, nicht laden",
           run_mode("asleep").get("reloads", 1) == 0, run_mode("asleep"))

    print("")
    print("Portal-Knoepfe (Stufe 2): dieselben Auftraege wie die CLI")
    ROLES = ["server_admin"]
    pa.caller_roles = lambda: ROLES
    pa.visible_instances = lambda: ["crm"]
    pa._deploy_now = lambda n: None
    SENT = []
    ANSWER = [{"ok": True, "message": "x"}]
    pa._queue_and_wait = lambda name, payload, wait: SENT.append(
        (name, payload, wait)) or ANSWER[0]
    r = c.post("/instances/crm/standby", data={"tab": "diagnose"})
    ok("Standby-Knopf: Auftrag 'standby' an den Host, Weiterleitung",
       SENT[-1][:2] == ("crm", {"action": "standby"}) and r.status_code == 303, SENT)
    c.post("/instances/crm/wake")
    ok("Wecken-Knopf: Auftrag 'wake', Wartezeit laenger als beim Standby",
       SENT[-1][:2] == ("crm", {"action": "wake"}) and SENT[-1][2] > SENT[-2][2], SENT)
    c.post("/instances/crm/maintenance", data={"op": "on", "message": " Zehn Minuten. "})
    ok("Wartung an: Auftrag mit on=True und dem Text, getrimmt",
       SENT[-1][1] == {"action": "maintenance", "on": True, "message": "Zehn Minuten."}, SENT[-1])
    c.post("/instances/crm/maintenance", data={"op": "off", "message": "bleibt weg"})
    ok("Wartung aus: on=False, kein Text",
       SENT[-1][1] == {"action": "maintenance", "on": False, "message": ""}, SENT[-1])
    n = len(SENT)
    ANSWER[0] = {"ok": False, "message": "kein Platz"}
    r = c.post("/instances/crm/wake")
    ok("Ablehnung des Hosts erscheint als Fehler auf der Seite",
       r.status_code == 303 and "err=" in r.headers["Location"]
       and "kein" in r.headers["Location"], r.headers.get("Location"))
    ANSWER[0] = {"ok": True}
    ROLES[:] = ["user"]
    ok("ohne Verwalterrolle: 403 und KEIN Auftrag",
       c.post("/instances/crm/standby").status_code == 403 and len(SENT) == n + 1)
    ROLES[:] = ["tenant_admin"]
    ok("tenant_admin einer fremden Instanz: 404 und kein Auftrag",
       c.post("/instances/wach/standby").status_code == 404 and len(SENT) == n + 1)
    ROLES[:] = ["server_admin"]
    pa._deploy_now = lambda n: {"x": 1}
    r = c.post("/instances/crm/standby")
    ok("laeuft ein Deployment: abgelehnt, kein Auftrag",
       "err=" in r.headers["Location"] and len(SENT) == n + 1)
    pa._deploy_now = lambda n: None
    mv = pa._mode_view({"state": "standby", "standby": {"since": "2026-10-16T10:00:00Z", "by": "joerg"},
                        "maintenance": {"message": "bald"}})
    ok("Ansicht: Standby und Wartung getrennt lesbar",
       mv["state"] == "standby" and mv["by"] == "joerg" and mv["maintenance"]
       and mv["m_message"] == "bald", mv)
    ok("Ansicht einer wachen Instanz: leer",
       pa._mode_view({})["state"] == "" and not pa._mode_view({})["maintenance"])

    print("")
    print("Instanzliste und Gesundheit: der Zustand steht da (RFC-0059)")
    PROBED = []
    pa._instance_probe = lambda n, i: PROBED.append(n) or ("ok", "Gesund", "HTTP 200")
    insts = {"crm": {"app_name": "CRM", "version": "1", "state": "standby",
                     "standby": {"since": "2026-10-16T10:00:00Z", "by": "auto-standby"}},
             "wach": {"app_name": "Wach", "version": "1", "maintenance": {"message": "x"}},
             "weckt": {"app_name": "Weckt", "version": "1", "state": "waking"}}
    pa.visible_instances = lambda: insts
    pa.load_instances = lambda: insts
    pa.caller_roles = lambda: {"server_admin"}
    page = c.get("/instances").get_data(as_text=True)
    ok("Liste: Spalte Status, Standby mit Urheber, geweckt, Wartung",
       "<th>Status</th>" in page and "Standby" in page and "auto-standby" in page
       and "Wird geweckt" in page and "Wartung" in page and "Gesund" in page, page[:300])
    ok("Schlafende werden nicht abgefragt (die Weckseite ist kein Fehler)",
       PROBED == ["wach"], PROBED)
    ok("Helfer (Liste und Gesundheitsseite teilen ihn): schlafend -> warn, wach -> None",
       pa._sleep_status(insts["crm"])[:2] == ("warn", "Standby")
       and pa._sleep_status(insts["weckt"])[1] == "Wird geweckt"
       and pa._sleep_status(insts["wach"]) is None)

print("")
print("OK" if not fails else f"{fails} FEHLER")
sys.exit(1 if fails else 0)
