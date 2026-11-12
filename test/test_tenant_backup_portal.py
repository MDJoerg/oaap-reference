#!/usr/bin/env python3
"""Die Portalseite „Sicherung“ je Mandant (RFC-0081 Stufe 2, Portal 0.3.44).

    tenant_admin   sieht die Art seines Mandanten; bei „eigenes Archiv“
                   Uhrzeit und Anzahl, nie die Art; einen fremden Mandanten
                   nie.
    server_admin   wählt den Mandanten, ändert Art, Grund und Obergrenze;
                   jede Änderung ist eine Anfrage `backup-tenant` im Spool.

Läuft im Portal-Abbild (Flask). Aufruf: python3 test/test_tenant_backup_portal.py
"""
import json
import os
import sys
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-tbportal-test-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services"))
sys.path.insert(0, os.path.join(HERE, "..", "platform"))
import appctl as a  # noqa: E402

a.os.geteuid = lambda: 0
fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:700]}")


dflt = a.ensure_default_tenant()
tv, _ = a.tenant_create("verein", "Verein")
other, _ = a.tenant_create("andere", "Andere")
a.SYSTEMD_DIR = tempfile.mkdtemp()
a._systemctl = lambda *x, **k: None
a.backup_tenant_set(tv, "own", at="02:15", keep=2, keep_max=3)
a.backup_tenant_set(other, "none", reason="sichert selbst")

sys.path.insert(0, os.path.join(HERE, "..", "platform", "services", "portal"))
import app as portal  # noqa: E402

portal.TENANTS_FILE = a.TENANTS_FILE
portal.BACKUP_EXCLUSIONS_FILE = a.BACKUP_EXCLUSIONS
portal.BACKUP_TENANTS_DIR = a.TENANT_BACKUP_DIR
portal.BACKUP_LAST = a.BACKUP_RUN_FILE
portal.BACKUP_CLASS = a.BACKUP_CLASS_FILE
TMP = tempfile.mkdtemp()
portal.SPOOL_DIR = os.path.join(TMP, "spool")
portal.SPOOL_QUEUE = os.path.join(portal.SPOOL_DIR, "queue")
portal.SPOOL_RESULTS = os.path.join(portal.SPOOL_DIR, "results")
os.makedirs(portal.SPOOL_RESULTS, exist_ok=True)
portal.TENANT_BACKUP_WAIT = 3
WHO = {"role": "tenant_admin", "tenant": tv}
portal.caller_roles = lambda: {WHO["role"]}
portal.caller_scope = lambda: (WHO["role"], WHO["tenant"])
cl = portal.app.test_client()
H = {"X-OAAP-User": "vorstand", "X-OAAP-Roles": "tenant_admin", "Origin": "http://localhost"}
SENT = []


def answer():
    import time
    for _ in range(40):
        for fn in os.listdir(portal.SPOOL_QUEUE) if os.path.isdir(portal.SPOOL_QUEUE) else []:
            path = os.path.join(portal.SPOOL_QUEUE, fn)
            SENT.append(json.load(open(path)))
            os.remove(path)
            json.dump({"ok": True, "message": "gespeichert"},
                      open(os.path.join(portal.SPOOL_RESULTS, fn), "w"))
            return
        time.sleep(0.1)


def post(path, form):
    th = threading.Thread(target=answer)
    th.start()
    r = cl.post(path, data=form, headers=H)
    th.join()
    return r


portal._same_origin = lambda: True
portal.identity_users = lambda: []
portal.read_audit = lambda *x, **k: []
t = cl.get("/tenant/backup", headers=H).get_data(as_text=True)
ok("tenant_admin: eigenes Archiv mit Uhrzeit und Anzahl, kein Formular für die Art",
   "eigenes Archiv" in t and 'value="02:15"' in t and "Art der Sicherung ändern" not in t, t[-1500:])
ok("… einen fremden Mandanten sieht er nicht",
   cl.get("/tenant/backup?tenant=andere", headers=H).status_code == 303)
r = post("/tenant/backup", {"op": "schedule", "at": "04:00", "keep": "3"})
req = SENT[-1]
ok("Speichern: eine Anfrage backup-tenant, ohne Mandant, mit Uhrzeit und Anzahl",
   req["action"] == "backup-tenant" and "tenant" not in req["args"]
   and req["args"]["at"] == "04:00" and req["args"]["keep"] == "3", req)
r = post("/tenant/backup", {"op": "class", "class": "none", "reason": "x"})
ok("… eine versuchte Art-Änderung reist nicht als Art",
   "class" not in SENT[-1]["args"], SENT[-1])
t = cl.get("/tenant", headers=H).get_data(as_text=True)
ok("die Mandantenseite verlinkt die Sicherung", 'href="/tenant/backup"' in t)

WHO.update(role="server_admin", tenant=dflt)
H = {"X-OAAP-User": "betreiber", "X-OAAP-Roles": "server_admin"}
t = cl.get("/tenant/backup?tenant=andere", headers=H).get_data(as_text=True)
ok("server_admin: „keine Sicherung“ mit Grund und das Formular für die Art",
   "keine Sicherung" in t and "sichert selbst" in t and "Art der Sicherung ändern" in t, t[-1500:])
r = post("/tenant/backup?tenant=andere", {"op": "class", "class": "own", "reason": "",
                                         "keep_max": "5"})
req = SENT[-1]
ok("server_admin: Art, Obergrenze und der Mandant reisen",
   req["args"].get("class") == "own" and req["args"].get("tenant") == other
   and req["args"].get("keep_max") == "5", req)
ok("… zurück auf derselben Seite", "tenant=andere" in r.headers.get("Location", ""),
   r.headers.get("Location"))
t = cl.get("/tenant", headers=H).get_data(as_text=True)
ok("die Liste der Mandanten verlinkt je Mandant die Sicherung",
   "/tenant/backup?tenant=verein" in t and "/tenant/backup?tenant=andere" in t)

print("\n=== die Tür auf dem Host ===")
os.makedirs(os.path.dirname(a._identity_users_path()), exist_ok=True)
with open(a._identity_users_path(), "w", encoding="utf-8") as f:
    json.dump([{"username": "boss", "roles": ["server_admin"], "active": True, "tenant": dflt},
               {"username": "vorstand", "roles": ["tenant_admin"], "active": True, "tenant": tv}], f)
tid, role, _ = a.acting_tenant("vorstand", other)
ok("ein tenant_admin, der einen fremden Mandanten nennt, wird abgewiesen", tid is None)
tid, role, _ = a.acting_tenant("vorstand", "")
ok("ohne Angabe: sein eigener Mandant, als tenant_admin", tid == tv and role == "tenant_admin")
try:
    a.backup_tenant_set(tid, "none", reason="x", who="vorstand", role=role)
    said = ""
except ValueError as e:
    said = str(e)
ok("… und der Host weist seine Art-Änderung ab, auch wenn sie im Spool ankäme",
   "operator's" in said and a.tenant_backup_class(tv) == "own", said)

print(f"\n{'ALL PASS' if not fails else str(fails) + ' FAILURES'}")
sys.exit(1 if fails else 0)
