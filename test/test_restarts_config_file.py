#!/usr/bin/env python3
"""Portal 0.3.61 (2.19): restart history, configuration file, search.

Asked by Jörg on 2026-11-24: an instance had restarted and nobody could say
when or why. He decided:
  - restarts from outside the platform count too;
  - the file is JSON without secrets;
  - an upload fills the form without saving;
  - the search covers the instance.

  1. host: the journal (appctl.restart_record / restart_detect), every
     caller of recreate_instance_containers names a cause
  2. portal: restart_view reads it in the page's words
  3. portal: config_file -- export without secrets, upload into the form,
     search
  4. the doors: routes with require_instance_admin, template anchors

Needs PyYAML (appctl); no Docker, no node.

    py -3 test/test_restarts_config_file.py
"""
import importlib
import json
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PLATFORM = os.path.join(HERE, "..", "platform")
PORTAL = os.path.join(PLATFORM, "services", "portal")
DATA = tempfile.mkdtemp(prefix="oaap-restarts-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, PLATFORM)
sys.path.insert(0, PORTAL)

fails = 0


def ok(what, cond, info=""):
    global fails
    print(("PASS  " if cond else "FAIL  ") + what + ("" if cond else f"   {str(info)[:600]}"))
    if not cond:
        fails += 1


APPCTL = open(os.path.join(PLATFORM, "appctl.py"), encoding="utf-8").read().replace("\r\n", "\n")
SRC = open(os.path.join(PORTAL, "app.py"), encoding="utf-8").read().replace("\r\n", "\n")

print("=== Host: das Journal ===")
try:
    a = importlib.import_module("appctl")
except ImportError as e:
    a = None
    print(f"SKIP  appctl: {e}")
if a is not None:
    a.RESTART_BY = "anna"
    a.restart_record("t1-shop", "config", "SHOP_TITLE")
    j = a.restart_journal("t1-shop")
    ok("eine Zeile mit Zeit, Ursache, Hinweis, Person",
       len(j) == 1 and j[0]["cause"] == "config" and j[0]["detail"] == "SHOP_TITLE"
       and j[0]["by"] == "anna" and j[0]["at"], j)
    a.restart_record("t1-shop", "erfunden")
    ok("KOEDER eine unbekannte Ursache wird nicht wörtlich übernommen",
       a.restart_journal("t1-shop")[-1]["cause"] == "setting")
    a.RESTART_BY = ""
    os.environ.pop("SUDO_USER", None)
    os.environ["SUDO_USER"] = "joerg"
    ok("Kommandozeile mit sudo: der Mensch dahinter", a.restart_actor() == "joerg (Kommandozeile)")
    os.environ.pop("SUDO_USER", None)
    ok("ohne sudo und ohne Terminal: der Knoten (Zeitgeber)",
       a.restart_actor() in ("Knoten", "root (Kommandozeile)"))
    p = a._restart_file("t1-shop")
    ok("die Datei liegt neben der Registry, für das Portal lesbar (0644)",
       p.startswith(os.path.join(DATA, "apps", "restarts"))
       and (os.name == "nt" or (os.stat(p).st_mode & 0o777) == 0o644), p)
    ok("KOEDER ein Name mit Pfadzeichen bleibt im Verzeichnis",
       os.path.dirname(a._restart_file("../../etc/x")) == a.RESTARTS_DIR)
    for k in range(a.RESTART_KEEP + 5):
        a.restart_record("t1-many", "manual", str(k))
    jm = a.restart_journal("t1-many")
    ok(f"behält die letzten {a.RESTART_KEEP}", len(jm) == a.RESTART_KEEP and jm[-1]["detail"] == str(a.RESTART_KEEP + 4))

    print("\n=== Host: Starts von außen (Minutentakt) ===")
    inst = {"app_id": "shop", "image": "x", "container": "oaap-app-t1-web"}
    svc = lambda i: [{"service": "", "container": "oaap-app-t1-web"}]   # noqa: E731
    insts = {"t1-web": inst}

    def st(started, restarts=0):
        return {"oaap-app-t1-web": {"state": "running", "started": started, "restarts": restarts,
                                    "exit_code": 0, "oom": False, "health": ""}}

    a.restart_detect(insts, st("2026-11-24T08:00:00.123456789Z"), svc, boot=1)
    ok("erster Blick auf einen Container: keine Zeile", a.restart_journal("t1-web") == [])
    a.restart_detect(insts, st("2026-11-24T08:00:00.123456789Z"), svc, boot=1)
    ok("unveränderte Startzeit: keine Zeile", a.restart_journal("t1-web") == [])
    a.restart_detect(insts, st("2026-11-24T09:00:00.5Z", 1), svc, boot=1)
    j = a.restart_journal("t1-web")
    ok("Neustartzähler gestiegen: Absturz", len(j) == 1 and j[0]["cause"] == "crash"
       and j[0]["at"].startswith("2026-11-24T09:00:00"), j)
    a.restart_detect(insts, st("2026-11-24T09:01:00Z", 2), svc, boot=1)
    j = a.restart_journal("t1-web")
    ok("eine Absturzschleife zählt auf derselben Zeile", len(j) == 1 and j[0].get("count") == 2, j)
    boot = int(a._restart_ts("2026-11-24T09:59:00+00:00"))
    a.restart_detect(insts, st("2026-11-24T10:00:00Z", 0), svc, boot=boot)
    ok("nach dem Booten des Knotens: Knoten-Neustart", a.restart_journal("t1-web")[-1]["cause"] == "node")
    a.restart_detect(insts, st("2026-11-24T11:00:00Z", 0), svc, boot=boot)
    ok("sonst: von außen (docker restart)", a.restart_journal("t1-web")[-1]["cause"] == "external")
    a.restart_record("t1-web", "deploy", "Version 2", at="2026-11-24T11:59:00+00:00")
    n = len(a.restart_journal("t1-web"))
    a.restart_detect(insts, st("2026-11-24T12:00:30Z", 0), svc, boot=boot)
    ok("ein Start, den eine Plattform-Zeile erklärt: keine zweite Zeile",
       len(a.restart_journal("t1-web")) == n)
    a.restart_detect(insts, st("2026-11-24T13:00:00Z", 0), svc, boot=boot)
    ok("KOEDER eine Zeile von außen erklärt nichts: der nächste fremde Start bekommt eine Zeile",
       len(a.restart_journal("t1-web")) == n + 1)
    # measured on oaap-test: a manual restart, its start seen, then a
    # `docker restart` 70 s later -- the manual line must not explain it too
    a.restart_record("t1-web", "manual", at="2026-11-24T14:00:00+00:00")
    a.restart_detect(insts, st("2026-11-24T14:00:01Z", 0), svc, boot=boot)
    n2 = len(a.restart_journal("t1-web"))
    a.restart_detect(insts, st("2026-11-24T14:01:10Z", 0), svc, boot=boot)
    j = a.restart_journal("t1-web")
    ok("KOEDER eine Plattform-Zeile erklärt nur den ersten Start nach ihr (docker restart 70 s später)",
       len(j) == n2 + 1 and j[-1]["cause"] == "external", [r["cause"] for r in j[-3:]])
    ok("Zeitstempel: Docker (Nanosekunden, Z) und ISO mit Versatz lesen gleich",
       a._restart_ts("2026-11-24T10:00:00.999999999Z") == a._restart_ts("2026-11-24T11:00:00+01:00"))

print("\n=== Host: jede Tür nennt ihre Ursache ===")
calls = [m.start() for m in re.finditer(r"(?<!def )recreate_instance_containers\(", APPCTL)]
bad = []
for c in calls:
    line_start = APPCTL.rfind("\n", 0, c) + 1
    if APPCTL[line_start:c].lstrip().startswith("#"):
        continue
    i, depth = APPCTL.index("(", c) + 1, 1
    while depth:
        depth += {"(": 1, ")": -1}.get(APPCTL[i], 0)
        i += 1
    if "cause=" not in APPCTL[c:i]:
        bad.append(APPCTL[c:i][:80])
ok(f"alle {len(calls)} Aufrufe von recreate_instance_containers geben cause= mit", not bad, bad)
fn = APPCTL[APPCTL.index("def recreate_instance_containers("):]
fn = fn[:fn.index("\ndef ", 10)]
ok("die eine Funktion schreibt die Zeile vor dem ersten docker rm, nicht im Standby",
   'restart_record(name, cause or "setting", detail)' in fn
   and fn.index("restart_record(name") < fn.index('"docker", "rm", "-f"')
   and 'if stored.get("state") != "standby":' in fn)
ok("Spool: die angemeldete Person steht hinter der Zeile",
   "RESTART_BY = actor or (" in APPCTL)
ok("Sicherungen (Mandant, Knoten) schreiben ihre Zeile vor dem Anhalten",
   'restart_record_running(list(mine), "backup"' in APPCTL
   and 'restart_record(_n, "backup", "Sicherung des Knotens")' in APPCTL)
ok("Aufwecken aus dem Standby (docker start) schreibt seine Zeile",
   'restart_record(name, "wake", "aus dem Standby", by=who)' in APPCTL)
ok("der Minutentakt fragt restart_detect nur, wenn Docker antwortete",
   "if runtime_ok:\n            restart_detect(instances, states, services_of)" in APPCTL)

print("\n=== Portal: der Verlauf in Worten der Seite ===")
import restart_view as rv  # noqa: E402
RD = os.path.join(DATA, "apps", "restarts")
rows = rv.read(RD, "t1-web")
ok("neueste zuerst, mit Bezeichnung", rows and rows[0]["label"] == rv.CAUSE_LABELS[rows[0]["cause"]]
   and rows[-1]["cause"] == "crash", [r["cause"] for r in rows])
ok("Starts von außen sind markiert, ohne Person", all(r["outside"] == (r["cause"] in rv.OUTSIDE) for r in rows)
   and all(r["by"] == "—" for r in rows if r["outside"]))
ok("die Absturzschleife sagt, wie oft", "2-mal bis" in rows[-1]["detail"], rows[-1])
ok("eine Instanz ohne Journal: leer, kein Fehler", rv.read(RD, "gibt-es-nicht") == [])
ok("Überblick: ein Satz mit Zeit und Ursache", rv.last_sentence(rows).startswith(rows[0]["when"]))
with open(os.path.join(RD, "t1-bad.jsonl"), "w", encoding="utf-8") as f:
    f.write("kein json\n{\"at\": \"2026-11-24T10:00:00+00:00\", \"cause\": \"manual\", \"by\": \"x\"}\n[]\n")
ok("KOEDER eine kaputte Zeile kostet nur sich selbst", len(rv.read(RD, "t1-bad")) == 1)

print("\n=== Portal: die Konfigurationsdatei ===")
import config_file as cf  # noqa: E402
ROWS = [
    {"key": "SHOP_TITLE", "label": "Titel", "secret": False, "multiline": False, "file": False, "stored": "Laden"},
    {"key": "SHOP_HOSTS", "label": "Erlaubte Hosts", "secret": False, "multiline": True, "file": False,
     "stored": "a.example;b.example"},
    # the page's rows never carry a secret value -- this one does, as a bait
    {"key": "SHOP_TOKEN", "label": "API-Token", "secret": True, "multiline": False, "file": False,
     "stored": "LEAK-123"},
    {"key": "SHOP_CERT", "label": "Zertifikat", "secret": False, "multiline": False, "file": True, "stored": "Q0VSVA=="},
]
doc = cf.export("t1-shop", {"app_id": "shop", "version": "1.2"}, ROWS, "2026-11-24T10:00:00+00:00")
text = json.dumps(doc)
ok("Kopf: schema, instance, app_id, version, exported",
   doc["schema"] == cf.SCHEMA and doc["instance"] == "t1-shop" and doc["app_id"] == "shop"
   and doc["version"] == "1.2" and doc["exported"])
ok("Werte: Text als Text, Liste als Liste", doc["values"] == {"SHOP_TITLE": "Laden",
                                                            "SHOP_HOSTS": ["a.example", "b.example"]}, doc)
ok("KOEDER kein vertraulicher Wert in der Datei, auch wenn die Zeile einen trüge",
   "LEAK-123" not in text and "SHOP_TOKEN" not in doc["values"])
ok("ausgelassen mit Grund: secret, file", {"key": "SHOP_TOKEN", "reason": "secret"} in doc["omitted"]
   and {"key": "SHOP_CERT", "reason": "file"} in doc["omitted"] and "Q0VSVA" not in text)

vals, err = cf.parse(json.dumps(doc).encode("utf-8"))
typed, changed, unknown = cf.to_form(ROWS, vals)
ok("die eigene Datei zurück: nichts geändert", changed == [] and unknown == [], (changed, unknown))
doc2 = json.loads(text)
doc2["values"].update({"SHOP_TITLE": "Neuer Laden", "SHOP_HOSTS": ["a.example", "c.example"],
                       "SHOP_TOKEN": "neu-geheim", "SHOP_CERT": "eingeschmuggelt", "FREMD": "x"})
vals, err = cf.parse(json.dumps(doc2).encode("utf-8"))
typed, changed, unknown = cf.to_form(ROWS, vals)
ok("geänderte Werte markiert, Liste als Zeilen fürs Textfeld",
   set(changed) == {"SHOP_TITLE", "SHOP_HOSTS", "SHOP_TOKEN"}
   and typed["SHOP_HOSTS"] == "a.example\nc.example", (changed, typed))
ok("ein Geheimnis aus der Datei geht ins Feld (schreibend)", typed.get("SHOP_TOKEN") == "neu-geheim")
ok("KOEDER ein Dateifeld wird aus der JSON-Datei nicht gefüllt, und genannt",
   "SHOP_CERT" not in typed and "SHOP_CERT" in unknown)
ok("ein Schlüssel, den die App nicht kennt: übergangen und genannt", "FREMD" in unknown and "FREMD" not in typed)
v3, _ = cf.parse(b'{"values": {"SHOP_TOKEN": "", "SHOP_HOSTS": "x.example;y.example"}}')
t3, c3, _ = cf.to_form(ROWS, v3)
ok("leeres Geheimnis = behalten; Listenform mit ';' wird zu Zeilen",
   "SHOP_TOKEN" not in t3 and t3["SHOP_HOSTS"] == "x.example\ny.example", t3)
for raw, why in ((b"", "Keine Datei"), (b"kein json", "kein JSON"), (b"[1,2]", "values"),
                 (b'{"values": 3}', "values"), (b'{"values": {"A": {"b": 1}}}', "weder Text"),
                 (b"x" * (cf.MAX_BYTES + 1), "KiB")):
    v, e = cf.parse(raw)
    ok(f"abgelehnt: {why}", v is None and why in e, e)
ok("BOM am Anfang (Windows-Editor) stört nicht", cf.parse(b'\xef\xbb\xbf{"values": {}}')[1] == "")

print("\n=== Portal: die Suche ===")
hits = cf.search(ROWS, "host")
ok("findet über Schlüssel und Bezeichnung", [h["key"] for h in hits] == ["SHOP_HOSTS"]
   and "Schlüssel" in hits[0]["where"] and "Bezeichnung" in hits[0]["where"], hits)
ok("findet über den Wert", [h["key"] for h in cf.search(ROWS, "b.exam")] == ["SHOP_HOSTS"])
ok("KOEDER kein Treffer im Wert eines Geheimnisses", cf.search(ROWS, "LEAK") == [])
ok("Groß-/Kleinschreibung egal, leere Suche = nichts",
   cf.search(ROWS, "TITEL")[0]["key"] == "SHOP_TITLE" and cf.search(ROWS, "  ") == [])

print("\n=== Portal: Türen und Seite ===")
for route, fname in (('@app.get("/instances/<name>/config.json")', "def instance_config_download"),
                     ('@app.post("/instances/<name>/config-upload")', "def instance_config_upload")):
    body = SRC[SRC.index(route):]
    body = body[:body.index("\n@app.", 5)]
    ok(f"{route}: nur wer die Instanz verwaltet, und nicht ohne Sicht des Hosts",
       fname in body and "require_instance_admin(name)" in body and "_config_view(name) is None" in body)
up = SRC[SRC.index('@app.post("/instances/<name>/config-upload")'):]
up = up[:up.index("\n@app.", 5)]
ok("Hochladen speichert nichts (kein Spool), füllt nur das Formular",
   "_queue_and_wait" not in up and "_instance_page(name, inst, typed=typed" in up)
ok("Download: no-store, als Anhang", '"Cache-Control": "no-store"' in SRC and "-config.json" in SRC)
ok("Seite: Sprungziel je Feld, :target hervorgehoben, Markierung aus der Datei",
   'id="feld-{{ c.key }}"' in SRC and ".cfgfield:target{" in SRC and "cfgchanged" in SRC
   and '#feld-{{ h.key }}' in SRC)
ok("Seite: Karte Neustarts im Reiter Diagnose, letzter Start im Kopf",
   '<div class="card" id="neustarts">' in SRC and "Zuletzt gestartet" in SRC
   and 'i["restarts"] = rv.read(RESTARTS_DIR, name)' in SRC)
ok("Geheimnisfeld: gefüllt nur mit dem eben hochgeladenen Wert",
   "value=\"{{ c.value if c.key in i.cfg_changed else '' }}\"" in SRC)
dock = open(os.path.join(PORTAL, "Dockerfile"), encoding="utf-8").read()
ok("Dockerfile kopiert die beiden neuen Module", "portal/restart_view.py" in dock and "portal/config_file.py" in dock)

print()
print("all restart/config-file checks passed" if not fails else f"{fails} check(s) FAILED")
sys.exit(1 if fails else 0)
