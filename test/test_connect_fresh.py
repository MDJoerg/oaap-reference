#!/usr/bin/env python3
"""Ein liegengebliebener Zustand ist kein Zustand (0.1.286, connect_fresh.py).

Befund 2026-11-15 (Messung RFC-0087 Stufe 2): Der Dienst `connect` war
angehalten, das Portal zeigte den Tunnel weiter als verbunden -- es las
state.json, und die blieb einfach liegen. Damit sah auch das
Statusdokument und also die Zentrale nichts.

Geprueft wird:
- die Regel: eine Datei mit `beat`, aelter als vier Schlaege und
  mindestens zwei Minuten, gilt nicht; ohne `beat` (Dienst vor 0.1.286,
  schrieb nur bei Aenderungen) wird sie genommen wie sie ist;
- die Kommandozeile (appctl.connect_state) liest nach derselben Regel;
- das Portal: Seite /connector nennt "seit ... nichts mehr", die
  Aufmerksamkeitspunkte melden connector_down und tunnel_down, die
  Halbkanten fuer die Zentrale stehen auf nicht verbunden.

Der Herzschlag selbst (der Dienst schreibt ohne Aenderung neu) steht in
test_connector_tunnel.py, gegen den echten Dienst.

Aufruf: python3 test/test_connect_fresh.py
"""
import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-connect-fresh-test-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, os.path.join(HERE, "..", "platform"))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services"))

import connect_fresh as cf                                    # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:800]}")


def iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


NOW = time.time()
CONNECTED = {"tunnels": {"x01": {"connected": True, "offers": [{"name": "mg"}]}},
             "connectors": {"i01": {"connected": True}}, "exposures": {}}


def state(age, beat=30):
    d = dict(CONNECTED, time=iso(NOW - age))
    if beat is not None:
        d["beat"] = beat
    return d


print("=== Regel ===")
ok("frisch (10 s) wird geglaubt", cf.judge(state(10), NOW)[0] is not None)
ok("90 s mit Schlag 30: noch geglaubt (zwei Minuten Mindestfrist)",
   cf.judge(state(90), NOW)[0] is not None)
s = state(150)
ok("150 s mit Schlag 30: liegengeblieben, nennt seine Zeit",
   cf.judge(s, NOW) == (None, s["time"]), cf.judge(s, NOW))
ok("Schlag 60: 200 s noch geglaubt, 250 s nicht",
   cf.judge(state(200, 60), NOW)[0] is not None and cf.judge(state(250, 60), NOW)[0] is None)
ok("ohne beat (alter Dienst): auch nach einem Tag genommen wie sie ist",
   cf.judge(state(86400, None), NOW)[0] is not None)
ok("keine Datei: None, ohne Zeit", cf.judge(None, NOW) == (None, ""))
ok("beat, aber unlesbare Zeit: gilt nicht", cf.judge(dict(CONNECTED, beat=30, time="gestern"), NOW)[0] is None)
ok("beat Unsinn: wie ohne beat", cf.judge(dict(CONNECTED, beat="x", time=iso(NOW - 999)), NOW)[0] is not None)


print("\n=== Halbkanten aus einem liegengebliebenen Zustand ===")
# Erster Live-Lauf 2026-11-15: mit None fiel die innere Haelfte ganz weg
# (ihr tunnel_id steht nur in state.json), die Zentrale sah eine Haelfte
# und schwieg. disconnected() behaelt die Namen, nimmt nur "verbunden".
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services", "portal"))
import fleet_links                                            # noqa: E402

raw = dict(state(600), connectors={"i01": {"connected": True, "since": "x", "tunnel_id": "t-abc"}},
           exposures={"e": {"host": "h"}})
dead = cf.disconnected(raw)
ok("disconnected: nichts verbunden, nichts freigegeben, tunnel_id bleibt",
   dead["connectors"]["i01"] == {"connected": False, "tunnel_id": "t-abc"}
   and not dead["tunnels"]["x01"]["connected"] and dead["exposures"] == {}, dead)
ok("disconnected: das Original bleibt unberuehrt", raw["connectors"]["i01"]["connected"])
links, _block, _by = fleet_links.build(
    {"connectors": {"i01": {"endpoint": "https://aussen.example", "offers": {}}}}, dead,
    {}, {}, {}, {}, {}, lambda t: t)
inner = [r for r in links if r.get("side") == "inner"]
ok("Halbkante innen: da, mit tunnel_id, Zustand error",
   len(inner) == 1 and inner[0]["id"] == "t-abc" and inner[0]["state"] == "error", links)


print("\n=== Kommandozeile ===")
import appctl as m                                            # noqa: E402

os.makedirs(m.CONNECT_STATE_DIR, exist_ok=True)


def put(doc):
    with open(os.path.join(m.CONNECT_STATE_DIR, "state.json"), "w", encoding="utf-8") as f:
        json.dump(doc, f)


put(state(5))
ok("appctl: frischer Zustand gelesen", m.connect_state().get("tunnels", {}).get("x01", {}).get("connected"))
put(state(600))
ok("appctl: liegengebliebener Zustand ist {}", m.connect_state() == {}, m.connect_state())


print("\n=== Portal ===")
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services", "portal"))
import app as portal                                           # noqa: E402

D = tempfile.mkdtemp(prefix="oaap-connect-fresh-portal-")
portal.CONNECT_FILE = os.path.join(D, "connect.json")
portal.CONNECT_STATE = os.path.join(D, "state.json")
with open(portal.CONNECT_FILE, "w", encoding="utf-8") as f:
    json.dump({"keys": {"x01": {"tenant": "t1"}},
               "connectors": {"i01": {"endpoint": "https://aussen.example"}}}, f)


def pstate(doc):
    with open(portal.CONNECT_STATE, "w", encoding="utf-8") as f:
        json.dump(doc, f)


portal._load_destinations_all = lambda: {"t1": {"mg": {"kind": "http",
                                                       "target": {"via": "x01/mg"}}}}
portal.tenant_label = lambda tid: tid
portal.caller_roles = lambda: {"server_admin"}
portal.load_tenants = lambda: {"t1": {"label": "t1"}}
portal.default_tenant_id = lambda: "t1"
portal.external_host = lambda: "oaap.example.org"
portal.host_tenant_scope = lambda host: ("t1", True)
portal.caller_scope = lambda: ("server_admin", "t1")
portal.multi_tenant = lambda: True
portal.node_profiles = lambda: []
portal.app.config["PROPAGATE_EXCEPTIONS"] = True

pstate(state(5))
ok("frisch: keine Aufmerksamkeit", portal.connect_attention() == [], portal.connect_attention())
v = portal.connect_view()
ok("frisch: Tunnel verbunden, nichts liegengeblieben",
   v["tunnels"][0]["connected"] and v["reported"] and not v["stale"], v)

old = state(600)
pstate(old)
kinds = sorted(i["kind"] for i in portal.connect_attention())
ok("liegengeblieben: connector_down und tunnel_down", kinds == ["connector_down", "tunnel_down"], kinds)
v = portal.connect_view()
ok("liegengeblieben: nichts verbunden, Seite weiss seit wann",
   not v["tunnels"][0]["connected"] and not v["connectors"][0]["connected"]
   and not v["reported"] and v["stale"] == old["time"], v)
c = portal.app.test_client()
t = c.get("/connector", headers={"X-OAAP-User": "root", "X-OAAP-Roles": "server_admin"}).get_data(as_text=True)
ok("Seite /connector: 'seit ... nichts mehr'",
   "nichts mehr" in t and old["time"].replace("T", " ").replace("Z", "") in t,
   t[t.find('class="err"'):t.find('class="err"') + 300])

# die Halbkanten fuer die Zentrale lesen ueber _connect_data
conf, st = portal._connect_data()
ok("_connect_data: liegengebliebener Zustand ist None", st is None and conf.get("keys"))

pstate(state(86400, None))
ok("alter Dienst ohne beat: wie bisher geglaubt", portal.connect_attention() == [])

print("")
print("OK" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
