#!/usr/bin/env python3
"""Pflege auf einen Blick: Store und Instanzliste (gerendert).

Jörg, 06.11. nach dem Update auf oaapx01: im Store fehlte ein Filter
„Updates verfügbar / Sicherheitsmeldungen", in der Instanzliste ein
Hinweis darauf und ein Filter überhaupt. Die Regeln selbst prüft
test_store_view.py; hier wird geprüft, dass die beiden Seiten sie zeigen:

    - Store: Hinweiszeile mit Kurzlinks, Filter „Pflege", Abzeichen
      „Update für n Instanzen" und „n Sicherheitsmeldungen"
    - Instanzen: Hinweiszeile, Spalte „Pflege" mit Links (Store-Seite,
      Karte „Abbild"), Filter Suche/Status/Kanal/Pflege, „Kein Treffer"
    - ein Mandanten-Admin zählt nur die eigenen Instanzen
    - der Katalog wird für die Instanzliste nicht bei jedem Aufruf neu
      geholt

Aufruf (im Portal-Abbild): python3 test/test_care_lists_portal.py
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("OAAP_DATA_DIR", tempfile.mkdtemp(prefix="oaap-care-lists-"))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services"))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services", "portal"))
import app as portal  # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:900]}")


TMP = tempfile.mkdtemp(prefix="oaap-care-lists-portal-")
portal.APPS_REGISTRY_DIR = TMP
portal.STORE_SOURCES_FILE = os.path.join(TMP, "store-sources.json")
json.dump({"sources": [{"id": "katalog", "name": "Katalog", "url": "catalog:katalog",
                        "trust": "platform", "enabled": True}]},
          open(portal.STORE_SOURCES_FILE, "w"))
json.dump({"schema": "0.2", "name": "Katalog", "apps": [
    {"id": "ai-gateway", "name": "KI-Gateway", "version": "0.5.3", "summary": "s",
     "package": {"catalog": True}},
    {"id": "fleetview", "name": "Fleetview", "version": "0.3.4", "summary": "s",
     "package": {"catalog": True}},
    {"id": "raci", "name": "RACI", "version": "0.1.2", "summary": "s",
     "package": {"catalog": True}},
]}, open(os.path.join(TMP, "catalog-katalog.json"), "w"))

INST = {
    "ai-gateway": {"app_id": "ai-gateway", "app_name": "KI-Gateway", "version": "0.2.1",
                   "channel": "production", "tenant": "pxx"},
    "fleetview": {"app_id": "fleetview", "app_name": "Fleetview", "version": "0.3.4",
                  "channel": "production", "tenant": "pxx"},
    "ollama": {"app_id": "ollama", "app_name": "Ollama", "version": "0.1.0",
               "channel": "test", "tenant": "cls"},
}
CARE = {"images": {"computed": "2026-11-06T10:00:00", "instances": {
    "ai-gateway": {"state": "open", "services": [{"state": "open", "open": 6}]},
    "fleetview": {"state": "fresh", "services": [{"state": "fresh", "open": 0}]},
    "ollama": {"state": "behind", "services": [{"state": "behind", "open": 0}]},
}}}
SCOPE = {"role": "server_admin", "tenant": None}


def visible():
    if SCOPE["role"] == "server_admin":
        return dict(INST)
    return {k: v for k, v in INST.items() if v["tenant"] == SCOPE["tenant"]}


portal.visible_instances = visible
portal.load_instances = lambda: dict(INST)
portal.care_facts = lambda: CARE
portal.pending_installs = lambda: set()
portal.node_profiles = lambda: []
portal.caller_roles = lambda: {SCOPE["role"]}
portal.caller_scope = lambda: (SCOPE["role"], SCOPE["tenant"])
portal.multi_tenant = lambda: False
portal.resolve_tenant = lambda ref: ref or ""
portal.fleet_view.probe_all = lambda pairs, probe: {
    n: ("ok", "Läuft", "") if n != "ollama" else ("down", "Antwortet nicht", "")
    for n, _ in pairs}
c = portal.app.test_client()
H = {"X-OAAP-User": "betreiber", "X-OAAP-Roles": "server_admin"}


def get(url):
    r = c.get(url, headers=H)
    return r.status_code, r.get_data(as_text=True)


print("=== Store ===")
st, body = get("/store")
ok("Store antwortet", st == 200, body[:300])
ok("Hinweiszeile mit Kurzlinks", 'href="/store?care=update"' in body
   and 'href="/store?care=security"' in body, body[:2000])
ok("Filter „Pflege“ mit drei Werten", 'name="care"' in body
   and "Update verfügbar" in body and "Update oder Sicherheitsmeldungen" in body)
ok("Abzeichen: Update für 1 Instanz", "Update für 1\n        Instanz<" in body
   or "Update für 1 Instanz" in " ".join(body.split()))
ok("Abzeichen: 6 Sicherheitsmeldungen", "6 Sicherheitsmeldungen" in " ".join(body.split()))
st, body = get("/store?care=update")
flat = " ".join(body.split())
ok("Filter „update“ zeigt nur das KI-Gateway", "KI-Gateway" in flat
   and "<h3>Fleetview</h3>" not in flat and "<h3>RACI</h3>" not in flat
   and "1 von 3 Apps" in flat, flat[:1500])
st, body = get("/store?care=bogus")
ok("unbekannter Filterwert wird ignoriert", st == 200 and "3 von 3 Apps" in " ".join(body.split()))

print("\n=== Instanzen ===")
portal._CATALOGUE_CACHE.update(at=0.0, versions={})
calls = []
real = portal.store_catalogue


def counting():
    calls.append(1)
    return real()


portal.store_catalogue = counting
st, body = get("/instances")
flat = " ".join(body.split())
ok("Liste antwortet", st == 200, body[:500])
ok("Spalte „Pflege“", "<th>Pflege</th>" in body)
ok("Update-Abzeichen verlinkt die Store-Seite",
   'href="/store/katalog/ai-gateway"' in body and "Update v0.5.3" in flat, flat[:3000])
ok("Sicherheitsmeldungen verlinken die Karte „Abbild“",
   'href="/instances/ai-gateway?tab=diagnose#abbild"' in body and "6 Sicherheitsmeldungen" in flat)
ok("Basis weitergezogen als leiser Hinweis", "Basis weitergezogen" in flat)
ok("Hinweiszeile oben", 'href="/instances?care=update"' in body
   and "1 Instanz mit Update im Store" in flat
   and "1 Instanz mit offenen Sicherheitsmeldungen" in flat)
ok("Filterfelder Suche, Status, Kanal, Pflege",
   all(f'name="{n}"' in body for n in ("q", "state", "channel", "care")))
get("/instances")
ok("der Katalog wird nicht bei jedem Aufruf neu geholt", len(calls) == 1, calls)

st, body = get("/instances?care=attention")
flat = " ".join(body.split())
ok("Filter „attention“: nur das KI-Gateway",
   ">ai-gateway<" in body and ">fleetview<" not in body and ">ollama<" not in body
   and "1 von 3" in flat, flat[-3000:])
st, body = get("/instances?state=down")
ok("Filter Status", ">ollama<" in body and ">ai-gateway<" not in body)
st, body = get("/instances?channel=test")
ok("Filter Kanal", ">ollama<" in body and ">fleetview<" not in body)
st, body = get("/instances?q=fleet")
ok("Suche", ">fleetview<" in body and ">ollama<" not in body)
st, body = get("/instances?q=gibtsnicht")
ok("kein Treffer wird gesagt", "Kein Treffer" in body and "alle 3 Instanzen" in body)

print("\n=== Mandanten-Admin ===")
SCOPE.update(role="tenant_admin", tenant="cls")
H = {"X-OAAP-User": "chef", "X-OAAP-Roles": "tenant_admin"}
st, body = get("/instances")
flat = " ".join(body.split())
ok("sieht nur die eigene Instanz, keine fremde Meldung",
   ">ollama<" in body and ">ai-gateway<" not in body
   and "Sicherheitsmeldungen</a>" not in body and "mit Update im Store" not in flat, flat[-2500:])
st, body = get("/store")
flat = " ".join(body.split())
ok("Store zählt keine fremden Instanzen",
   "Update für" not in flat and "Sicherheitsmeldungen</span>" not in flat
   and "offenen Sicherheitsmeldungen</a>" not in flat, flat[:2500])

print(f"\n{'ALL PASS' if not fails else str(fails) + ' FAILURES'}")
sys.exit(1 if fails else 0)
