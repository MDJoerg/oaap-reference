#!/usr/bin/env python3
"""Anmeldung auf allen Routen, je Instanz (RFC-0092, gateway 0.2.23).

Ein Paket darf Routen 'public' erklaeren. Der Betreiber einer Instanz
will manchmal weniger -- ohne neues Paket. Ein Schalter am Instanzsatz:

    an       jede Route fragt /verify, auch die oeffentliche; Routen mit
             Rollen bleiben, wie sie sind
    eng      /platform/* und /auth/* bleiben unberuehrt; die Routen
             im Satz werden NICHT umgeschrieben
    bleibt   ein Paket-Update laesst den Schalter stehen
    aus      zurueck zu dem, was das Paket sagt
    Log      jede Aenderung im Mandantenlog
    Probe    eine Generalprobe hat das schon -- abgelehnt

Aufruf: python3 test/test_login_required.py
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-loginreq-test-")
os.environ["OAAP_DATA_DIR"] = DATA
PLATFORM = os.path.join(HERE, "..", "platform")
sys.path.insert(0, PLATFORM)

import appctl as m                                            # noqa: E402

m.reload_gateway = lambda: None
m.refresh_destinations = lambda: None
os.makedirs(m.CADDY_APPS_DIR, exist_ok=True)

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:600]}")


ROUTES = [{"path": "/", "roles": ["keyuser"]},
          {"path": "/hook", "roles": ["public"]}]


def record(**over):
    base = {
        "app_id": "crm", "app_name": "CRM", "version": "1.4.0",
        "channel": "production", "port": 8101, "container": "oaap-app-crm",
        "image": "oaap-app/crm:1.4.0", "svc_port": 8000,
        "services": [{"service": "", "container": "oaap-app-crm",
                      "image": "oaap-app/crm:1.4.0", "build": "", "port": 8000}],
        "routes": [dict(r) for r in ROUTES],
        "storage": [], "config": [], "roles": ["keyuser"], "visibility": {},
        "source": {"kind": "artifact", "version": "1.4.0",
                   "stored": "1.4.0-a.zip", "sha256": "a" * 64, "path": ""},
    }
    base.update(over)
    return base


tid = m.ensure_default_tenant()
reg = m.load_registry()
reg["instances"]["crm"] = record(tenant=tid, id="aaaaaaaaaaaa", name="crm")
reg["instances"]["crm-probe"] = record(
    tenant=tid, id="dddddddddddd", name="crm-probe", port=8104,
    container="oaap-app-crm-probe",
    rehearsal={"of": "crm", "code_from": "crm", "archive": "x",
               "archive_created": "2026-11-01T00:00:00Z",
               "created": "2026-11-18T00:00:00Z",
               "expires": "2099-01-01T00:00:00Z", "extensions": 0})
m.save_registry(reg)


def site(name="crm"):
    m.write_app_caddy(name, m.load_registry()["instances"][name])
    with open(os.path.join(m.CADDY_APPS_DIR, f"{name}.caddy"),
              encoding="utf-8") as f:
        return f.read()


def block(text, head):
    """The lines of the handle block that starts with `head`."""
    out, inside = [], False
    for ln in text.splitlines():
        if ln.startswith(head):
            inside = True
            continue
        if inside:
            if ln == "\t}":
                break
            out.append(ln)
    return "\n".join(out)


print("Vorher: die oeffentliche Route ist oeffentlich")
before = site()
hook = block(before, "\thandle /hook* {")
ok("/hook fragt /verify nicht (nur die Bremse)", hook and "/verify" not in hook and "/throttle" in hook, hook)
ok("/ fragt mit der Rolle keyuser",
   "roles=keyuser" in block(before, "\thandle {"))

print("\nAn")
msg = m.login_required_set("crm", m.load_registry()["instances"]["crm"], True,
                           "Demo, nicht anonym", who="tester")
ok("der Satz nennt die Route, die zugeht", "/hook" in msg, msg)
rec = m.load_registry()["instances"]["crm"]
ok("der Schalter steht am Instanzsatz, mit wer und Grund",
   m.login_required_of(rec) and rec["login_required"]["by"] == "tester"
   and rec["login_required"]["reason"] == "Demo, nicht anonym", rec)
ok("die gespeicherten Routen sind NICHT umgeschrieben",
   rec["routes"] == ROUTES, rec["routes"])
after = site()
hook = block(after, "\thandle /hook* {")
ok("/hook fragt jetzt /verify, nicht mehr die Bremse", "uri /verify?" in hook and "/throttle" not in hook, hook)
ok("... ohne Rolle: angemeldet reicht", "uri /verify?roles=&" in hook
   or "uri /verify?roles=\n" in hook + "\n", hook)
ok("... fuer diese Instanz", "instance=crm" in hook, hook)
ok("/ fragt weiter mit keyuser, unveraendert",
   block(after, "\thandle {") == block(before, "\thandle {"))
ok("/platform/* bleibt, wie es war",
   block(after, "\thandle /platform/* {")
   == block(before, "\thandle /platform/* {"))
ok("/auth/* bleibt, wie es war",
   block(after, "\thandle /auth/* {") == block(before, "\thandle /auth/* {"))
ok("keine neue OPTIONS-Ausnahme fuer die ehemals oeffentliche Route",
   after.count("method OPTIONS") == before.count("method OPTIONS"))
ok("noch einmal an: nichts Neues", "already" in m.login_required_set(
    "crm", m.load_registry()["instances"]["crm"], True))

with open(m.TENANT_LOG, encoding="utf-8") as f:
    LOG = [json.loads(ln) for ln in f if ln.strip()]
hit = [e for e in LOG if e.get("action") == "instance.login-required"]
ok("im Mandantenlog: an, die Route, der Grund", hit and "an" in hit[-1]
   ["detail"] and "/hook" in hit[-1]["detail"]
   and "Demo" in hit[-1]["detail"], hit)

print("\nAus")
msg = m.login_required_set("crm", m.load_registry()["instances"]["crm"],
                           False, who="tester")
ok("zurueck zum Paket", "package" in msg, msg)
ok("die Site ist wieder die von vorher", site() == before)
ok("noch einmal aus: nichts zu tun", "does not" in m.login_required_set(
    "crm", m.load_registry()["instances"]["crm"], False))

print("\nGeneralprobe")
try:
    m.login_required_set("crm-probe",
                         m.load_registry()["instances"]["crm-probe"], True)
    ok("eine Generalprobe wird abgelehnt", False)
except m.DiagnoseRefused as e:
    ok("eine Generalprobe wird abgelehnt", "rehearsal" in str(e), e)

print("\nWas den Schalter traegt")
SRC = open(os.path.join(PLATFORM, "appctl.py"), encoding="utf-8").read()
ok("ein Paket-Update uebernimmt den Schalter",
   'reg["instances"][name]["login_required"] = dict(inst["login_required"])'
   in SRC)
ok("die CLI kennt 'oaap app login-required'",
   'sub.add_parser("login-required"' in SRC)
ok("der Host-Worker fuehrt die Portal-Aktion aus",
   'elif action == "login-required":' in SRC)
ok("der Name wird wie bei anderen Instanzbefehlen aufgeloest",
   '"maintenance", "login-required",' in SRC)
PORTAL = open(os.path.join(PLATFORM, "services", "portal", "app.py"),
              encoding="utf-8").read()
ok("das Portal hat den Knopf, fuer Instanzverwalter",
   '@app.post("/instances/<name>/login-required")' in PORTAL
   and "Anmeldung auf allen Routen einschalten" in PORTAL)
ok("... und nennt die Routen, die zugehen", "public_routes" in PORTAL)

print(f"\n{'ALLE PASS' if not fails else str(fails) + ' FAIL'}")
sys.exit(1 if fails else 0)
