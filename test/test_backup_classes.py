#!/usr/bin/env python3
"""Backup-Klassen (RFC-0081 Stufe 1, oaap.data.backup 0.5).

    Die Erklärung   offsite / local / none; „none“ nur mit Grund; jede
                    Änderung im Protokoll; nur bei Änderung geschrieben.
    Test-Instanzen  mit dem Schalter fehlen ihre Daten im Archiv, ihre
                    Container stehen nicht still, das Begleitblatt nennt
                    sie, die Vollständigkeitsprüfung schlägt nicht an, die
                    Wiederherstellung lässt sie ruhen und sagt es.
    Meldungen       ein Knoten mit „none“ bekommt kein „Kein aktuelles
                    Backup“; „local“ ohne Lauf weiterhin.

Braucht tar, kein Docker. Aufruf: python3 test/test_backup_classes.py
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
DATA = tempfile.mkdtemp(prefix="oaap-backup-classes-test-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services"))
sys.path.insert(0, os.path.join(HERE, "..", "platform"))

import appctl as m  # noqa: E402

m.reload_gateway = lambda: None
fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:600]}")


class _Empty:
    stdout = ""
    stderr = ""
    returncode = 0


_real_run = m.run
_real_sub = subprocess.run
DOCKER = []


def _run(cmd, **kw):
    if cmd and cmd[0] == "docker":
        if cmd[1] == "ps":
            r = _Empty()
            r.stdout = "oaap-app-web\noaap-app-web-test\n"
            return r
        DOCKER.append(list(cmd))
        return _Empty()
    return _real_run(cmd, **kw)


def _sub(cmd, *a, **kw):
    if cmd and cmd[0] == "docker":
        DOCKER.append(list(cmd))
        return _Empty()
    return _real_sub(cmd, *a, **kw)


m.run, subprocess.run = _run, _sub

os.makedirs(m.APP_DIR, exist_ok=True)
with open(os.path.join(m.APP_DIR, ".env"), "w", encoding="utf-8") as f:
    f.write("OAAP_VERSION=0.1.258\nOAAP_HTTP_PORT=80\n")
os.makedirs(os.path.join(DATA, "data", "identity"), exist_ok=True)
DEFAULT = m.ensure_default_tenant()
reg = m.load_registry()
for key, ch in (("web", "production"), ("web-test", "test")):
    reg["instances"][key] = {
        "app_id": "web", "app_name": "Web", "version": "1.0", "channel": ch,
        "port": 8900 + len(reg["instances"]), "svc_port": 80,
        "container": f"oaap-app-{key}", "tenant": DEFAULT, "image": "oaap/web:1.0",
        "build": "", "name": key, "id": m.new_instance_id(),
        "routes": [{"path": "/", "roles": ["user"]}]}
m.save_registry(reg)
for key, inst in m.load_registry()["instances"].items():
    d = os.path.join(m.instance_dir(key, inst), "storage")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "daten.txt"), "w", encoding="utf-8") as f:
        f.write(f"Nutzdaten von {key}\n")
with open(m._identity_users_path(), "w", encoding="utf-8") as f:
    json.dump([{"username": "joerg", "tenant": DEFAULT}], f)
OUT = tempfile.mkdtemp(prefix="oaap-backup-classes-out-")


def cli(action, target=None, reason=""):
    ns = argparse.Namespace(action=action, target=target, reason=reason, to=OUT, at="",
                            keep=None, on=False, off=False, refresh=False, tenant="")
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            m.cmd_backup(ns)
    except SystemExit:
        pass
    return buf.getvalue()


def newest():
    files = sorted(f for f in os.listdir(OUT) if f.startswith("oaap-backup-")
                   and f.endswith(".tar.gz"))
    return os.path.join(OUT, files[-1]) if files else ""


print("=== die Erklärung ===")
said = cli("class")
ok("ohne Erklärung: „not declared“, Test-Instanzen werden gesichert",
   "(not declared)" in said and "backed up" in said, said)
said = cli("class", "none")
ok("„none“ ohne Grund abgelehnt", "say why" in said and m.backup_class()["class"] == "",
   said)
said = cli("class", "irgendwas")
ok("unbekannte Klasse abgelehnt", "one of" in said, said)
said = cli("class", "local")
ok("„local“ geht ohne Grund", m.backup_class()["class"] == "local", said)
log = m.read_tenant_log(DEFAULT, limit=20)
ok("die Änderung steht im Protokoll des Knotens",
   any(e.get("action") == "backup.class" and "local" in e.get("detail", "") for e in log), log)
mt = os.stat(m.BACKUP_CLASS_FILE).st_mtime_ns
said = cli("class", "local")
ok("dieselbe Klasse noch einmal: unverändert, Datei nicht angefasst",
   "Unchanged" in said and os.stat(m.BACKUP_CLASS_FILE).st_mtime_ns == mt, said)
said = cli("test-instances", "vielleicht")
ok("test-instances kennt nur on/off", "on|off" in said, said)

print("=== ohne Schalter: alles im Archiv ===")
DOCKER.clear()
said = cli("create")
arc = newest()
entries = _real_sub(["tar", "-tzf", arc], check=True, text=True,
                    capture_output=True).stdout.split() if arc else []
inst = m.load_registry()["instances"]
rel = {k: os.path.relpath(m.instance_dir(k, i), DATA).replace(os.sep, "/") for k, i in inst.items()}
ok("ohne Schalter liegen beide Instanzen im Archiv",
   all(any(e.startswith(r) for e in entries) for r in rel.values()), (said, entries[:12]))
os.remove(arc)

print("=== Test-Instanzen auslassen ===")
said = cli("test-instances", "off")
ok("Schalter gesetzt", m.backup_class()["skip_test"], said)
DOCKER.clear()
said = cli("create")
arc = newest()
ok("das Archiv wird geschrieben", bool(arc), said)
entries = _real_sub(["tar", "-tzf", arc], check=True, text=True,
                    capture_output=True).stdout.split() if arc else []
ok("die Daten der Test-Instanz fehlen", not any(e.startswith(rel["web-test"]) for e in entries),
   [e for e in entries if "instances" in e][:8])
ok("die der Produktiv-Instanz sind da", any(e.startswith(rel["web"]) for e in entries))
stopped = [c for c in DOCKER if c[1] == "stop" for c in c[2:]]
ok("der Container der Test-Instanz steht nicht still",
   "oaap-app-web-test" not in stopped and "oaap-app-web" in stopped, DOCKER)
ok("die Vollständigkeitsprüfung schlägt nicht an", "does not contain" not in said, said)
mf = json.loads(_real_sub(["tar", "-xzOf", arc, "backup-manifest.json"], check=True,
                          text=True, capture_output=True).stdout) if arc else {}
ok("das Begleitblatt nennt die ausgelassene Test-Instanz und die Klasse",
   mf.get("test_instances_left_out") == ["web-test"] and mf.get("backup_class") == "local", mf)
ok("die Ausgabe sagt es", "test instance(s) are NOT in this archive" in said, said)
ok("… und nennt die weiterlaufenden Container als Test-Instanzen, nicht als ausgenommenen Mandanten",
   "container(s) of test instances keep running (oaap-app-web-test)" in said
   and "excluded tenant" not in said, said)
status = cli("status")
ok("`backup status` zeigt Klasse und ausgelassene Test-Instanzen",
   "Backup class: local" in status and "LEFT OUT (1: web-test)" in status, status)

print("=== die Wiederherstellung ===")
with open(os.path.join(DATA, "last-restore-manifest.json"), "w", encoding="utf-8") as f:
    json.dump(mf, f)
STARTED = []
m._deploy_from_registry = lambda name, inst: STARTED.append(name) or True
m._report_dropped_profiles = lambda: None
m.refresh_generated_sites = lambda: None
buf = io.StringIO()
try:
    with contextlib.redirect_stdout(buf):
        m.cmd_restore_instances(None)
except SystemExit:
    pass
said = buf.getvalue()
ok("die ausgelassene Test-Instanz ruht und wird genannt",
   "web-test" not in STARTED and "DORMANT web-test" in said and "web" in STARTED, (STARTED, said))

print("=== Meldungen (RFC-0080) ===")
with open(m.BACKUP_SCHEDULE_FILE, "w", encoding="utf-8") as f:
    json.dump({"enabled": False}, f)
with open(m.BACKUP_RUN_FILE, "w", encoding="utf-8") as f:
    json.dump({"state": "ok", "finished": "2026-01-01T00:00:00+00:00"}, f)
now = m.notify.now_utc()
ok("„local“ mit ausgeschaltetem Zeitplan: weiterhin „Zeitplan aus“",
   (m._notify_backup_conditions(now)[1] or {}).get("kurz") == "Zeitplan aus")
cli("class", "none", "Testknoten, Daten sind Wegwerfdaten")
ok("„none“ erklärt: kein „Kein aktuelles Backup“", m._notify_backup_conditions(now)[1] is None)

print("=== Stufe 2: Sicherung je Mandant ===")
m.SYSTEMD_DIR = tempfile.mkdtemp(prefix="oaap-systemd-")
SYSTEMCTL = []
m._systemctl = lambda *a, **k: SYSTEMCTL.append(a)
cli("class", "local")
cli("test-instances", "on")
VID, _ = m.tenant_create("verein", "Verein")
reg = m.load_registry()
reg["instances"]["kasse"] = {
    "app_id": "kasse", "app_name": "Kasse", "version": "1.0", "channel": "production",
    "port": 8950, "svc_port": 80, "container": "oaap-app-kasse", "tenant": VID,
    "image": "oaap/kasse:1.0", "build": "", "name": "kasse", "id": m.new_instance_id(),
    "routes": [{"path": "/", "roles": ["user"]}]}
m.save_registry(reg)
kd = os.path.join(m.instance_dir("kasse", reg["instances"]["kasse"]), "storage")
os.makedirs(kd, exist_ok=True)
open(os.path.join(kd, "kasse.txt"), "w").write("Kassenbuch\n")
with open(m._identity_users_path(), "w", encoding="utf-8") as f:
    json.dump([{"username": "joerg", "tenant": DEFAULT},
               {"username": "kassierer", "tenant": VID}], f)


def refused(fn, *a, **k):
    try:
        fn(*a, **k)
    except ValueError as e:
        return str(e)
    return ""


ok("Standard: im Knoten-Backup", m.tenant_backup_class(VID) == "node")
ok("der Knoten-Mandant kann kein eigenes Archiv haben",
   "node's own tenant" in refused(m.backup_tenant_set, DEFAULT, "own"))
ok("„none“ ohne Grund abgelehnt", "say why" in refused(m.backup_tenant_set, VID, "none"))
ok("ein Mandanten-Admin ändert die Art nicht",
   "operator's" in refused(m.backup_tenant_set, VID, "own", role="tenant_admin"))
ok("… und keine Uhrzeit, solange er im Knoten-Backup ist",
   "operator's" in refused(m.backup_tenant_set, VID, at="01:00", role="tenant_admin"))
said = m.backup_tenant_set(VID, "own", at="02:15", keep=2, keep_max=3, who="joerg")
t = m.load_tenants()[VID].get("backup") or {}
ok("eigenes Archiv: Datensatz mit Uhrzeit, Anzahl, Obergrenze",
   m.tenant_backup_class(VID) == "own" and t.get("at") == "02:15" and t.get("keep") == 2
   and t.get("keep_max") == 3, (said, t))
drop = os.path.join(m.SYSTEMD_DIR, f"oaap-tenant-backup@{VID}.timer.d", "when.conf")
ok("ein eigener Zeitgeber: Vorlage, Drop-in mit der Uhrzeit, eingeschaltet",
   os.path.isfile(os.path.join(m.SYSTEMD_DIR, "oaap-tenant-backup@.service"))
   and "OnCalendar=*-*-* 02:15:00" in open(drop).read()
   and ("enable", f"oaap-tenant-backup@{VID}.timer") in SYSTEMCTL, SYSTEMCTL)
ok("das steht im Protokoll des Mandanten",
   any(e.get("action") == "backup.tenant" and "eigenes Archiv" in e.get("detail", "")
       for e in m.read_tenant_log(VID, limit=20)))
ok("der Mandanten-Admin darf Uhrzeit und Anzahl ändern …",
   refused(m.backup_tenant_set, VID, at="04:00", keep=3, role="tenant_admin") == ""
   and m.load_tenants()[VID]["backup"]["at"] == "04:00")
ok("… aber nicht über die Obergrenze",
   "between 1 and 3" in refused(m.backup_tenant_set, VID, keep=4, role="tenant_admin"))

print("=== das Knotenarchiv lässt den Mandanten mit eigenem Archiv aus ===")
DOCKER.clear()


def _run_k(cmd, **kw):
    if cmd and cmd[0] == "docker" and cmd[1] == "ps":
        r = _Empty()
        r.stdout = "oaap-app-web\noaap-app-web-test\noaap-app-kasse\n"
        return r
    return _run(cmd, **kw)


m.run = _run_k
said = cli("create")
arc = newest()
entries = _real_sub(["tar", "-tzf", arc], check=True, text=True,
                    capture_output=True).stdout.split() if arc else []
stopped = [c for c in DOCKER if c[1] == "stop" for c in c[2:]]
ok("seine Daten fehlen im Knotenarchiv, sein Container steht nicht still",
   arc and not any(f"tenants/{VID}/" in e for e in entries) and "oaap-app-kasse" not in stopped,
   (said[-600:], stopped))
mf = json.loads(_real_sub(["tar", "-xzOf", arc, "backup-manifest.json"], check=True,
                          text=True, capture_output=True).stdout) if arc else {}
left = [e for e in mf.get("tenants_excluded") or [] if e["tenant"] == VID]
ok("das Begleitblatt sagt warum: eigenes Archiv",
   left and left[0].get("class") == "own" and "own archive" in left[0]["reason"], left)
ok("die Vollständigkeitsprüfung schlägt nicht an", "does not contain" not in said, said[-400:])
os.remove(arc)

print("=== der Lauf des Mandanten ===")
m.BACKUP_DIR_DEFAULT = tempfile.mkdtemp(prefix="oaap-tenant-arch-")
with open(m.BACKUP_SCHEDULE_FILE, "w", encoding="utf-8") as f:
    json.dump({"enabled": True, "target": m.BACKUP_DIR_DEFAULT}, f)
for i in range(4):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        m.cmd_backup_tenant_run(VID)
    import time as _t
    _t.sleep(1.1)   # one archive per second: the stamp is the name
d = m.tenant_backup_target(VID)
files = sorted(os.listdir(d))
rec = m.tenant_backup_last(VID)
ok("das Archiv liegt neben denen des Knotens, je Mandanten-Id, auf 3 gekürzt",
   d.endswith(os.path.join("tenants", VID)) and len(files) == 3
   and all(f.startswith("oaap-tenant-verein-") for f in files), files)
ok("der Laufbericht sagt ok, mit Größe", rec.get("state") == "ok" and rec.get("bytes"), rec)
cur = m.notify_conditions({})
ok("ein gelungener Lauf: keine Meldung für den Mandanten",
   not any(k.startswith("backup.") for k in cur.get(VID, {})), cur.get(VID))
real_ta = m._tenant_archive
m._tenant_archive = lambda *a, **k: m.die("tar exit 2: No space left on device")
try:
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        m.cmd_backup_tenant_run(VID)
except SystemExit:
    pass
m._tenant_archive = real_ta
rec = m.tenant_backup_last(VID)
ok("ein gescheiterter Lauf: im Bericht mit dem echten Grund, das letzte ok bleibt",
   rec.get("state") == "failed" and "No space" in rec.get("message", "") and rec.get("last_ok"), rec)
with open(m.BACKUP_RUN_FILE, "w", encoding="utf-8") as f:
    json.dump({"state": "failed", "finished": "2026-11-12T03:00:00Z", "message": "Knoten kaputt"}, f)
cur = m.notify_conditions({})
ok("Meldung: das Archiv DES MANDANTEN ist fehlgeschlagen, nicht das des Knotens",
   "No space" in cur[VID]["backup.failed"]["text"] and "Knoten kaputt" not in cur[VID]["backup.failed"]["text"]
   and "Knoten kaputt" in cur[DEFAULT]["backup.failed"]["text"], cur.get(VID))
with open(m.BACKUP_RUN_FILE, "w", encoding="utf-8") as f:
    json.dump({"state": "ok", "finished": m._iso_now()}, f)

print("=== nie zwei Sicherungen zugleich ===")
try:
    import fcntl
    fd = os.open(m.BACKUP_LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    m.BACKUP_LOCK_WAIT = 1
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            m.cmd_backup_tenant_run(VID)
    except SystemExit:
        pass
    os.close(fd)
    rec = m.tenant_backup_last(VID)
    ok("ein Lauf, der die Sperre nicht bekommt, gibt mit Grund auf",
       rec.get("state") == "failed" and "another backup" in rec.get("message", ""), rec)
    m.BACKUP_LOCK_WAIT = 3600
except ImportError:
    print("SKIP  Sperre (kein fcntl auf diesem System)")

print("=== zurück ===")
said = m.backup_tenant_set(VID, "none", reason="Verein sichert selbst", who="joerg")
ok("„keine Sicherung“ mit Grund = das Ausnehmen, Zeitgeber aus",
   m.tenant_backup_class(VID) == "none" and VID in m.excluded_tenants()
   and not os.path.exists(drop) and "backup" not in m.load_tenants()[VID], said)
said = m.backup_tenant_set(VID, "node", who="joerg")
ok("zurück ins Knoten-Backup: Ausnehmen aufgehoben",
   m.tenant_backup_class(VID) == "node" and VID not in m.excluded_tenants(), said)

print()
print("ALL PASS" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
