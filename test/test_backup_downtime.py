#!/usr/bin/env python3
"""Wie lange stehen die Apps? (RFC-0029 D3)

Gemessen auf oaapx01 am 05.09.2026: **487 Sekunden Ausfall** fuer 8,0 GB
-- und fast alles davon war Komprimierung. Kopieren ist plattengebunden
und schnell, Komprimieren ist rechengebunden und langsam, und
Komprimieren braucht die Daten nicht stillstehend.

Der Satz, der hier verteidigt wird:

    Die Container laufen wieder, BEVOR komprimiert wird.

Das ist keine Frage der Geschwindigkeit, sondern der Reihenfolge --
deshalb prueft diese Datei die Reihenfolge und nicht die Dauer. Eine
Messung waere auf einer Testmaschine ohnehin bedeutungslos; die
Reihenfolge ist es nie.

Dazu die zweite Haelfte von D3, die D1 spaeter braucht: Was der Ausfall
tatsaechlich gekostet hat, wird aufgeschrieben. Das Portal soll neben
dem Zeitfeld die zuletzt gemessene Dauer DIESES Knotens nennen -- eine
Zahl aus einem Handbuch waere die Maschine von jemand anderem.

Braucht kein Docker und keinen Knoten.

Run: python3 test/test_backup_downtime.py
"""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-downtime-test-")
OUT = tempfile.mkdtemp(prefix="oaap-downtime-out-")
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


# Das Protokoll der Ereignisse in ihrer Reihenfolge -- der ganze Test
# haengt daran, was hier in welcher Zeile steht.
EVENTS = []
_real_run = m.run


def fake_run(cmd, **kw):
    if cmd and cmd[0] == "docker":
        if cmd[1] == "ps":
            return subprocess.CompletedProcess(cmd, 0, stdout="c1 c2\n", stderr="")
        EVENTS.append("docker " + cmd[1])
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    if cmd and cmd[0] == "tar" and any(a.startswith("-c") for a in cmd):
        EVENTS.append("tar -z" if any("z" in a for a in cmd
                                      if a.startswith("-c")) else "tar")
    if cmd and cmd[0] in ("gzip", "pigz"):
        EVENTS.append("komprimieren")
    return _real_run(cmd, **kw)


m.run = fake_run

# `docker start` laeuft absichtlich an m.run vorbei (direkt ueber
# subprocess, damit ein Fehlschlag beim Neustart den Lauf nicht
# abbricht) -- also muss auch dieser Weg mitgeschrieben werden. Das
# Original wird vorher festgehalten, sonst ruft die Attrappe sich selbst.
_real_sub_run = subprocess.run


def fake_sub_run(cmd, **kw):
    if cmd and cmd[0] == "docker":
        EVENTS.append("docker " + cmd[1])
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    return _real_sub_run(cmd, **kw)


m.subprocess.run = fake_sub_run


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


os.makedirs(os.path.join(DATA, "app"), exist_ok=True)
with open(os.path.join(DATA, "app", ".env"), "w", encoding="utf-8") as f:
    f.write("OAAP_VERSION=9.9.9\nOAAP_HTTP_PORT=80\n")
os.makedirs(os.path.join(DATA, "data", "identity"), exist_ok=True)
with open(os.path.join(DATA, "data", "identity", "users.json"), "w",
          encoding="utf-8") as f:
    json.dump([{"username": "joerg", "roles": ["server_admin"]}], f)
tid = m.ensure_default_tenant()
reg = {"instances": {"demo": {
    "app_id": "d", "app_name": "Demo", "version": "1", "channel": "test",
    "container": "oaap-app-demo", "port": 8100, "svc_port": 80,
    "id": "aaaa11112222", "tenant": tid,
    "source": {"kind": "git", "url": "https://example.invalid/x"}}},
    "retained": {}}
m.save_registry(reg)
d = m.instance_dir("demo", reg["instances"]["demo"])
os.makedirs(os.path.join(d, "storage"), exist_ok=True)
with open(os.path.join(d, "storage", "nutzdaten.txt"), "w", encoding="utf-8") as f:
    f.write("Daten, die stillstehen muessen waehrend sie kopiert werden.\n")

print("=== die Reihenfolge ===")
out, code = capture(m.cmd_backup, Args(OUT))
ok("das Backup laeuft durch", code == 0, out[-300:])
print("   Ablauf: " + " -> ".join(EVENTS))

ok("die Container werden gestoppt", "docker stop" in EVENTS, str(EVENTS))
ok("dann wird kopiert", "tar" in EVENTS, str(EVENTS))
ok("das Kopieren komprimiert NICHT (kein -z)", "tar -z" not in EVENTS, str(EVENTS))
ok("dann laufen die Container wieder", "docker start" in EVENTS, str(EVENTS))
ok("und ERST DANN wird komprimiert -- das ist der ganze Punkt von D3",
   "komprimieren" in EVENTS
   and EVENTS.index("docker start") < EVENTS.index("komprimieren"),
   str(EVENTS))
ok("gestoppt wird vor dem Kopieren",
   EVENTS.index("docker stop") < EVENTS.index("tar"), str(EVENTS))

print("\n=== was der Ausfall gekostet hat, wird aufgeschrieben ===")
run = m.last_backup_run()
ok("es gibt einen Eintrag", run is not None)
ok("mit der Ausfallzeit getrennt von der Gesamtdauer",
   run and "downtime_seconds" in run and "total_seconds" in run, str(run))
ok("die Ausfallzeit ist nicht groesser als die Gesamtdauer",
   run and run["downtime_seconds"] <= run["total_seconds"], str(run))
ok("er nennt das Archiv und seine Groesse",
   run and run["archive"].endswith(".tar.gz") and run["bytes"] > 0, str(run))
ok("und die Datei liegt neben der Registry, wo das Portal sie lesen kann",
   os.path.isfile(m.BACKUP_RUN_FILE) and m.APPS_DIR in m.BACKUP_RUN_FILE,
   m.BACKUP_RUN_FILE)
# Die Rechte werden auf Windows nicht durchgesetzt; geprueft wird
# deshalb, dass die Datei NICHT auf 600 steht -- das Portal liest sie.
ok("sie ist nicht auf 600 gesperrt -- das Portal soll sie lesen",
   oct(os.stat(m.BACKUP_RUN_FILE).st_mode)[-3:] != "600",
   oct(os.stat(m.BACKUP_RUN_FILE).st_mode))

print("\n=== die Ansage vorher stimmt mit dem ueberein, was passiert ===")
ok("der Befehl kuendigt an, dass nur fuer das Kopieren gestoppt wird",
   "COPY only" in out, out[:400])
ok("und meldet hinterher beide Zahlen",
   "app downtime" in out and "total)" in out,
   out[out.find("Backup written"):][:200])
ok("dazwischen sagt er, dass die Apps wieder laufen",
   "running again after" in out, out[:600])

print("\n=== nach einem Fehlschlag bleibt keine halbe Datei liegen ===")
vorher = set(os.listdir(OUT))


def tar_scheitert(cmd, **kw):
    if cmd and cmd[0] == "tar" and any(a.startswith("-c") for a in cmd):
        raise subprocess.CalledProcessError(2, cmd, stderr="voll")
    return fake_run(cmd, **kw)


EVENTS.clear()
m.run = tar_scheitert
try:
    out2, code2 = capture(m.cmd_backup, Args(OUT))
finally:
    m.run = fake_run
ok("der Lauf schlaegt fehl", code2 != 0, f"code={code2}")
ok("die Container laufen trotzdem wieder -- ein Fehlschlag darf den "
   "Knoten nicht gestoppt zuruecklassen",
   "docker start" in EVENTS, str(EVENTS))
neu = set(os.listdir(OUT)) - vorher
ok("und es bleibt nichts liegen, was nach einem Backup aussieht",
   not neu, str(neu))

print(f"\n{ok_n} bestanden, {fail_n} fehlgeschlagen")
print("ALLE PRUEFUNGEN BESTANDEN" if not fail_n else "FEHLGESCHLAGEN")
sys.exit(1 if fail_n else 0)
