#!/usr/bin/env python3
"""Personen an Instanzen binden (RFC-0143 Stufe 1).

Ein tenant_admin bindet eine Person des Mandanten an eine Instanz als
Beobachter, Betreuer oder Verantwortlich. Der Spool-Worker urteilt mit
einer Funktion `may()`: erst die Plattform-Rolle, dann die Bindung. Eine
Bindung an die Produktivinstanz gilt auch für ihre Test-Instanz (D2).

Geprüft über den echten Worker; kein Docker, kein Knoten. Der Test beißt:
ohne `maintenance` beim Betreuer und ohne den Weg über die Produktivinstanz
fallen Prüfungen.

Aufruf: python3 test/test_people.py
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-people-test-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, os.path.join(HERE, "..", "platform"))

import appctl as m                                            # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:500]}")


m.run = lambda cmd, *a, **kw: types.SimpleNamespace(stdout="", stderr="", returncode=0)
m.reload_gateway = lambda: None
CALLS = []
m.maintenance_set = lambda name, inst, on, message="", who="", role="": (
    CALLS.append(("maintenance", name, on, who, role)) or "is in maintenance")
m.restart_instance = lambda name, inst, own_rid="", who="", role="": (
    CALLS.append(("restart", name, who, role)) or "restarted")
m.site_check_button = lambda name, inst, who="", role="": (
    CALLS.append(("check-run", name, who, role)) or "checking")
m.site_check_set = lambda name, inst, on, who="", role="": (
    CALLS.append(("check-set", name, on, who, role)) or "switched")


def fake_promote(source, target, confirmed=False):
    CALLS.append(("promote", source, target))
    return "1.1.0", "a" * 40, [], target


m.promote_artifact = fake_promote
os.makedirs(m.APPS_DIR, exist_ok=True)
tid = m.ensure_default_tenant()
tenants = m.load_tenants()
tenants["t-other"] = {"label": "andere", "slug": "andere"}
m.save_tenants(tenants)


def inst(name, channel="production", tenant=None, **kw):
    return {"app_id": "website", "name": name, "tenant": tenant or tid,
            "channel": channel, "port": 8400, "id": name[:1] * 12,
            "routes": [{"path": "/", "roles": ["public"]}], **kw}


reg = m.load_registry()
reg["instances"].update({
    "web": inst("web", source={"kind": "artifact", "promoted_from": "web-test"}),
    "web-test": inst("web-test", channel="test"),
    "shop": inst("shop"),
    "lab-test": inst("lab-test", channel="test"),
})
m.save_registry(reg)
d = os.path.join(DATA, "data", "identity")
os.makedirs(d, exist_ok=True)
USERS = [
    {"username": "root", "roles": ["server_admin"], "tenant": "", "active": True},
    {"username": "tina", "roles": ["tenant_admin"], "tenant": tid, "active": True},
    {"username": "uli", "roles": ["user"], "tenant": tid, "active": True},
    {"username": "olga", "roles": ["partner"], "tenant": tid, "active": True},
    {"username": "tess", "roles": ["user"], "tenant": tid, "active": True},
    {"username": "bea", "roles": ["user"], "tenant": tid, "active": True},
    {"username": "otto", "roles": ["user"], "tenant": "t-other", "active": True},
    {"username": "ina", "roles": ["user"], "tenant": tid, "active": False},
]


def write_users():
    with open(os.path.join(d, "users.json"), "w", encoding="utf-8") as f:
        json.dump(USERS, f)


write_users()
QUEUE = os.path.join(m.SPOOL_DIR, "queue")
_rid = [0]


def queue(**req):
    _rid[0] += 1
    rid = f"r{_rid[0]}"
    os.makedirs(QUEUE, exist_ok=True)
    with open(os.path.join(QUEUE, f"{rid}.json"), "w", encoding="utf-8") as f:
        json.dump({"id": rid, **req}, f)
    with contextlib.redirect_stdout(io.StringIO()):
        m.cmd_process_deploys(None)
    with open(os.path.join(m.SPOOL_DIR, "results", f"{rid}.json"), encoding="utf-8") as f:
        return json.load(f)


def refused(r):
    return not r.get("ok") and "requires server_admin or the tenant_admin" in r.get("message", "")


def refuses(fn, *a, **kw):
    try:
        fn(*a, **kw)
    except m.PeopleRefused as e:
        return str(e)
    return ""


print("=== binden (CLI-Weg, people_set) ===")
ok("Betreuer binden", "Betreuer" in m.people_set("web", "add", "uli", "betreuer", who="tina"))
ok("Verantwortlich binden (Partnerin, D6)",
   "Verantwortlich" in m.people_set("web", "add", "olga", "owner", who="tina"))
ok("Beobachterin binden", "Beobachter" in m.people_set("web", "add", "bea", "observer"))
ok("eigene Bindung der Test-Instanz", "Betreuer" in m.people_set("web-test", "add", "tess",
                                                                    "caretaker"))
ok("doppelt binden: abgewiesen, mit Hinweis auf set",
   "change it with set" in refuses(m.people_set, "web", "add", "uli", "owner"))
ok("Person eines anderen Mandanten: abgewiesen",
   "not a person of the instance's tenant" in refuses(m.people_set, "web", "add", "otto", "observer"))
ok("deaktiviertes Konto: abgewiesen",
   "no active account" in refuses(m.people_set, "web", "add", "ina", "observer"))
ok("unbekannte Rolle: abgewiesen", "role must be one of" in refuses(m.people_set, "web", "add",
                                                                     "tess", "admin"))
rec = m.load_registry()["instances"]["web"]["people"]
ok("Eintrag: person, role, since, by", {p["person"]: p["role"] for p in rec}
   == {"uli": "caretaker", "olga": "owner", "bea": "observer"}
   and all(p.get("since") and p.get("by") for p in rec), rec)

print("=== der Worker urteilt mit may() ===")
CALLS.clear()
r = queue(action="maintenance", instance="web", on=True, by="uli")
ok("Betreuer schaltet Wartung, Protokoll-Rolle bound:caretaker",
   r.get("ok") and CALLS[-1] == ("maintenance", "web", True, "uli", "bound:caretaker"), (r, CALLS))
r = queue(action="restart", instance="web", by="uli")
ok("Betreuer startet neu", r.get("ok") and CALLS[-1][0] == "restart", (r, CALLS))
r = queue(action="site-check", instance="web", op="run", by="uli")
ok("Betreuer: Seiten-Check „Jetzt prüfen“", r.get("ok") and CALLS[-1][0] == "check-run", r)
n = len(CALLS)
r = queue(action="site-check", instance="web", on=False, by="uli")
ok("Betreuer: Seiten-Check schalten abgewiesen (nur Verantwortlich)", refused(r)
   and len(CALLS) == n and "Betreuer does not allow" in r["message"], r)
for a in ("promote", "rollback", "remove", "rename", "config", "visibility", "site-watch"):
    r = queue(action=a, instance="web", by="uli", **({"from": "web-test"} if a == "promote" else {}))
    ok(f"Betreuer: {a} abgewiesen", refused(r), r)
r = queue(action="instance-people", instance="web", op="add", person="tess", role="observer",
          by="uli")
ok("Betreuer bindet niemanden (D3)", refused(r), r)
r = queue(action="maintenance", instance="web", on=True, by="bea")
ok("Beobachterin: Wartung abgewiesen", refused(r), r)
r = queue(action="maintenance", instance="shop", on=True, by="uli")
ok("nicht gebundene Instanz: abgewiesen", refused(r)
   and "no binding" not in r["message"], r)  # the answer names no binding detail

print("=== Produktiv und Test (D2) ===")
r = queue(action="maintenance", instance="web-test", on=True, by="uli")
ok("Bindung an web gilt für web-test", r.get("ok") and CALLS[-1][1] == "web-test", (r, CALLS))
r = queue(action="maintenance", instance="web", on=True, by="tess")
ok("Bindung nur an der Test-Instanz reicht nicht für Produktiv", refused(r), r)
r = queue(action="maintenance", instance="web-test", on=True, by="tess")
ok("… aber für die Test-Instanz selbst", r.get("ok"), r)
r = queue(action="maintenance", instance="lab-test", on=True, by="uli")
ok("andere Test-Instanz ohne Paar: abgewiesen", refused(r), r)
r = queue(action="promote", instance="web", **{"from": "web-test"}, by="olga")
ok("Verantwortliche setzt web-test produktiv", r.get("ok")
   and CALLS[-1] == ("promote", "web-test", "web"), (r, CALLS))
r = queue(action="promote", instance="web", **{"from": "lab-test"}, by="olga")
ok("… aber nicht aus einer fremden Test-Instanz", not r.get("ok")
   and "promotes only the test instance" in r["message"], r)
r = queue(action="site-check", instance="web", on=True, by="olga")
ok("Verantwortliche schaltet den Seiten-Check (D5)", r.get("ok") and CALLS[-1][0] == "check-set", r)
r = queue(action="remove", instance="web", by="olga")
ok("Verantwortliche: löschen nie über eine Bindung", refused(r), r)

print("=== Konto und Mandant urteilen live ===")
USERS[2]["active"] = False
write_users()
r = queue(action="maintenance", instance="web", on=True, by="uli")
ok("deaktiviert: die Bindung wirkt nicht mehr", not r.get("ok"), r)
USERS[2]["active"] = True
USERS[2]["tenant"] = "t-other"
write_users()
r = queue(action="maintenance", instance="web", on=True, by="uli")
ok("in anderen Mandanten verschoben: die Bindung wirkt nicht mehr", refused(r), r)
USERS[2]["tenant"] = tid
write_users()

print("=== die Karte: instance-people über den Spool ===")
r = queue(action="instance-people", instance="shop", op="add", person="tess", role="betreuer",
          by="tina")
ok("tenant_admin bindet über den Spool", r.get("ok") and any(
    p["person"] == "tess" for p in m.load_registry()["instances"]["shop"]["people"]), r)
r = queue(action="instance-people", instance="shop", op="set", person="tess", role="owner",
          by="tina")
ok("… ändert die Rolle", r.get("ok") and m.load_registry()["instances"]["shop"]["people"][0]
   ["role"] == "owner", r)
r = queue(action="instance-people", instance="shop", op="remove", person="tess", by="root")
ok("server_admin beendet die Bindung, das Feld verschwindet", r.get("ok")
   and "people" not in m.load_registry()["instances"]["shop"], r)

print("=== may() direkt, Protokoll ===")
ok("may: server_admin", m.may("root", "shop", "remove") == (True, "role: server_admin"))
ok("may: tenant_admin", m.may("tina", "shop", "remove") == (True, "role: tenant_admin"))
ok("may: Betreuer", m.may("uli", "web", "maintenance") == (True, "bound: caretaker"))
ok("Rollen schachteln sich (D1)", m.BINDING_ACTIONS["observer"] < m.BINDING_ACTIONS["caretaker"]
   < m.BINDING_ACTIONS["owner"])
ok("nie über eine Bindung: löschen, installieren, Konfiguration, Adressen, binden",
   not ({"remove", "install", "config", "rename", "address", "visibility", "token",
         "instance-people"} & m.BINDING_ACTIONS["owner"]))
log = "\n".join(json.dumps(e) for e in m.read_tenant_log(limit=0))
ok("Protokoll: instance.people mit alt -> neu", "instance.people" in log
   and "uli: - -> caretaker" in log and "tess: owner -> -" in log)
ok("Protokoll: Absage nennt die Bindung", "Betreuer does not allow" in log)
src = open(m.__file__, encoding="utf-8").read()
ok("Update/Neubau/Promotion behalten `people` (wie site_watch)",
   'reg["instances"][name]["people"] = [dict(p) for p in people_of(inst)]' in src)

print("=== Mutation: beißt der Test? ===")


def bites(patch, undo):
    global fails
    before = fails
    patch()
    with contextlib.redirect_stdout(io.StringIO()):
        CALLS.clear()
        r1 = queue(action="maintenance", instance="web", on=True, by="uli")
        r2 = queue(action="maintenance", instance="web-test", on=True, by="uli")
    undo()
    fails = before
    return not (r1.get("ok") and r2.get("ok"))


saved = dict(m.BINDING_ACTIONS)
ok("ohne `maintenance` beim Betreuer fällt die Prüfung", bites(
    lambda: m.BINDING_ACTIONS.__setitem__("caretaker", saved["caretaker"] - {"maintenance"}),
    lambda: m.BINDING_ACTIONS.update(saved)))
saved_prod = m.production_of
ok("ohne den Weg über die Produktivinstanz fällt die Prüfung", bites(
    lambda: setattr(m, "production_of", lambda reg, key: (None, None)),
    lambda: setattr(m, "production_of", saved_prod)))

print("=== Portal: Karte „Zuständige“ ===")
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services", "portal"))
try:
    import app as portal  # noqa: E402
    from jinja2 import Environment
    regnow = m.load_registry()["instances"]
    portal.identity_users = lambda: USERS
    portal.load_instances = lambda: regnow
    portal.resolve_tenant = m.resolve_tenant
    src = portal.INSTANCE_EDIT_BODY
    a = src.index("<section class=\"panel {{ 'active' if tab == 'zugang' }}\">")
    bb = src.index("<h2>API-Schlüssel</h2>", a)
    t = Environment(autoescape=True).from_string(src[a:bb] + "</div></section>")
    pv = portal._people_view("web", regnow["web"])
    html = t.render(i={"key": "web", "name": "web", "people": pv}, tab="zugang")
    ok("Karte: Personen mit Rolle, Ändern und Beenden",
       "Zuständige" in html and "olga" in html and "uli" in html
       and 'value="caretaker" selected' in html and "Beenden" in html, html[:400])
    ok("Kandidaten: aktive Personen des Mandanten, ohne Gebundene, Fremde, Inaktive",
       set(pv["candidates"]) == {"tess", "tina"}, pv["candidates"])
    ok("Formular an die Tür, zurück in den Reiter",
       'action="/instances/web/people"' in html and 'name="tab" value="zugang"' in html)
    pt = portal._people_view("web-test", regnow["web-test"])
    ok("Test-Instanz zeigt die Bindungen der Produktivinstanz",
       {p["person"] for p in pt["through"]} == {"uli", "olga", "bea"}
       and pt["through"][0]["via"] == "web", pt["through"])
    USERS[3]["active"] = False
    ok("inaktives Konto: „wirkt nicht“", any(r["dead"] for r in portal._people_view(
        "web", regnow["web"])["rows"] if r["person"] == "olga"))
    USERS[3]["active"] = True
    ok("Tür im Portal", any(r.rule == "/instances/<name>/people"
                            for r in portal.app.url_map.iter_rules()))
except ImportError as e:
    print("SKIP  Portal (Flask fehlt):", e)

print()
print("OK" if not fails else f"{fails} FEHLER")
sys.exit(1 if fails else 0)
