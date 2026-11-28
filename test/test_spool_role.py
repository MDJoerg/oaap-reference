#!/usr/bin/env python3
"""Der Worker prüft die Rolle selbst (RFC-0143 Stufe 0, gemessen 28.11.2026).

Auf oaap-test führte der Worker `maintenance on` für ein aktives Konto mit
der Rolle `user` aus -- die einzige Prüfung war `require_instance_admin` im
Portal. Der Spool ist Daten, kein Vertrauen: Jetzt verlangt der Worker für
jede Aktion, die das Portal nur Instanz-Admins anbietet
(`INSTANCE_ADMIN_ACTIONS`), selbst server_admin oder den tenant_admin des
Mandanten der Instanz. `wake` bleibt offen (die Weckseite).

Geprüft über den echten Worker; kein Docker, kein Knoten. Der Test beißt:
Ohne `maintenance` in der Liste fällt eine Prüfung.

Aufruf: python3 test/test_spool_role.py
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-spool-role-test-")
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
m.wake_instance = lambda name, inst, who="", role="": CALLS.append(("wake", name, who)) or "woken"
m.restart_instance = lambda name, inst, own_rid="", who="", role="": (
    CALLS.append(("restart", name, who)) or "restarted")
os.makedirs(m.APPS_DIR, exist_ok=True)
tid = m.ensure_default_tenant()
reg = m.load_registry()
reg["instances"]["web"] = {"app_id": "website", "name": "web", "tenant": tid,
                           "channel": "production", "port": 8400, "id": "w" * 12,
                           "routes": [{"path": "/", "roles": ["public"]}]}
m.save_registry(reg)
d = os.path.join(DATA, "data", "identity")
os.makedirs(d, exist_ok=True)
with open(os.path.join(d, "users.json"), "w", encoding="utf-8") as f:
    json.dump([
        {"username": "root", "roles": ["server_admin"], "tenant": "", "active": True},
        {"username": "tina", "roles": ["tenant_admin"], "tenant": tid, "active": True},
        {"username": "uli", "roles": ["user"], "tenant": tid, "active": True},
        {"username": "instance:partnerverwaltung", "roles": ["user"], "tenant": tid,
         "active": True},
    ], f)
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


def run_cases():
    CALLS.clear()
    print("=== ohne Admin-Rolle ===")
    r = queue(action="maintenance", instance="web", on=True, by="uli")
    ok("Wartung durch eine `user`-Rolle: abgewiesen, nichts geschaltet",
       refused(r) and not CALLS, (r, CALLS))
    r = queue(action="maintenance", instance="web", on=True, by="instance:partnerverwaltung")
    ok("… auch durch das Dienstkonto einer App (der Fall auf oaap-test)", refused(r) and not CALLS)
    r = queue(action="restart", instance="web", by="uli")
    ok("Neustart durch `user`: abgewiesen", refused(r) and not CALLS, r)
    for a in ("rollback", "promote", "throttle", "rename", "visibility", "remove"):
        r = queue(action=a, instance="web", by="uli", **({"from": "web"} if a == "promote" else {}))
        ok(f"{a} durch `user`: abgewiesen", refused(r), r)
    print("=== die berechtigten Wege bleiben offen ===")
    r = queue(action="maintenance", instance="web", on=True, by="tina")
    ok("tenant_admin des Mandanten: Wartung geht", r.get("ok")
       and CALLS[-1][:3] == ("maintenance", "web", True), (r, CALLS))
    r = queue(action="maintenance", instance="web", on=False, by="root")
    ok("server_admin: Wartung geht", r.get("ok") and CALLS[-1][2] is False, (r, CALLS))
    r = queue(action="wake", instance="web", by="uli")
    ok("Aufwecken bleibt für alle offen (Weckseite)", r.get("ok") and CALLS[-1][0] == "wake", r)


run_cases()
log = "\n".join(json.dumps(e) for e in m.read_tenant_log(limit=0))
ok("Protokoll: abgewiesene Aktion mit Person und Grund", "instance.refused" in log
   and "maintenance: no administrator" in log and "uli" in log)
ok("Liste spiegelt die Portal-Türen, Aufwecken nicht darin",
   "wake" not in m.INSTANCE_ADMIN_ACTIONS and {"maintenance", "restart", "promote", "rollback",
                                               "site-check"} <= m.INSTANCE_ADMIN_ACTIONS)

print("=== Mutation: beißt der Test? ===")
before = fails
saved = m.INSTANCE_ADMIN_ACTIONS
m.INSTANCE_ADMIN_ACTIONS = saved - {"maintenance"}
with contextlib.redirect_stdout(io.StringIO()):
    run_cases()
bit = fails > before
fails = before
m.INSTANCE_ADMIN_ACTIONS = saved
ok("ohne `maintenance` in der Liste fallen Prüfungen", bit)

print()
print("OK" if not fails else f"{fails} FEHLER")
sys.exit(1 if fails else 0)
