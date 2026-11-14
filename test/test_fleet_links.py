#!/usr/bin/env python3
"""Halbkanten im Statusdokument (RFC-0087 Stufe 1, oaap.fleet.status 0.10) --
die Seite des Knotens, rein (kein Flask, keine Dateien).

Festgehalten wird:

- beide Tunnelenden kommen zur selben ID, der aeussere aus dem Hash, der
  innere aus dem Schluessel -- und portal/fleet_links.py und der
  Connect-Dienst rechnen dieselbe Regel (zwei Abschriften, ein Test);
- die ID ist weder der Schluessel noch der gespeicherte Hash;
- die Preisgabe: Obergrenze des Knotens, Stufe je Mandant nie darueber,
  ohne Salz faellt `pseudonym` auf `count`;
- nie eine Adresse, ein Pfad oder ein Ziel in den Zeilen;
- ein Angebot nennt die eigene Instanz, die es erreicht, sonst `external`;
- Destinationen `via` und `through`, `direct` bleibt draussen;
- auf `count` weder Mandant noch Destinationsname, gebunden nur als Zahl.

Aufruf: py -3 test/test_fleet_links.py
"""
import ast
import hashlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PORTAL = os.path.join(HERE, "..", "platform", "services", "portal")
CONNECT = os.path.join(HERE, "..", "platform", "services", "connect", "app.py")
sys.path.insert(0, PORTAL)

import fleet_links as fl  # noqa: E402
import fleet_view         # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}"
          + ("" if cond or detail == "" else f"  -- {str(detail)[:700]}"))


print("=== die Tunnel-ID ===")
KEY = "oaapc_ganz-geheimer-schluessel-0123456789"
H = hashlib.sha256(KEY.encode()).hexdigest()
tid = fl.tunnel_id(H)
ok("aussen aus dem Hash = innen aus dem Schluessel", tid == fl.tunnel_id_from_key(KEY), tid)
ok("Form t- plus 24 Hex", tid.startswith("t-") and len(tid) == 26
   and all(c in "0123456789abcdef" for c in tid[2:]), tid)
ok("weder Schluessel noch gespeicherter Hash noch ein Stueck davon",
   KEY not in tid and H[:24] not in tid and tid[2:] not in H)
ok("ohne Schluessel keine ID", fl.tunnel_id("") == "" and fl.tunnel_id_from_key("") == "")

# The connect service carries its own copy (its image has no portal code).
src = open(CONNECT, encoding="utf-8").read()
fn = next(n for n in ast.parse(src).body if isinstance(n, ast.FunctionDef) and n.name == "tunnel_id")
ns = {"hashlib": hashlib}
exec(compile(ast.Module(body=[fn], type_ignores=[]), CONNECT, "exec"), ns)
ok("der Connect-Dienst rechnet dieselbe Regel", ns["tunnel_id"](H) == tid, ns["tunnel_id"](H))

print("=== Preisgabe ===")
S = {"ceiling": "pseudonym", "salt": "abc", "tenants": {"T2": "names", "T3": "count"}}
ok("ohne eigene Wahl gilt die Obergrenze", fl.tenant_level(S, "T1") == "pseudonym")
ok("ein Mandant kann nicht mehr preisgeben als der Knoten", fl.tenant_level(S, "T2") == "pseudonym")
ok("weniger geht immer", fl.tenant_level(S, "T3") == "count")
ok("Obergrenze names: die Wahl des Mandanten gilt",
   fl.tenant_level({**S, "ceiling": "names"}, "T2") == "names")
ok("Vorgabe ohne Datei: pseudonym -- ohne Salz faellt es auf count",
   fl.ceiling({}) == "pseudonym" and fl.tenant_level({}, "T1") == "count")
ok("unbekannte Stufe zaehlt nicht", fl.tenant_level({**S, "tenants": {"T1": "alles"}}, "T1")
   == "pseudonym")
d = fl.Disclosure(S, {"T1": {"label": "ais"}})
p1 = d.tenant("T1")
ok("Pseudonym stabil, m- plus 10 Hex, nicht der Name", p1 == d.tenant("T1") and p1.startswith("m-")
   and len(p1) == 12 and "ais" not in p1, p1)
ok("anderes Salz, anderes Pseudonym (nicht zwischen Knoten verknuepfbar)",
   fl.Disclosure({**S, "salt": "xyz"}, {}).tenant("T1") != p1)

print("=== die Zeilen ===")
TENANTS = {"T-ais": {"label": "ais"}, "T-ops": {"label": "oaap-ops"}, "T-x": {"label": "kunde"}}
CONF = {
    "keys": {"oaapi01": {"hash": H, "tenant": "T-ais"},
             "frei": {"hash": hashlib.sha256(b"k2").hexdigest(), "tenant": "T-x"}},
    "connectors": {"oaapx01": {"endpoint": "https://geheim.example/", "paused": False,
                               "offers": {"mg": {"to": "https://mg.ais.oaap-demo.example/",
                                                 "path": "/v1"},
                                          "nas": {"to": "http://192.168.1.20:5000"}}}},
}
STATE = {"tunnels": {"oaapi01": {"connected": True, "since": "2026-11-14T18:00:00Z",
                                 "offers": [{"name": "mg", "kind": "http"}]}},
         "connectors": {"oaapx01": {"connected": False, "tunnel_id": tid,
                                    "last_error": "Cannot connect to host geheim.example:443"}}}
DESTS = {"T-ais": {"ais-oaapi01": {"target": {"via": "oaapi01/mg"}},
                   "gamingpc": {"target": {"through": "ais-ovc/gamingpc"}},
                   "draussen": {"target": {"direct": "https://api.example.com/"}}},
         "T-x": {"geheim-ziel": {"target": {"via": "frei/x"}}}}
INST = {"ais-chat": {"tenant": "T-ais", "destinations": {"llm": "ais-oaapi01"}},
        "ais-mg": {"tenant": "T-ais", "destinations": {"gpu": "gamingpc"}},
        "ais-ovc": {"tenant": "T-ais"},
        "kunde-app": {"tenant": "T-x", "destinations": {"x": "geheim-ziel"}}}
HOSTS = {"mg.ais.oaap-demo.example": "ais-mg", "ais-mg": "ais-mg"}
SET = {"ceiling": "names", "salt": "s", "tenants": {"T-x": "count"}}
resolve = lambda ref: ref  # noqa: E731
links, block, by_inst = fl.build(CONF, STATE, DESTS, INST, TENANTS, SET, HOSTS, resolve)
text = json.dumps([links, block, by_inst])
ok("nie Endpunkt, Angebotsadresse, Pfad oder direktes Ziel",
   not any(x in text for x in ("geheim.example", "192.168", "/v1", "api.example.com",
                               "mg.ais.oaap-demo")), text)
outer = [l for l in links if l.get("side") == "outer"]
inner = [l for l in links if l.get("side") == "inner"]
ok("aussen: der Tunnel mit ID, Name, Mandant, Zustand ok, Angebot mg",
   any(l["id"] == tid and l["label"] == "oaapi01" and l["tenant"] == "ais" and l["state"] == "ok"
       and l["offers"] == ["mg"] for l in outer), outer)
frei = [l for l in outer if l["label"] == "frei"][0]
ok("ein Tunnel, den eine Destination braucht und der nicht verbunden ist: error",
   frei["state"] == "error" and "tenant" not in frei, frei)
ok("innen: dieselbe ID, nicht verbunden = error, ohne Fehlertext (er nennt den Endpunkt)",
   inner and inner[0]["id"] == tid and inner[0]["state"] == "error"
   and "error" not in inner[0] and inner[0]["paused"] is False, inner)
ok("ein Angebot nennt die eigene Instanz -- oder `external`",
   inner[0]["offers"] == [{"name": "mg", "reaches": "ais-mg"},
                          {"name": "nas", "reaches": "external"}], inner[0]["offers"])
dest = {l["name"]: l for l in links if l["kind"] == "destination"}
ok("via: Tunnel per ID statt Name, Angebot, gebunden ais-chat",
   dest["ais-oaapi01"]["via"] == {"tunnel": tid, "offer": "mg"}
   and dest["ais-oaapi01"]["bound"] == ["ais-chat"] and dest["ais-oaapi01"]["tenant"] == "ais",
   dest.get("ais-oaapi01"))
ok("through: Tunnel-App und Ziel (auf names mit Namen)",
   dest["gamingpc"]["through"] == {"instance": "ais-ovc", "target": "gamingpc"}, dest.get("gamingpc"))
ok("direct bleibt draussen", "draussen" not in dest)
cnt = [l for l in links if l["kind"] == "destination" and l["name"].startswith("d-")]
ok("Mandant auf count: kein Mandant, Name pseudonym, gebunden nur als Zahl",
   len(cnt) == 1 and "tenant" not in cnt[0] and cnt[0]["bound"] == 1
   and "geheim-ziel" not in text, cnt)
ok("tenants: Anzahl 3, genannt nur die zwei ueber count",
   block["count"] == 3 and sorted(i["tenant"] for i in block["items"]) == ["ais", "oaap-ops"]
   and block["ceiling"] == "names", block)
ok("Instanzzeilen: Mandant nur, wo er genannt werden darf",
   by_inst.get("ais-chat") == "ais" and "kunde-app" not in by_inst, by_inst)
links_p, _b, _i = fl.build(CONF, STATE, DESTS, INST, TENANTS,
                           {"ceiling": "pseudonym", "salt": "s"}, HOSTS, resolve)
gp = [l for l in links_p if "through" in l][0]
ok("auf pseudonym: externes Ziel und Destination nur als Pseudonym (D4)",
   gp["through"]["target"].startswith("x-") and gp["name"].startswith("d-")
   and "gamingpc" not in json.dumps(links_p), gp)
st_old = {"connectors": {"oaapx01": {"connected": True}}}
links_o, _b, _i = fl.build(CONF, st_old, {}, {}, {}, SET, HOSTS, resolve)
ok("Connect-Dienst ohne tunnel_id (vor 0.1.281): keine innere Zeile statt einer falschen",
   not [l for l in links_o if l.get("side") == "inner"])

print("=== das Dokument ===")
doc = fleet_view.build_document("n", "0.1.281", [], "t", [], [], [], [],
                                links=links, tenants=block)
ok("Schema 0.10, links und tenants drin", doc["schema"] == "oaap.fleet.status/0.10"
   and doc["links"] == links and doc["tenants"] == block)
doc9 = fleet_view.build_document("n", "0.1.281", [], "t", [], [], [], [])
ok("ohne Halbkanten (Aufbau gescheitert): keine Felder statt halber",
   "links" not in doc9 and "tenants" not in doc9)

print("=== oaap fleet disclose (appctl) ===")
import tempfile                                     # noqa: E402
os.environ["OAAP_DATA_DIR"] = tempfile.mkdtemp(prefix="oaap-disclose-")
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services"))
sys.path.insert(0, os.path.join(HERE, "..", "platform"))
try:
    import appctl as a                              # noqa: E402
except ImportError as exc:
    a = None
    print(f"SKIP  appctl braucht {exc}")
if a is not None:
    a.save_tenants({"T1": {"label": "ais"}, "T2": {"label": "kunde"}})
    d0 = a.fleet_disclose_load()
    ok("erste Nutzung: Obergrenze pseudonym, ein Salz, Datei lesbar fuers Portal",
       d0["ceiling"] == "pseudonym" and len(d0["salt"]) == 32
       and (os.name == "nt" or oct(os.stat(a.FLEET_DISCLOSE_FILE).st_mode)[-3:] == "644"), d0)
    ok("das Salz bleibt bei jedem weiteren Lesen dasselbe", a.fleet_disclose_load()["salt"] == d0["salt"])
    refused = ""
    try:
        a.fleet_disclose("names", "ais")
    except SystemExit as exc:
        refused = str(exc.code)
    ok("ein Mandant ueber die Obergrenze: abgelehnt", refused != "" and "names" not in
       a.fleet_disclose_load()["tenants"].values(), refused)
    a.fleet_disclose("count", "kunde")
    a.fleet_disclose("names")
    a.fleet_disclose("names", "ais")
    d1 = a.fleet_disclose_load()
    ok("Obergrenze names, ais names, kunde count -- nach Mandanten-ID gespeichert",
       d1["ceiling"] == "names" and d1["tenants"] == {"T1": "names", "T2": "count"}, d1)
    a.fleet_disclose("default", "kunde")
    ok("default nimmt die eigene Wahl zurueck", "T2" not in a.fleet_disclose_load()["tenants"])
    logf = a.TENANT_LOG
    log = open(logf, encoding="utf-8").read() if logf and os.path.exists(logf) else ""
    ok("die Wahl eines Mandanten steht in SEINEM Protokoll", '"fleet.disclose"' in log
       and '"T1"' in log, (logf, log[-300:]))

print(f"\n{'ALLE GRUEN' if not fails else str(fails) + ' FEHLER'}")
sys.exit(1 if fails else 0)
