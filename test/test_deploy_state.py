#!/usr/bin/env python3
"""Deployments, die man sehen und anhalten kann (RFC-0024).

Festgehalten wird die REGEL, nicht das heutige Verhalten:

* eine Anfrage bekommt eine Kennung, und die Kennung steht im Ergebnis
  UND im Protokoll — sonst kann niemand erfahren, wie SEIN Deployment
  ausgegangen ist (§1),
* der Arbeiter beansprucht eine Anfrage, indem er sie aus der
  Warteschlange schiebt: genau daran entscheidet sich, ob „Abbrechen"
  noch geht (§5),
* ein Anspruch, der älter ist als das Zeitlimit, gehört zu einem
  abgestürzten Lauf — er heißt nicht mehr „läuft" (§5),
* wer noch wartet, kann zurückgezogen werden; wer angefangen hat,
  nicht — und die Absage sagt warum (§5),
* das Paket in Betrieb wird nicht gelöscht, und die Absage nennt den
  Grund (§6),
* das Zeitlimit greift dort, wo die Zeit vergeht: an den Befehlen, die
  der Arbeiter absetzt (§5).

Braucht kein Docker und keinen Knoten.

Aufruf: python3 test/test_deploy_state.py
"""
import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-deploystate-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, os.path.join(HERE, "..", "platform"))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services", "portal"))

import appctl as m                                            # noqa: E402
import deploy_state as ds                                     # noqa: E402

QUEUE = os.path.join(m.SPOOL_DIR, "queue")
CLAIMS = m.SPOOL_CLAIMS
RESULTS = os.path.join(m.SPOOL_DIR, "results")
UPLOADS = os.path.join(m.SPOOL_DIR, "uploads")
for d in (QUEUE, CLAIMS, RESULTS, UPLOADS, m.APPS_DIR):
    os.makedirs(d, exist_ok=True)

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:400]}")


def queue_request(rid, instance, action, age=0.0, **extra):
    p = os.path.join(QUEUE, f"{rid}.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"id": rid, "instance": instance, "action": action,
                   **extra}, f)
    if age:
        t = time.time() - age
        os.utime(p, (t, t))
    return p


def claim_request(rid, instance, action, age=0.0, **extra):
    queue_request(rid, instance, action, **extra)
    p = os.path.join(CLAIMS, f"{rid}.json")
    os.replace(os.path.join(QUEUE, f"{rid}.json"), p)
    if age:
        t = time.time() - age
        os.utime(p, (t, t))
    return p


def clear():
    for d in (QUEUE, CLAIMS, RESULTS, UPLOADS):
        for fn in os.listdir(d):
            os.remove(os.path.join(d, fn))


def log_lines():
    try:
        with open(m.DEPLOY_LOG, encoding="utf-8") as f:
            return [json.loads(x) for x in f if x.strip()]
    except OSError:
        return []


print("=== §2 Zustand: wartet, läuft, oder abgestürzt ===")
clear()
queue_request("a" * 32, "demo", "artifact")
claim_request("b" * 32, "demo", "redeploy")
claim_request("c" * 32, "demo", "redeploy", age=ds.STALE_AFTER + 30)
state = {e["id"][0]: e["state"] for e in ds.in_flight(QUEUE, CLAIMS, "demo")}
ok("eine Anfrage in der Warteschlange heißt „wartet\"", state.get("a") == "queued", state)
ok("eine beanspruchte Anfrage heißt „läuft\"", state.get("b") == "running", state)
ok("ein Anspruch über dem Zeitlimit heißt nicht mehr „läuft\"",
   state.get("c") == "stale", state)

clear()
queue_request("d" * 32, "andere", "artifact")
ok("eine fremde Instanz taucht nicht auf",
   ds.in_flight(QUEUE, CLAIMS, "demo") == [])
ok("ohne Instanzname sieht man alles",
   len(ds.in_flight(QUEUE, CLAIMS)) == 1)

clear()
queue_request("e" * 32, "demo", "visibility")
ok("eine Sichtbarkeitsänderung ist kein Deployment",
   ds.deployment(ds.in_flight(QUEUE, CLAIMS, "demo")) is None,
   "sonst hieße jeder Klick im Portal „Deployment läuft“")
queue_request("f" * 32, "demo", "artifact")
d = ds.deployment(ds.in_flight(QUEUE, CLAIMS, "demo"))
ok("ein Paket-Upload dagegen schon", d and d["action"] == "artifact", d)
ok("mit Kennung antwortet er über GENAU diese Anfrage",
   (ds.deployment(ds.in_flight(QUEUE, CLAIMS, "demo"), "e" * 32) or {})
   .get("action") == "visibility")
ok("eine unbekannte Kennung ergibt nichts",
   ds.deployment(ds.in_flight(QUEUE, CLAIMS, "demo"), "9" * 32) is None)

print("\n=== §5 Abbrechen: nur, was noch nicht angefangen hat ===")
clear()
queue_request("1" * 32, "demo", "artifact")
open(os.path.join(UPLOADS, "1" * 32 + ".zip"), "wb").write(b"PK\x03\x04")
done, why, started = ds.withdraw(QUEUE, UPLOADS, "demo", "1" * 32,
                                 ds.in_flight(QUEUE, CLAIMS, "demo"))
ok("eine wartende Anfrage lässt sich zurückziehen", done, why)
ok("und ihr Paket bleibt nicht liegen",
   not os.path.exists(os.path.join(UPLOADS, "1" * 32 + ".zip")))
ok("danach wartet nichts mehr", ds.in_flight(QUEUE, CLAIMS, "demo") == [])

clear()
claim_request("2" * 32, "demo", "artifact")
done, why, started = ds.withdraw(QUEUE, UPLOADS, "demo", "2" * 32,
                                 ds.in_flight(QUEUE, CLAIMS, "demo"))
ok("ein angelaufenes Deployment wird NICHT abgebrochen", not done)
ok("und die Absage sagt, dass es angelaufen ist", started, why)
ok("der Anspruch bleibt liegen — es läuft ja",
   os.path.exists(os.path.join(CLAIMS, "2" * 32 + ".json")))

clear()
queue_request("3" * 32, "demo", "artifact")
done, why, started = ds.withdraw(QUEUE, UPLOADS, "fremde", "3" * 32,
                                 ds.in_flight(QUEUE, CLAIMS, "fremde"))
ok("eine fremde Instanz kann nichts zurückziehen", not done and not started, why)
ok("und die Anfrage liegt unberührt da",
   os.path.exists(os.path.join(QUEUE, "3" * 32 + ".json")))

print("\n=== §1/§5 Der Arbeiter: Anspruch, Kennung, Ergebnis ===")
clear()
before = len(log_lines())
# 'visibility' auf eine unbekannte Instanz: geht durch die ganze
# Schleife -- Anspruch, Ergebnis, Protokoll -- ohne Docker anzufassen.
queue_request("7" * 32, "gibtesnicht", "visibility")
m.cmd_process_deploys(None)
ok("die Warteschlange ist danach leer", os.listdir(QUEUE) == [])
ok("und der Anspruch ist zurückgegeben", os.listdir(CLAIMS) == [])
res_path = os.path.join(RESULTS, "7" * 32 + ".json")
ok("es gibt ein Ergebnis für die Kennung", os.path.exists(res_path))
res = json.load(open(res_path, encoding="utf-8")) if os.path.exists(res_path) else {}
ok("das Ergebnis nennt seine Kennung", res.get("id") == "7" * 32, res)
added = log_lines()[before:]
ok("das Protokoll hat genau eine Zeile dazubekommen", len(added) == 1, added)
ok("und diese Zeile trägt die Kennung",
   added and added[-1].get("id") == "7" * 32,
   "ohne sie kann niemand fragen: wie ist MEIN Deployment ausgegangen?")

print("\n=== §5 Ein abgestürzter Arbeiter lässt niemanden hängen ===")
clear()
before = len(log_lines())
claim_request("8" * 32, "demo", "artifact", age=ds.STALE_AFTER + 300)
open(os.path.join(UPLOADS, "8" * 32 + ".zip"), "wb").write(b"PK\x03\x04")
m.reap_stale_claims(RESULTS)
ok("der tote Anspruch ist weg", os.listdir(CLAIMS) == [])
res_path = os.path.join(RESULTS, "8" * 32 + ".json")
ok("es gibt ein Ergebnis, keine Stille", os.path.exists(res_path))
res = json.load(open(res_path, encoding="utf-8")) if os.path.exists(res_path) else {}
ok("und zwar ein fehlgeschlagenes", res.get("ok") is False, res)
ok("das Protokoll hält es fest",
   len(log_lines()) == before + 1 and log_lines()[-1].get("id") == "8" * 32)
ok("und das verwaiste Paket ist aufgeräumt",
   not os.path.exists(os.path.join(UPLOADS, "8" * 32 + ".zip")))

clear()
claim_request("9" * 32, "demo", "artifact", age=60)
m.reap_stale_claims(RESULTS)
ok("ein frischer Anspruch wird NICHT abgeräumt",
   os.listdir(CLAIMS) == ["9" * 32 + ".json"],
   "sonst würde der Aufräumer laufende Builds für tot erklären")

print("\n=== §5 Das Zeitlimit greift an den Befehlen ===")
clear()
sleeper = [sys.executable, "-c", "import time; time.sleep(30)"]
m.DEADLINE = time.time() + 1
t0 = time.time()
try:
    m.run(sleeper)
    caught = "nichts"
except m.DeployTimeout as e:
    caught = str(e)
except Exception as e:                                   # pragma: no cover
    caught = f"{type(e).__name__}: {e}"
finally:
    m.DEADLINE = None
ok("ein Befehl über der Frist wird abgebrochen",
   caught.startswith("aborted after"), caught)
ok("und zwar sofort, nicht erst nach dem Kindprozess", time.time() - t0 < 10,
   f"{time.time() - t0:.1f}s")
ok("ohne Frist gilt kein Zeitlimit",
   m.run([sys.executable, "-c", "pass"]).returncode == 0)

print("\n=== §6 Das Paket in Betrieb bleibt ===")
inst = {"source": {"kind": "artifact", "stored": "0.20.6-cea3.zip"}}
adir = m.artifact_dir("demo")
os.makedirs(adir, exist_ok=True)
for fn in ("0.20.6-cea3.zip", "0.20.5-be79.zip"):
    open(os.path.join(adir, fn), "wb").write(b"PK\x03\x04")
try:
    m.artifact_remove(inst, "demo", "0.20.6-cea3.zip")
    why = ""
except ValueError as e:
    why = str(e)
ok("das laufende Paket wird nicht gelöscht", bool(why), "es wurde gelöscht!")
ok("und die Absage nennt den Grund",
   "backup" in why.lower() or "Backup" in why, why)
ok("die Datei liegt noch da",
   os.path.exists(os.path.join(adir, "0.20.6-cea3.zip")))
msg = m.artifact_remove(inst, "demo", "0.20.5-be79.zip")
ok("ein anderes Paket lässt sich löschen",
   not os.path.exists(os.path.join(adir, "0.20.5-be79.zip")), msg)
try:
    m.artifact_remove(inst, "demo", "gibtesnicht.zip")
    why = ""
except ValueError as e:
    why = str(e)
ok("ein unbekanntes Paket wird abgelehnt", bool(why), why)

print("\n=== Die Wörter (§7) ===")
app_py = open(os.path.join(HERE, "..", "platform", "services", "portal",
                           "app.py"), encoding="utf-8").read()
ok("das Paket in Betrieb heißt „in Betrieb\", nicht „läuft\"",
   '<span class="badge">in Betrieb</span>' in app_py
   and '<span class="badge">läuft</span>' not in app_py,
   "„läuft“ heißt auf Deutsch auch „ist noch am Laufen“ — genau die "
   "Verwechslung, aus der RFC-0024 entstand")
ok("jede Paketzeile hat eine Aktion",
   "Erneut ausrollen" in app_py and "Hierauf zurück" in app_py)
ok("ein laufendes Deployment ist auf der Instanzseite sichtbar",
   "Deployment läuft" in app_py and "Deployment wartet" in app_py)

print()
print("=== Die Instanzseite zeigt es (§3/§4) ===")
try:
    import ast
    from jinja2 import Environment
    tree = ast.parse(app_py)
    body = next(ast.literal_eval(n.value) for n in tree.body
                if isinstance(n, ast.Assign)
                and any(getattr(t, "id", "") == "INSTANCE_EDIT_BODY"
                        for t in n.targets))
    tpl = Environment(autoescape=True).from_string(body)

    def render(deploy_now):
        return tpl.render(
            # `key` ist die Kennung in der URL, `name` der Name im
            # Mandanten (RFC-0025). Hier dieselbe Zeichenkette, weil die
            # Instanz im Standard-Mandanten liegt.
            i={"key": "demo", "name": "demo", "is_test": True, "artifacts": [
                   {"file": "0.20.6-cea3.zip", "running": True,
                    "received": "2026-08-29 19:48"},
                   {"file": "0.20.5-be79.zip", "running": False,
                    "received": "2026-08-29 17:27"}],
               "deploy_now": deploy_now, "deploy_limit": ds.DEPLOY_MAX_MINUTES,
               "config": [], "endpoints": [], "links": [],
               "link_candidates": [], "groups": [], "roles": [],
               "storage": [], "services": [], "route_rows": [],
               "source_lines": [], "aliases": []},
            tabs=[("deployment", "Deployment")], tab="deployment",
            msg=None, error=None)

    q = render({"state": "queued", "id": "1" * 32, "since": 30,
                "ago": "30 Sekunden", "action": "artifact",
                "label": "hochgeladenes Paket"})
    r = render({"state": "running", "id": "1" * 32, "since": 240,
                "ago": "4 Minuten", "action": "artifact",
                "label": "hochgeladenes Paket"})
    n = render(None)
    ok("wartet: die Seite sagt es und bietet „Abbrechen“",
       "Deployment wartet" in q and "Abbrechen" in q)
    ok("läuft: die Seite sagt es, mit „seit 4 Minuten“",
       "Deployment läuft" in r and "4 Minuten" in r)
    ok("läuft: „Abbrechen“ wird NICHT angeboten", "Abbrechen" not in r,
       "ein angelaufener Bau wird nicht abgeschossen")
    ok("ohne Deployment steht die Zeile nicht da",
       "Deployment läuft" not in n and "Deployment wartet" not in n)
    ok("das Paket in Betrieb bekommt „Erneut ausrollen“",
       n.index("Erneut ausrollen") < n.index("Hierauf zurück"),
       "es steht in der ersten Zeile, und die ist die laufende")
    ok("gelöscht werden kann nur das andere",
       n.count("/instances/demo/artifact-delete") == 1
       and "0.20.5-be79.zip" in n.split("artifact-delete")[1][:400]
       and "0.20.6-cea3.zip" not in n.split("artifact-delete")[1][:400],
       "das Paket in Betrieb darf keinen Löschen-Knopf haben")
except ImportError:
    print("SKIP  jinja2 fehlt — die Vorlage wird nicht gerendert")

print()
if fails:
    print(f"{fails} PRUEFUNG(EN) FEHLGESCHLAGEN")
    sys.exit(1)
print("ALLE PRUEFUNGEN BESTANDEN")
