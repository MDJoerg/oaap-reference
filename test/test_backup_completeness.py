#!/usr/bin/env python3
"""Enthält ein Backup wirklich, was der Knoten hat? (oaap.data.backup)

Diese Datei gibt es wegen eines Fundes vom 2026-09-05 auf oaap-test:
Seit die Instanzdaten unter `tenants/` liegen (RFC-0026, 0.1.59) hat
`oaap backup create` die Registry und die Benutzer gesichert — und
**kein einziges Byte Anwendungsdaten und keine instance.env**. Das
Archiv war 9 KB gross bei 899 MB Nutzdaten, der Befehl meldete Erfolg,
und die Wiederherstellung haette ebenfalls funktioniert: auf eine leere
Plattform.

Wieder derselbe Schnitt wie bei den vier Funden vom 03.09.: Ein
Bezeichner bekommt eine neue Bedeutung, und **eine Stelle liest ihn
weiter in der alten**. Die Sicherung war der fuenfte Leser.

Der Satz, der hier verteidigt wird:

    Vollstaendigkeit wird am ARCHIV gemessen, nicht an der Pfadliste
    im Quelltext. Eine Pfadliste kann richtig aussehen und leer sein.

Braucht kein Docker und keinen Knoten (Docker-Aufrufe sind ersetzt,
tar laeuft echt).

Run: python3 test/test_backup_completeness.py
"""
import json
import os
import subprocess
import sys
import tarfile
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-backup-test-")
OUT = tempfile.mkdtemp(prefix="oaap-backup-out-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, os.path.join(HERE, "..", "platform"))

import appctl as m  # noqa: E402

ok_n = fail_n = 0


def ok(label, cond, detail=""):
    global ok_n, fail_n
    if cond:
        ok_n += 1
        print(f"PASS  {label}")
    else:
        fail_n += 1
        print(f"FAIL  {label} {detail}")


# Docker laeuft hier nicht; tar schon — sonst pruefte der Test seine
# eigene Attrappe statt das Archiv.
_real_run = m.run


def fake_run(cmd, **kw):
    if cmd and cmd[0] == "docker":
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    return _real_run(cmd, **kw)


m.run = fake_run
m.reload_gateway = lambda: None


def capture(fn, *a):
    import contextlib
    import io
    buf = io.StringIO()
    code = 0
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            fn(*a)
    except SystemExit as e:
        code = e.code or 0
    return buf.getvalue(), code


class Args:
    def __init__(self, to):
        self.action = "create"
        self.to = to


# ---------------------------------------------------------------- Aufbau
os.makedirs(os.path.join(DATA, "app"), exist_ok=True)
with open(os.path.join(DATA, "app", ".env"), "w", encoding="utf-8") as f:
    f.write("OAAP_VERSION=9.9.9\nSESSION_SECRET=s\nSETUP_TOKEN=t\nOAAP_HTTP_PORT=80\n")
os.makedirs(os.path.join(DATA, "data", "identity"), exist_ok=True)
with open(os.path.join(DATA, "data", "identity", "users.json"), "w",
          encoding="utf-8") as f:
    json.dump([{"username": "joerg", "roles": ["server_admin"]}], f)

tid = m.ensure_default_tenant()
kunde = m.tenant_create("kunde", "Kunde Meier GmbH")[0]

# Zwei Instanzen in zwei Mandanten -- der Fall, in dem die Luecke
# entstanden ist, hatte genau diese Form. Eine dritte hat noch kein
# Verzeichnis: sie darf die Pruefung nicht ausloesen.
INSTANCES = {
    "demo": {"id": "aaaa11112222", "tenant": tid},
    "kunde-viewer": {"id": "bbbb33334444", "tenant": kunde},
    "frisch": {"id": "cccc55556666", "tenant": tid},
}
reg = {"instances": {}, "retained": {}}
for name, meta in INSTANCES.items():
    reg["instances"][name] = {
        "app_id": "demo", "app_name": "Demo", "version": "1.0",
        "channel": "production", "container": f"oaap-app-{name}",
        "port": 8100, "svc_port": 8080, "routes": [],
        "id": meta["id"], "tenant": meta["tenant"],
        "source": {"kind": "git", "url": "https://example.invalid/x"},
    }
m.save_registry(reg)

for name in ("demo", "kunde-viewer"):
    d = m.instance_dir(name, reg["instances"][name])
    os.makedirs(os.path.join(d, "storage"), exist_ok=True)
    with open(os.path.join(d, "instance.env"), "w", encoding="utf-8") as f:
        f.write(f"OAAP_APP_SECRET=geheim-{name}\n")
    with open(os.path.join(d, "storage", "nutzdaten.txt"), "w",
              encoding="utf-8") as f:
        f.write(f"Die Daten von {name}. Ohne diese Zeile ist alles umsonst.\n")

print("=== die Ablage liegt dort, wo RFC-0026 sie hingelegt hat ===")
ok("die Instanzdaten liegen unter tenants/<mandant>/instances/<id>/",
   "tenants" in m.instance_dir("demo", reg["instances"]["demo"]),
   m.instance_dir("demo", reg["instances"]["demo"]))
ok("und die instance.env liegt dort mit",
   os.path.isfile(m.env_path("demo", reg["instances"]["demo"])))

print("\n=== das Archiv enthaelt sie ===")
out, code = capture(m.cmd_backup, Args(OUT))
ok("das Backup laeuft durch", code == 0, out[-400:])
archives = [f for f in os.listdir(OUT) if f.endswith(".tar.gz")]
ok("und schreibt genau ein Archiv", len(archives) == 1, str(archives))
path = os.path.join(OUT, archives[0])

with tarfile.open(path) as t:
    names = t.getnames()
    body = {}
    for n in names:
        if n.endswith("nutzdaten.txt") or n.endswith("instance.env"):
            body[n] = t.extractfile(n).read().decode()

ok("die Nutzdaten beider Instanzen sind drin",
   sum(1 for n in names if n.endswith("nutzdaten.txt")) == 2,
   str([n for n in names if "nutzdaten" in n]))
ok("und zwar mit Inhalt, nicht nur als Verzeichniseintrag",
   any("Ohne diese Zeile" in v for v in body.values()))
ok("die instance.env beider Instanzen ist drin (die Geheimnisse)",
   sum(1 for n in names if n.endswith("instance.env")) == 2,
   str([n for n in names if "instance.env" in n]))
ok("auch die des ZWEITEN Mandanten -- ein Ganzknoten-Archiv nimmt alle mit",
   any("geheim-kunde-viewer" in v for v in body.values()))
ok("Registry und Benutzer sind weiterhin dabei",
   "apps/registry.json" in names and "data/identity/users.json" in names)
ok("der Mandantenspeicher ist dabei", "apps/tenants.json" in names)
ok("das Knotenprofil ist NICHT dabei (es beschreibt die Maschine)",
   "apps/node.json" not in names)
ok("das Archiv ist deutlich groesser als eine leere Huelle",
   os.path.getsize(path) > 400, f"{os.path.getsize(path)} Bytes")

print("\n=== die Selbstpruefung faengt genau den Fund vom 05.09. ===")
# Ein Archiv, das gebaut wird wie vor dem Fix: ohne tenants/.
lueckenhaft = os.path.join(OUT, "wie-vor-dem-fix.tar.gz")
subprocess.run(["tar", "-czf", lueckenhaft, "-C", DATA,
                "app/.env", "apps", "data/identity"], check=True)
fehlend = m._backup_missing_instances(lueckenhaft, reg)
ok("ein Archiv ohne tenants/ wird als unvollstaendig erkannt",
   set(fehlend) == {"demo", "kunde-viewer"}, str(fehlend))
ok("die Instanz ohne Verzeichnis wird NICHT gemeldet",
   "frisch" not in fehlend, str(fehlend))
ok("das echte Archiv besteht dieselbe Pruefung",
   m._backup_missing_instances(path, reg) == [],
   str(m._backup_missing_instances(path, reg)))

print("\n=== und ein unvollstaendiges Archiv wird nicht geschrieben ===")
# Der Weg, auf dem die Luecke entstanden ist, genau nachgestellt: die
# Daten liegen da, die PFADLISTE vergisst sie. Deshalb wird hier dem
# tar-Aufruf das Verzeichnis entzogen und sonst nichts veraendert --
# haette man stattdessen die Daten versteckt, pruefte der Test den
# harmlosen Fall (es gibt nichts zu sichern) statt den gefaehrlichen
# (es gibt etwas, und niemand merkt, dass es fehlt).
def vergesslicher_tar(cmd, **kw):
    if cmd and cmd[0] == "tar" and "-czpf" in cmd:
        cmd = [c for c in cmd if c != "tenants"]
    return fake_run(cmd, **kw)


m.run = vergesslicher_tar
try:
    out, code = capture(m.cmd_backup, Args(OUT))
finally:
    m.run = fake_run
ok("der Lauf schlaegt fehl statt still ein leeres Archiv abzulegen",
   code != 0, f"code={code} {out[-300:]}")
ok("und sagt, welche Instanzen fehlen",
   "demo" in out and "kunde-viewer" in out, out[-400:])
ok("es bleibt keine halbe Datei liegen",
   not any(f.endswith(".tmp") for f in os.listdir(OUT)), str(os.listdir(OUT)))
ok("und kein zweites Archiv",
   len([f for f in os.listdir(OUT) if f.startswith("oaap-backup-")]) == 1,
   str(os.listdir(OUT)))

print("\n=== das Mandanten-Protokoll faehrt mit ===")
# Gefunden vom ersten echten Wiederherstellungsversuch (05.09.): Der
# Knoten kam vollstaendig zurueck und sagte "No entries yet". Das
# Protokoll ist das Gegengewicht zu "ein server_admin darf hier alles"
# -- eine Wiederherstellung, die es still fallen laesst, schenkt dem
# Betreiber ein weisses Blatt.
m.audit_tenant("tenant.create", kunde, "kunde", who="joerg",
               role="server_admin")
out, code = capture(m.cmd_backup, Args(OUT))
neu = sorted(f for f in os.listdir(OUT) if f.startswith("oaap-backup-"))[-1]
with tarfile.open(os.path.join(OUT, neu)) as t:
    namen = t.getnames()
    log = next((t.extractfile(n).read().decode() for n in namen
                if n.endswith("tenant-log.jsonl")), "")
ok("das Protokoll ist im Archiv",
   any(n.endswith("tenant-log.jsonl") for n in namen),
   str([n for n in namen if "audit" in n]))
ok("mit dem Eintrag, wer was getan hat",
   "tenant.create" in log and "joerg" in log, log[:200])

print("\n=== und ein Archiv ohne Protokoll wird nicht geschrieben ===")


def vergesslicher_tar2(cmd, **kw):
    if cmd and cmd[0] == "tar" and "-czpf" in cmd:
        cmd = [c for c in cmd if c != "data/audit"]
    return fake_run(cmd, **kw)


m.run = vergesslicher_tar2
try:
    out, code = capture(m.cmd_backup, Args(OUT))
finally:
    m.run = fake_run
ok("der Lauf schlaegt fehl", code != 0, f"code={code}")
ok("und nennt das Protokoll beim Namen",
   "tenant audit log" in out, out[-300:])

print(f"\n{ok_n} bestanden, {fail_n} fehlgeschlagen")
print("ALLE PRUEFUNGEN BESTANDEN" if not fail_n else "FEHLGESCHLAGEN")
sys.exit(1 if fail_n else 0)
