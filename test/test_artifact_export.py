#!/usr/bin/env python3
"""Das aufbewahrte Paket wieder herausbekommen (oaap.apps.runtime 2.14).

Zwei Dinge stehen hier, und das erste ist ein Fehler, den erst der
laufende Knoten gezeigt hat:

**Die Pakete waren im Portal unsichtbar.** Bis 0.1.75 las die
Instanzseite `/apps-registry/<key>/artifacts` -- den Ort, an dem die
Pakete lagen, bevor RFC-0026 die Instanzdaten nach
`tenants/<tid>/instances/<iid>/` verschoben hat. Das Portal hat dort
keinen Mount und darf keinen bekommen (in dem Baum liegt die
instance.env jedes Kunden). Also listete das Verzeichnis stillschweigend
nichts, die Karte „Hochgeladene Pakete" verschwand -- und mit ihr
„Erneut ausrollen", „Hierauf zurück" und „Löschen". Auf oaapx01 am
05.09.2026 gefunden, sieben betroffene Instanzen.

    Wer einen Bezeichner umbaut, muss alle seine Leser aufzaehlen.

Das ist derselbe Fehler wie die stehengebliebenen Mounts (0.1.71) und
das fehlende Mandantenprotokoll im Archiv (0.1.72). Deshalb prueft diese
Datei den Index gegen den ECHTEN Ort, nicht gegen eine Annahme.

**Und der Download selbst.** Der Knoten gibt frei, nicht das Portal --
ein Lesen ueber die Mandantengrenze wird dort entschieden, wo die
Registry gilt, und dieselbe Stelle schreibt die Protokollzeile.

Aufruf: python3 test/test_artifact_export.py   (kein Docker, kein Knoten)
"""
import importlib
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "platform"))

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:400]}")


DATA = tempfile.mkdtemp(prefix="oaap-export-test-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.modules.pop("appctl", None)
a = importlib.import_module("appctl")
a.DATA_DIR = DATA
a.APPS_DIR = os.path.join(DATA, "apps")
a.REGISTRY = os.path.join(a.APPS_DIR, "registry.json")
a.TENANTS_DIR = os.path.join(DATA, "tenants")
a.TENANTS_FILE = os.path.join(a.APPS_DIR, "tenants.json")
a.ARTIFACT_INDEX = os.path.join(a.APPS_DIR, "artifacts.json")
a.SPOOL_DIR = os.path.join(DATA, "data", "deploy-spool")
os.makedirs(a.APPS_DIR, exist_ok=True)

TID = "11111111-1111-1111-1111-111111111111"
CLS = "22222222-2222-2222-2222-222222222222"
with open(a.TENANTS_FILE, "w", encoding="utf-8") as f:
    json.dump({"tenants": {TID: {"label": "default"},
                           CLS: {"label": "cls"}}}, f)

REG = {"instances": {
    "bdt-hub": {"id": "aaaa0001", "tenant": TID, "channel": "production",
                "app_id": "bdt-hub", "version": "0.20.2",
                "source": {"kind": "artifact", "stored": "0.20.2-abc123def456.zip"}},
    "cls-viewer": {"id": "bbbb0002", "tenant": CLS, "channel": "test",
                   "app_id": "viewer", "version": "1.0",
                   "source": {"kind": "artifact", "stored": "1.0-fff000111222.zip"}},
    "open-webui": {"id": "cccc0003", "tenant": TID, "channel": "production",
                   "app_id": "open-webui", "version": "1",
                   "source": {"kind": "store", "url": "https://example.invalid"}},
}}


def put(name, fn, body=b"PK\x03\x04 nicht wirklich ein zip", age=0):
    """Ein Paket dort ablegen, wo es SEIT RFC-0026 liegt.

    `age` in Sekunden, damit „das neueste zuerst" wirklich an der Zeit
    haengt und nicht daran, wie das Dateisystem das Verzeichnis
    auflistet -- der Name allein wuerde hier zufaellig richtig liegen.
    """
    inst = REG["instances"][name]
    d = a.artifact_dir(name, inst)
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, fn)
    with open(p, "wb") as f:
        f.write(body)
    if age:
        t = os.path.getmtime(p) - age
        os.utime(p, (t, t))
    return p


put("bdt-hub", "0.20.2-abc123def456.zip", b"x" * 4096)
put("bdt-hub", "0.20.1-000111222333.zip", b"y" * 2048, age=3600)
put("cls-viewer", "1.0-fff000111222.zip", b"z" * 1024)

print("")
print("Die Pakete liegen im Mandantenbaum -- nicht mehr unter apps/")

ok("der alte Ort ist leer",
   not os.path.isdir(os.path.join(a.APPS_DIR, "bdt-hub", "artifacts")),
   "genau deshalb sah das Portal nichts")
ok("der neue Ort traegt die Identitaeten, nicht die Namen",
   a.artifact_dir("bdt-hub", REG["instances"]["bdt-hub"])
   == os.path.join(DATA, "tenants", TID, "instances", "aaaa0001", "artifacts"),
   a.artifact_dir("bdt-hub", REG["instances"]["bdt-hub"]))

print("")
print("Der Knoten schreibt einen Index dorthin, wo das Portal liest")

a.artifact_index_write(REG)
with open(a.ARTIFACT_INDEX, encoding="utf-8") as f:
    idx = json.load(f)
inst_idx = idx["instances"]

ok("die Datei liegt in apps/, dem einzigen Mount des Portals",
   os.path.isfile(a.ARTIFACT_INDEX), a.ARTIFACT_INDEX)
ok("sie nennt beide Pakete der Instanz",
   [x["file"] for x in inst_idx["bdt-hub"]]
   == ["0.20.2-abc123def456.zip", "0.20.1-000111222333.zip"],
   inst_idx.get("bdt-hub"))
ok("das neueste zuerst",
   inst_idx["bdt-hub"][0]["file"] == "0.20.2-abc123def456.zip", inst_idx)
ok("mit Groesse", inst_idx["bdt-hub"][0]["bytes"] == 4096, inst_idx)
ok("und Empfangszeit", "T" in inst_idx["bdt-hub"][0]["received"], inst_idx)
ok("auch die Instanz des anderen Mandanten steht drin",
   "cls-viewer" in inst_idx, inst_idx)
ok("eine Instanz aus dem Store gar nicht -- sie hat kein Paket",
   "open-webui" not in inst_idx, inst_idx)

print("")
print("Der Index ist eine Ansicht, keine Wahrheit")

os.remove(os.path.join(a.artifact_dir("bdt-hub", REG["instances"]["bdt-hub"]),
                       "0.20.1-000111222333.zip"))
stale = json.load(open(a.ARTIFACT_INDEX, encoding="utf-8"))
ok("eine veraltete Zeile bleibt erst einmal stehen",
   len(stale["instances"]["bdt-hub"]) == 2,
   "das ist in Ordnung -- der Download fragt den Knoten noch einmal")
try:
    a.artifact_export(REG["instances"]["bdt-hub"], "bdt-hub",
                      "0.20.1-000111222333.zip", "r1")
    got = ""
except ValueError as e:
    got = str(e)
ok("und der Knoten lehnt sie ab statt eine Datei zu erfinden",
   "nicht" in got or "not a package" in got, got)
a.artifact_index_write(REG)
ok("nach dem naechsten Schreiben ist sie weg",
   len(json.load(open(a.ARTIFACT_INDEX, encoding="utf-8"))
       ["instances"]["bdt-hub"]) == 1)

print("")
print("Ein Dateiname ist kein Pfad")

for evil in ("../../../../etc/passwd", "..\\\\windows\\\\win.ini",
             "/etc/shadow", "0.20.2-abc123def456.zip/../x"):
    try:
        a.artifact_export(REG["instances"]["bdt-hub"], "bdt-hub", evil, "r2")
        refused = False
    except (ValueError, OSError):
        refused = True
    ok(f"abgelehnt: {evil[:28]}", refused,
       "der Name wird gegen die Liste geprueft, nie an ein Verzeichnis geklebt")

print("")
print("Die Freigabe legt genau eine Datei in den Spool")

p = a.artifact_export(REG["instances"]["bdt-hub"], "bdt-hub",
                      "0.20.2-abc123def456.zip", "req-42")
handed = os.path.join(a.SPOOL_DIR, "exports", "req-42.zip")
ok("sie meldet den Paketnamen zurueck", p == "0.20.2-abc123def456.zip", p)
ok("die Datei liegt bereit", os.path.isfile(handed), handed)
ok("und es sind wirklich dieselben Bytes",
   open(handed, "rb").read()
   == open(os.path.join(a.artifact_dir("bdt-hub", REG["instances"]["bdt-hub"]),
                        "0.20.2-abc123def456.zip"), "rb").read(),
   "sonst waere 'genau diese Bytes' eine Behauptung")

print("")
print("Und das Original bleibt, wenn der Spool aufgeraeumt wird")

os.remove(handed)
ok("das aufbewahrte Paket ist unversehrt",
   os.path.isfile(os.path.join(
       a.artifact_dir("bdt-hub", REG["instances"]["bdt-hub"]),
       "0.20.2-abc123def456.zip")),
   "der Spool haelt nur einen zweiten Verweis, keine zweite Wahrheit")

print("")
print("Ein liegengebliebener Export wird beim naechsten Mal weggeraeumt")

orphan = os.path.join(a.SPOOL_DIR, "exports", "gestorben.zip")
with open(orphan, "wb") as f:
    f.write(b"vergessen")
old = a.time.time() - a.EXPORT_TTL_SECONDS - 60
os.utime(orphan, (old, old))
a.artifact_export(REG["instances"]["bdt-hub"], "bdt-hub",
                  "0.20.2-abc123def456.zip", "req-43")
ok("der alte ist weg", not os.path.exists(orphan),
   "sonst liegt ein Paket fuer immer im Spool")
ok("der neue liegt da",
   os.path.isfile(os.path.join(a.SPOOL_DIR, "exports", "req-43.zip")))

print("")
print("Der Download steht im Mandantenprotokoll")

ok("die Handlung ist als protokollpflichtig eingetragen",
   a.TENANT_AUDITED.get("artifact-export") == "instance.export",
   a.TENANT_AUDITED.get("artifact-export"))
ok("und ist von einer Loeschung unterscheidbar",
   a.TENANT_AUDITED["artifact-export"] != a.TENANT_AUDITED["artifact-remove"],
   "sonst liest der Mandant 'jemand hat etwas mit einem Paket gemacht'")

print("")
print(f"{'ALLE PRUEFUNGEN BESTANDEN' if not fails else str(fails) + ' FEHLGESCHLAGEN'}")
sys.exit(1 if fails else 0)
