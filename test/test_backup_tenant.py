#!/usr/bin/env python3
"""Das Archiv eines Mandanten, und das, was ein Knotenarchiv auslaesst.

RFC-0029 D5 und D5b, und sie gehoeren zusammen: Einen Mandanten aus der
Knotensicherung herauszunehmen ist nur vertretbar, wenn er sich einzeln
sichern laesst -- sonst waere das Ergebnis ein Kunde ohne jede Sicherung
und die Zusage, dass sich schon jemand anders kuemmert. Genau die
Abmachung, die lautlos scheitert.

Was sich damit aendert, ist nicht klein. Heute ist das Betreiberarchiv
"alles auf dieser Maschine" -- auf einem Knoten mit Kunden also jeder
vollstaendige Datenbestand jedes Kunden, dort, wo das Sicherungsziel
zufaellig steht. Mit dem Ausnehmen wird daraus "alles, wofuer ich
geradestehe".

Die drei Bedingungen der Entscheidung werden einzeln geprueft: das
Archiv schreibt auf, was es weglaesst; die Wiederherstellung SAGT es,
statt es entdecken zu lassen; und der Mandant sieht es in seinem
eigenen Protokoll.

Aufruf: python3 test/test_backup_tenant.py
"""
import argparse
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-backup-tenant-test-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, os.path.join(HERE, "..", "platform"))

import appctl as m                                            # noqa: E402

m.reload_gateway = lambda: None

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:500]}")


class _Empty:
    """Was `docker` in diesem Lauf antwortet: nichts laeuft."""
    stdout = ""
    stderr = ""
    returncode = 0


_real_run = m.run


def _no_docker(cmd, **kw):
    if cmd and cmd[0] == "docker":
        return _Empty()
    return _real_run(cmd, **kw)


m.run = _no_docker

# Eine Plattform, wie sie nach einer Installation aussieht.
os.makedirs(m.APP_DIR, exist_ok=True)
with open(os.path.join(m.APP_DIR, ".env"), "w", encoding="utf-8") as f:
    f.write("OAAP_VERSION=0.1.112\nOAAP_HTTP_PORT=80\n")
os.makedirs(os.path.join(DATA, "data", "identity"), exist_ok=True)

DEFAULT = m.ensure_default_tenant()
with contextlib.redirect_stdout(io.StringIO()):
    m.cmd_tenant(argparse.Namespace(
        action="create", name="cls", target=None, title="Kunde",
        account="", account_name="", grace_days=30, yes=True, count=50))
CLS, _t = m.tenant_by_label("cls")

reg = m.load_registry()
for label, tid, key in (("eigen", DEFAULT, "portal"), ("cls", CLS, "cls-viewer")):
    reg["instances"][key] = {
        "app_id": key, "app_name": key.title(), "version": "1.0",
        "channel": "production", "port": 8900 + len(reg["instances"]),
        "svc_port": 80, "container": f"oaap-app-{key}", "tenant": tid,
        "image": f"oaap/{key}:1.0", "build": "",
        "name": key.split("-")[-1], "id": m.new_instance_id(),
        "routes": [{"path": "/", "roles": ["user"]}]}
m.save_registry(reg)
for key, inst in m.load_registry()["instances"].items():
    d = os.path.join(m.instance_dir(key, inst), "storage")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "daten.txt"), "w", encoding="utf-8") as f:
        f.write(f"Nutzdaten von {key}\n")

with open(m._identity_users_path(), "w", encoding="utf-8") as f:
    json.dump([{"username": "joerg", "tenant": DEFAULT},
               {"username": "kunde", "tenant": CLS}], f)

OUT = tempfile.mkdtemp(prefix="oaap-backup-out-")


def backup(**kw):
    ns = argparse.Namespace(action="create", to=OUT, at="", keep=None,
                            on=False, off=False, refresh=False, target=None,
                            tenant="", reason="")
    for k, v in kw.items():
        setattr(ns, k, v)
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            m.cmd_backup(ns)
    except SystemExit:
        pass
    return buf.getvalue()


def listing(path):
    return subprocess.run(["tar", "-tzf", path], check=True, text=True,
                          capture_output=True).stdout.split()


def newest(prefix):
    files = sorted(f for f in os.listdir(OUT)
                   if f.startswith(prefix) and f.endswith(".tar.gz"))
    return os.path.join(OUT, files[-1]) if files else ""


print("D5 — das Archiv EINES Mandanten")

said = backup(tenant="cls")
arc = newest("oaap-tenant-cls-")
ok("es wird geschrieben", bool(arc), said)
entries = listing(arc) if arc else []
ok("es traegt die Daten dieses Mandanten",
   any(f"tenants/{CLS}/" in e for e in entries), entries[:10])
ok("und NICHT die des anderen",
   not any(f"tenants/{DEFAULT}/" in e for e in entries), entries[:10])
ok("seine Benutzer liegen bei", "tenant-users.json" in entries, entries)
ok("sein Protokoll auch", "tenant-audit.jsonl" in entries, entries)

mf = json.loads(subprocess.run(
    ["tar", "-xzOf", arc, "tenant-manifest.json"], check=True, text=True,
    capture_output=True).stdout)
ok("das Begleitblatt heisst ANDERS als das eines Knotenarchivs",
   "backup-manifest.json" not in entries and "tenant-manifest.json" in entries,
   "der Installer sucht genau nach dem anderen Namen -- ein Archiv, das "
   "er nicht zurueckspielen darf, darf nicht zurueckspielbar aussehen")
ok("es nennt den Mandanten", mf.get("tenant") == CLS and
   mf.get("tenant_label") == "cls", mf)
ok("es fuehrt nur dessen Instanzen", list(mf["instances"]) == ["cls-viewer"],
   mf["instances"])
ok("und es sagt von sich selbst, dass es nicht zurueckspielbar ist",
   mf.get("restorable") is False and "RFC-0029 D5" in mf.get("note", ""), mf)
ok("die Ausgabe sagt es dem Betreiber auch",
   "CANNOT be restored" in said, said)
ok("und dass nur die Apps dieses Mandanten standen",
   "rest of the node kept running" in said, said)

users = json.loads(subprocess.run(
    ["tar", "-xzOf", arc, "tenant-users.json"], check=True, text=True,
    capture_output=True).stdout)
ok("die Benutzerliste enthaelt nur diesen Mandanten",
   [u["username"] for u in users] == ["kunde"], users)

said = backup(tenant="gibtsnicht")
ok("ein unbekanntes Kuerzel wird abgelehnt, nicht ersetzt",
   "no tenant with label" in said, said)

print("")
print("D5b, Bedingung 3 — der Mandant sieht es in SEINEM Protokoll")


def ex(action, label, reason=""):
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            m.cmd_backup(argparse.Namespace(
                action=action, target=label, reason=reason, to="", at="",
                keep=None, on=False, off=False, refresh=False, tenant=""))
    except SystemExit:
        pass
    return buf.getvalue()


said = ex("exclude", "cls")
ok("ohne Begruendung wird nicht ausgenommen",
   not m.excluded_tenants() and "say why" in said, said)
ok("und die Ablehnung sagt, warum eine Begruendung noetig ist",
   "nobody remembers deciding not to" in said, said)

said = ex("exclude", "cls", "Kunde sichert selbst auf sein NAS")
ok("mit Begruendung wird ausgenommen", CLS in m.excluded_tenants(),
   m.excluded_tenants())
log = m.read_tenant_log(CLS, limit=50)
entry = next((e for e in log if e.get("action") == "backup.excluded"), None)
ok("der Mandant findet es in seinem eigenen Protokoll", entry is not None, log)
ok("mit der Begruendung im Wortlaut",
   entry and "NAS" in entry.get("detail", ""), entry)
ok("die Ausgabe nennt den Weg zum eigenen Archiv",
   "backup create --tenant cls" in said, said)
ok("und sagt, dass die Instanzen weiterlaufen",
   "keep running" in said, said)

buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    m.cmd_backup_status(None)
status = buf.getvalue()
ok("`backup status` zeigt, wer drin ist und wer nicht",
   "cls: EXCLUDED" in status and "default: in the node backup" in status,
   status)
ok("und sagt, dass das Archiv keine vollstaendige Kopie mehr ist",
   "NOT a complete copy" in status, status)

print("")
print("D5b, Bedingung 1 — das Archiv schreibt auf, was es weglaesst")

said = backup()
arc = newest("oaap-backup-")
ok("das Knotenarchiv wird trotzdem geschrieben", bool(arc), said)
entries = listing(arc) if arc else []
ok("die Daten des ausgenommenen Mandanten fehlen",
   not any(f"tenants/{CLS}/" in e for e in entries), entries[:12])
ok("die des anderen sind da",
   any(f"tenants/{DEFAULT}/" in e for e in entries), entries[:12])
ok("die Vollstaendigkeitspruefung schlaegt deswegen NICHT an",
   "does not contain the data of" not in said,
   "sonst waere die Regel nicht abgeschwaecht, sondern das Ausnehmen "
   "unmoeglich")

nm = json.loads(subprocess.run(
    ["tar", "-xzOf", arc, "backup-manifest.json"], check=True, text=True,
    capture_output=True).stdout)
ok("das Begleitblatt fuehrt die enthaltenen Mandanten",
   nm.get("tenants_included") == ["default"], nm.get("tenants_included"))
left = nm.get("tenants_excluded") or []
ok("und die ausgelassenen, mit Grund, Person und Instanzen",
   len(left) == 1 and left[0]["label"] == "cls"
   and "NAS" in left[0]["reason"] and left[0]["instances"] == ["cls-viewer"],
   left)
ok("die Ausgabe sagt es dem Betreiber, bevor er es sucht",
   "is NOT in this archive" in said and "not a complete copy" in said, said)

print("")
print("D5b, Bedingung 2 — die Wiederherstellung SAGT es")

with open(os.path.join(DATA, "last-restore-manifest.json"), "w",
          encoding="utf-8") as f:
    json.dump(nm, f)
m._deploy_from_registry = lambda name, inst: True
m._report_dropped_profiles = lambda: None
m.refresh_generated_sites = lambda: None
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    m.cmd_restore_instances(None)
out = buf.getvalue()
# Mit .index() gepruefet waere das ein Absturz statt eines Urteils, wenn
# der Satz fehlt -- und ein Test, der abstuerzt, meldet keinen FAIL.
# Genau daran ist die Mutation "die Wiederherstellung schweigt" zuerst
# vorbeigekommen.
warned, dormant = out.find("deliberately left tenants out"), out.find("DORMANT")
ok("sie nennt den ausgelassenen Mandanten ueberhaupt", warned >= 0, out)
ok("und zwar BEVOR sie etwas startet", 0 <= warned < dormant, out)
ok("die Instanz kommt zurueck, aber ruhend",
   "DORMANT cls-viewer" in out, out)
ok("und es wird gesagt, warum das keine Willkuer ist",
   "looks wiped" in out, out)
ok("die Instanz des anderen Mandanten wird normal gestartet",
   "1 restored" in out and "1 skipped" in out, out)

print("")
print("Zuruecknehmen geht auch, und steht ebenfalls im Protokoll")

said = ex("include", "cls")
ok("der Mandant ist wieder Teil der Knotensicherung",
   CLS not in m.excluded_tenants(), m.excluded_tenants())
log = m.read_tenant_log(CLS, limit=50)
ok("und sieht auch das in seinem Protokoll",
   any(e.get("action") == "backup.included" for e in log), log)

print("")
print(f"{'FEHLER' if fails else 'Alles gruen'} - {fails} Fehlschlag(e)")
sys.exit(1 if fails else 0)
