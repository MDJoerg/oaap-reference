#!/usr/bin/env python3
"""Der Paketkatalog im Store des Portals (RFC-0050 Stufe 2, Portalseite).

Der Knoten schreibt dem Portal einen Schnappschuss der Liste (ohne
Dateipfade); das Portal liest ihn wie jede andere Quelle und bietet dazu
die Wahl "direkt in Produktion" oder "mit Test-Instanz". Geprueft wird:

    - der Katalog erscheint im Store, ein kaputter Katalog als Fehlerzeile
      (keine leere Seite, kein 500)
    - die Wahl steht nur bei einer App, die noch nicht installiert ist,
      und die Anfrage traegt hoechstens "test" oder "production" -- alles
      andere wird Produktion; die Anfrage nennt nie ein Paket
    - was aus dem Schnappschuss kommt, ist Daten: Markup wird maskiert, eine
      Quelle mit unmoeglicher Kennung wird nicht als Dateiname benutzt
    - eine ungeprueft eingetragene Quelle verlangt weiter die Bestaetigung

Braucht Flask; der Knoten ist ersetzt.
Aufruf: python3 test/test_catalog_portal.py
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services", "portal"))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services"))

import app as portal                                           # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:700]}")


TMP = tempfile.mkdtemp(prefix="oaap-catalog-portal-")
portal.APPS_REGISTRY_DIR = TMP
portal.STORE_SOURCES_FILE = os.path.join(TMP, "store-sources.json")
portal.SPOOL_DIR = os.path.join(TMP, "spool")
portal.SPOOL_QUEUE = os.path.join(portal.SPOOL_DIR, "queue")
portal.SPOOL_RESULTS = os.path.join(portal.SPOOL_DIR, "results")
portal.INSTALL_WAIT_SECONDS = 0
INSTALLED = {}
portal.visible_instances = lambda: {k: {"app_id": k, "version": v}
                                    for k, v in INSTALLED.items()}
portal.pending_installs = lambda: set()
portal.node_profiles = lambda: []
portal.caller_roles = lambda: {"server_admin"}
portal.caller_scope = lambda: ("server_admin", "")
portal.host_tenant_scope = lambda host: (None, True)
c = portal.app.test_client()
H = {"X-OAAP-User": "betreiber", "X-OAAP-Roles": "server_admin"}


def sources(*srcs):
    json.dump({"sources": list(srcs)}, open(portal.STORE_SOURCES_FILE, "w"))


def snapshot(sid, apps=None, error=None):
    doc = {"error": error, "apps": []} if error else {
        "schema": "0.2", "name": "Paketkatalog", "apps": apps}
    json.dump(doc, open(os.path.join(TMP, f"catalog-{sid}.json"), "w"))


def entry(app_id, version="0.1.2", **kw):
    return dict({"id": app_id, "name": app_id.title(), "version": version,
                 "summary": f"{app_id} Zusammenfassung",
                 "package": {"catalog": True, "sha256": "a" * 64, "size": 10}}, **kw)


def src(sid="catalog-katalog", trust="verified"):
    return {"id": sid, "name": "Betreiber-Katalog", "url": "catalog:katalog",
            "trust": trust, "enabled": True}


def queued():
    d = portal.SPOOL_QUEUE
    out = []
    if os.path.isdir(d):
        for fn in sorted(os.listdir(d)):
            out.append(json.load(open(os.path.join(d, fn))))
            os.remove(os.path.join(d, fn))
    return out


print("=== der Katalog im Store ===")
sources(src())
snapshot("catalog-katalog", [entry("website"), entry("vereinsportal", "0.0.9")])
r = c.get("/store", headers=H)
t = r.get_data(as_text=True)
ok("beide Apps stehen im Store, die Quelle ist genannt",
   r.status_code == 200 and "Website" in t and "Vereinsportal" in t, t[-500:])
ok("ein Dateipfad oder eine Pruefsumme steht nicht auf der Seite",
   "packages/" not in t and "a" * 64 not in t)
snapshot("catalog-katalog", error="the catalog's list is not valid JSON")
r = c.get("/store", headers=H)
t = r.get_data(as_text=True)
ok("ein kaputter Katalog: Seite lebt, der Grund steht da",
   r.status_code == 200 and "not valid JSON" in t, (r.status_code, t[-400:]))
os.remove(os.path.join(TMP, "catalog-catalog-katalog.json"))
r = c.get("/store", headers=H)
ok("noch kein Schnappschuss (Knoten hat noch nicht geschrieben): Fehlerzeile, kein 500",
   r.status_code == 200 and "Betreiber-Katalog" in r.get_data(as_text=True))
# Ein Verzeichnis `catalog-a` gibt es, und `catalog-a/../evil.json` ist eine
# andere Datei im selben Ordner: ohne Pruefung der Kennung wuerde sie gelesen.
os.makedirs(os.path.join(TMP, "catalog-a"), exist_ok=True)
json.dump({"apps": [entry("boese")]}, open(os.path.join(TMP, "evil.json"), "w"))
sources(src("a/../evil"))
r = c.get("/store", headers=H)
ok("eine Quelle mit unmoeglicher Kennung wird nie als Dateiname benutzt",
   r.status_code == 200 and "Boese" not in r.get_data(as_text=True)
   and "invalid source id" in r.get_data(as_text=True))
sources(src())

print("\n=== was aus dem Schnappschuss kommt, ist Daten ===")
snapshot("catalog-katalog", [entry("website", summary="<script>alert(1)</script>",
                                   name="<img src=x onerror=y>")])
t = c.get("/store", headers=H).get_data(as_text=True)
ok("Name und Zusammenfassung mit Markup stehen als Text da",
   "<script>alert" not in t and "<img src=x" not in t and "&lt;" in t)
snapshot("catalog-katalog", [entry("website"), entry("../x"), entry("A B")])
t = c.get("/store", headers=H).get_data(as_text=True)
ok("die Seite einer App mit gueltiger Kennung geht auf",
   c.get("/store/catalog-katalog/website", headers=H).status_code == 200)

print("\n=== die Wahl: Produktion oder Test ===")
snapshot("catalog-katalog", [entry("website")])
t = c.get("/store/catalog-katalog/website", headers=H).get_data(as_text=True)
ok("eine noch nicht installierte App bietet beide Wege an",
   'name="channel" value="production"' in t and 'name="channel" value="test"' in t
   and "website-test" in t, t[-1500:])
ok("Produktion ist vorgewaehlt", 'value="production" checked' in t)
INSTALLED["website"] = "0.1.0"
t = c.get("/store/catalog-katalog/website", headers=H).get_data(as_text=True)
ok("eine installierte App bietet die Wahl nicht an (die Instanz behaelt ihren Kanal)",
   'name="channel"' not in t and "Aktualisieren auf v0.1.2" in t, t[-800:])
INSTALLED.clear()


def post(form):
    r = c.post("/store/install", data=form, headers=H)
    return r, queued()


r, q = post({"app_id": "website", "source_id": "catalog-katalog", "channel": "test"})
ok("'mit Test-Instanz': die Anfrage traegt channel=test, die App-Id und die Quelle",
   r.status_code == 303 and len(q) == 1 and q[0]["channel"] == "test"
   and q[0]["instance"] == "website" and q[0]["source_id"] == "catalog-katalog"
   and q[0]["action"] == "install", q)
r, q = post({"app_id": "website", "source_id": "catalog-katalog", "channel": "production"})
ok("'direkt': channel=production", len(q) == 1 and q[0]["channel"] == "production", q)
r, q = post({"app_id": "website", "source_id": "catalog-katalog"})
ok("ohne Angabe: Produktion (wie bisher)", len(q) == 1 and q[0]["channel"] == "production", q)
for bad in ("../../x", "TEST", "test ", "staging", ""):
    r, q = post({"app_id": "website", "source_id": "catalog-katalog", "channel": bad})
    if not (len(q) == 1 and q[0]["channel"] == "production"):
        break
else:
    bad = None
ok("alles ausser genau 'test' wird Produktion", bad is None, (bad, q))
ok("die Anfrage nennt nie ein Paket, keine Adresse, keinen Pfad",
   not any(k in q[0] for k in ("url", "package", "path", "zip", "version")), q)

print("\n=== ungeprueft ===")
sources(src(trust="unverified"))
snapshot("catalog-katalog", [entry("website")])
r, q = post({"app_id": "website", "source_id": "catalog-katalog", "channel": "test"})
ok("ohne Haken: nicht in den Spool", r.status_code == 303 and q == [], q)
r, q = post({"app_id": "website", "source_id": "catalog-katalog", "channel": "test",
             "confirm_source": "catalog-katalog"})
ok("mit Haken: in den Spool, mit der Bestaetigung",
   len(q) == 1 and q[0]["confirm_source"] == "catalog-katalog", q)

print("\nFAILS:", fails)
sys.exit(1 if fails else 0)
