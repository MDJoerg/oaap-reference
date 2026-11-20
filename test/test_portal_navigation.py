#!/usr/bin/env python3
"""Wege im Portal (Jörg, 20.11.).

    - Der server_admin wechselt den Mandanten über die Kopfzeile und kommt
      von dort auf die Adresse des Knotens zurück. Ein tenant_admin sieht
      das Menü nicht, ein Knoten ohne externen Namen auch nicht.
    - In der Instanzliste öffnet der Name die Seite der Instanz in einem
      neuen Fenster; „Bearbeiten“ bleibt die Objektseite.
    - Die Kacheln unter „Apps“ sind gleich hoch: die Beschreibung steht in
      drei Zeilen, „mehr“ blendet sie auf; die Kachel ist kein Link mehr,
      der einen Knopf enthielte.

Aufruf (im Portal-Abbild): python3 test/test_portal_navigation.py
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
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


TMP = tempfile.mkdtemp(prefix="oaap-portal-nav-")
TENANTS = {"t-default": {"label": "default", "name": "Knotenbetrieb"},
           "t-bec": {"label": "bec", "name": "BEC GmbH"},
           "t-pxx": {"label": "pxx", "name": "PXX"}}
portal.TENANTS_FILE = os.path.join(TMP, "tenants.json")
json.dump({"tenants": TENANTS}, open(portal.TENANTS_FILE, "w"))
EXT = {"host": "node.example.org"}
portal.external_host = lambda: EXT["host"]
ROLE = {"r": "server_admin", "tenant": "t-default"}
portal.caller_roles = lambda: {ROLE["r"]}
portal.caller_record = lambda: {"username": "x", "tenant": ROLE["tenant"]}
portal.multi_tenant = lambda: True
INSTANCES = {
    "bec-tools": {"name": "tools", "app_id": "mcp-gateway", "app_name": "MCP Gateway",
                  "tenant": "t-bec", "version": "0.28.0", "channel": "production",
                  "address": "tools.bec.example.com"},
    "bec-chat": {"name": "chat", "app_id": "chat", "app_name": "Chat",
                 "tenant": "t-bec", "version": "0.16.0", "channel": "production"},
}
portal.load_instances = lambda: INSTANCES
portal.fleet_view.probe_all = lambda pairs, fn: {n: ("ok", "Gesund", "") for n, _ in pairs}
portal.catalogue_versions = lambda: {}
portal.care_facts = lambda: {}
portal.pending_installs = lambda: set()
portal._in_flight = lambda: []
portal.node_profiles = lambda: []
c = portal.app.test_client()


def get(path, host="node.example.org", role="server_admin", tenant="t-default"):
    ROLE["r"], ROLE["tenant"] = role, tenant
    r = c.get(path, headers={"X-OAAP-User": "x", "X-OAAP-Roles": role}, base_url=f"https://{host}")
    return r.status_code, r.get_data(as_text=True)


print("=== Mandantenwechsler ===")
st, body = get("/instances")
ok("server_admin an der Knotenadresse: das Menü steht in der Kopfzeile",
   st == 200 and 'class="usermenu tenantmenu"' in body, (st, body[:400]))
ok("es führt zurück zur Adresse des Knotens", 'href="https://node.example.org/"' in body)
ok("und zu jedem Mandanten, der Standardmandant ist der Knoten selbst",
   'href="https://bec.node.example.org/"' in body and 'href="https://pxx.node.example.org/"' in body
   and "default.node.example.org" not in body)
ok("an der Knotenadresse ist „Knoten“ angehakt",
   'href="https://node.example.org/" aria-current="page"' in body)

st, body = get("/instances", host="bec.node.example.org")
ok("am Ort von bec ist bec angehakt und steht oben",
   'href="https://bec.node.example.org/" aria-current="page"' in body
   and '<span class="who">bec</span>' in body, body[:1500])

st, body = get("/instances", role="tenant_admin", tenant="t-bec", host="bec.node.example.org")
ok("tenant_admin: kein Wechsler", st == 200 and 'class="usermenu tenantmenu"' not in body, st)

EXT["host"] = ""
st, body = get("/instances")
ok("ohne externen Namen: kein Wechsler (es gäbe keine Adressen)", 'class="usermenu tenantmenu"' not in body)
EXT["host"] = "node.example.org"

print("\n=== Instanzliste ===")
st, body = get("/instances")
ok("der Name öffnet die eigene Adresse der Instanz in einem neuen Fenster",
   'href="https://tools.bec.example.com/" target="_blank" rel="noopener"' in body, body[-3000:])
ok("ohne eigenen Namen die automatische Adresse, ebenfalls im neuen Fenster",
   'href="https://chat.bec.node.example.org/" target="_blank" rel="noopener"' in body, body[-3000:])
ok("„Bearbeiten“ bleibt die Objektseite",
   'href="/instances/bec-tools">Bearbeiten</a>' in body)

print("\n=== Instanzseite ===")
src = open(portal.__file__, encoding="utf-8").read()
ok("die Adresse auf der Instanzseite öffnet ein neues Fenster",
   '<a href="{{ i.address_url }}" target="_blank" rel="noopener">' in src)

print("\n=== Kacheln unter „Apps“ ===")
ok("die Kachel ist kein Link mit Knopf darin: der Link liegt als Decke darüber",
   '<div class="tile apptile">' in src and 'class="tilecover"' in src
   and '<a class="tile" href="{{ t.url }}"' not in src)
ok("die Beschreibung steht in drei Zeilen, ganz im Tooltip, „mehr“ blendet auf",
   "-webkit-line-clamp:3" in src and 'class="desc" title="{{ t.description }}"' in src
   and 'class="more" hidden>mehr</button>' in src and ".apptile.open p.desc" in src)

print("\n=== Gesundheit im Kontext eines Mandanten ===")
INSTANCES["pxx-geheim"] = {"name": "geheim", "app_id": "chat", "app_name": "Chat",
                           "tenant": "t-pxx", "version": "0.16.0", "channel": "production"}
portal.traffic.snapshot = lambda d: {"at": 1795160000, "hosts": {
    "tools.bec.example.com": [40, 0, 4096, 2000, 300, 2],
    "geheim.pxx.node.example.org": [99, 0, 1, 1, 1, 0]},
    "containers": [["oaap-app-bec-tools", 3.5, 120]]}
portal._read_json = lambda path: ({"instances": {"bec-tools": {"services": [
    {"container": "oaap-app-bec-tools", "restarts": 2, "oom": False}]}}}
    if path == portal.STATE_VIEW else {})
portal._rehearsal_options = lambda: {"instances": {"bec-tools": {"kbytes": 2048},
                                                   "pxx-geheim": {"kbytes": 999999}}}
portal.report.read_ledger = lambda path: (
    [{"day": "2026-10-15", "bytes": {"tenant": 800000}},
     {"day": "2026-11-01", "bytes": {"tenant": 1000000}},
     {"day": "2026-11-12", "bytes": {"tenant": 1500000}},
     {"day": "2026-11-19", "bytes": {"tenant": 2000000}}] if path.endswith("t-bec.jsonl") else [])
portal._tenant_backup_facts = lambda tid: {"word": "im Knoten-Backup", "last": None,
                                           "node_last": {"when": "2026-11-20 03:30"}}

st, body = get("/health", host="bec.node.example.org")
ok("server_admin an der Adresse von bec: die Seite des Mandanten", st == 200
   and "Gesundheit · BEC GmbH" in body, (st, body[:600]))
ok("nur seine Instanzen, keine fremde", ">tools</a>" in body and ">chat</a>" in body
   and "geheim" not in body, body[-4000:])
ok("Zustand, Daten, Verkehr der Stunde, CPU/Speicher jetzt, Neustarts",
   "2,0 MB" in body and ">40" in body and "50 ms" in body and "3.5 %" in body
   and "120 MB" in body, body[-4000:])
ok("die Datengröße im Verlauf: Kurve und Veränderung gegen 7/30 Tage",
   "<svg" in body and "7 Tage +" in body and "30 Tage +" in body, body[:3000])
ok("ein Weg zurück zur Gesundheit des Knotens",
   'href="https://node.example.org/health"' in body)
ok("keine knotenweite Kurve auf der Mandantenseite", "Staerkster Container" not in body)

st, body = get("/health", role="tenant_admin", tenant="t-bec")
ok("tenant_admin an der Knotenadresse: auch dort nur sein Mandant",
   st == 200 and "Gesundheit · BEC GmbH" in body and "geheim" not in body, (st, body[:300]))
ok("der tenant_admin bekommt keinen Verweis auf den Knoten",
   'href="https://node.example.org/health"' not in body)
st, body = get("/health", role="tenant_admin", tenant="t-bec", host="pxx.node.example.org")
ok("tenant_admin an der Adresse eines fremden Mandanten: abgelehnt", st == 403, st)
st, body = get("/health", role="user", tenant="t-bec", host="bec.node.example.org")
ok("ein user bekommt die Seite nicht", st == 403, st)
with portal.app.test_request_context("/health", base_url="https://node.example.org",
                                     headers={"X-OAAP-Roles": "server_admin"}):
    ROLE["r"], ROLE["tenant"] = "server_admin", "t-default"
    ok("server_admin an der Knotenadresse: weiter die Seite des Knotens",
       portal._health_scope() == (None, None), portal._health_scope())


print("\n=== Verlauf je Instanz (RFC-0108) ===")
import time as _t  # noqa: E402
import instance_metrics as im  # noqa: E402
MD = tempfile.mkdtemp(prefix="oaap-nav-metrics-")
portal.METRICS_DIR = MD
now = int(_t.time())
tg = {"bec-tools": {"tenant": "t-bec", "containers": ["oaap-app-bec-tools"], "hosts": [],
                    "dir": "/x", "asleep": False},
      "pxx-geheim": {"tenant": "t-pxx", "containers": ["oaap-app-pxx-geheim"], "hosts": [],
                     "dir": "/y", "asleep": False}}
for k in range(30, 0, -1):
    im.sample(MD, now - k * 60, {"containers": [("oaap-app-bec-tools", 5.0 + k % 3, 200.0, 0, 0),
                                                ("oaap-app-pxx-geheim", 50.0, 999.0, 0, 0)]},
              tg, du=lambda p: 42.0)
st, body = get("/health?i=bec-tools", host="bec.node.example.org")
ok("CPU und Speicher jetzt aus dem Verlauf, nicht aus den Top 6",
   ">200 MB" in body and "120 MB" not in body, body[-5000:])
ok("Minikurven je Instanz", 'class="spark"' in body)
ok("ein Klick auf „Verlauf“ öffnet die Diagramme der Instanz",
   'id="verlauf-instanz"' in body and "Verlauf: tools" in body and "vl-svg" in body, body[:3000])
ok("die Fensterknöpfe behalten die Instanz",
   'href="/health?i=bec-tools&amp;w=1w#verlauf-instanz"' in body, body[:4000])
ok("der Verlauf des Mandanten (Summe) steht oben", "Verlauf des Mandanten" in body)
ok("kein Wert der fremden Instanz (999 MB, 50 %) auf der Seite",
   "999 MB" not in body and "999,0" not in body and "geheim" not in body)
st, body = get("/health?i=pxx-geheim", host="bec.node.example.org")
ok("eine fremde Instanz im Parameter öffnet nichts",
   'id="verlauf-instanz"' not in body and "geheim" not in body)

import tenant_health_view as thv  # noqa: E402
r = thv.ranking([("a", "a", "bec", {"series": {"cpu": [[1, 10, 10, 10], [2, 30, 30, 30]],
                                                "mem": [[1, 100, 100, 100]],
                                                "disk": [[1, 5, 5, 5], [2, 25, 25, 25]]}}),
                 ("b", "b", "pxx", {"series": {"cpu": [[1, 50, 50, 50]]}}),
                 ("c", "c", "pxx", {"series": {}})], "24h")
ok("Rangliste: mittlere CPU absteigend, Wachstum der Daten, ohne Werte nicht dabei",
   [x["key"] for x in r["cpu"]] == ["b", "a"] and r["cpu"][1]["cpu"] == 20
   and r["growth"][0]["growth"] == 20 and len(r["mem"]) == 1, r)
ok("die Knotenseite trägt die Karte „Was kostet am meisten“",
   'id="kosten"' in src and "ranked=ranked" in src)
ok("die Instanzseite zeigt den Verlauf im Reiter Diagnose",
   "{% if i.history %}{{ i.mx_style }}{{ i.history }}{% endif %}" in src)

print(f"\n{'ALL PASS' if not fails else str(fails) + ' FAILURES'}")
sys.exit(1 if fails else 0)
