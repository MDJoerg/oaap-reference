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

print("=== die Seite Flotte: disclosure_view ===")
TN = {"T1": {"label": "ais", "name": "AIS GmbH"}, "T2": {"label": "kunde"},
      "T3": {"label": "cls"}}
v = fl.disclosure_view(S, TN)
by = {r["label"]: r for r in v["rows"]}
ok("Obergrenze und alle Mandanten fuer den Server-Admin, nach Kurzname sortiert",
   v["ceiling"] == "pseudonym" and v["ceiling_set"] and [r["label"] for r in v["rows"]]
   == ["ais", "cls", "kunde"], v)
ok("ohne eigene Wahl: folgt der Obergrenze, Pseudonym draussen",
   by["ais"]["own"] == "" and by["ais"]["level"] == "pseudonym"
   and by["ais"]["outward"] == fl.Disclosure(S, TN).tenant("T1") and by["ais"]["title"] == "AIS GmbH")
ok("eigene Wahl ueber der Obergrenze bleibt sichtbar, gilt aber nicht",
   by["kunde"]["own"] == "names" and by["kunde"]["capped"] and by["kunde"]["level"] == "pseudonym")
ok("Stufe count: draussen kein Name", by["cls"]["level"] == "count" and by["cls"]["outward"] == "")
ok("waehlbar sind nur Stufen bis zur Obergrenze", v["choosable"] == ["count", "pseudonym"])
mine = fl.disclosure_view(S, TN, only="T2")
ok("ein tenant_admin sieht nur seinen Mandanten -- keine Zeile, keine Zahl der anderen",
   [r["tid"] for r in mine["rows"]] == ["T2"] and "ais" not in json.dumps(mine), mine)
ok("ohne Datei: Vorgabe pseudonym, als nicht festgelegt erkennbar",
   fl.disclosure_view({}, TN)["ceiling"] == "pseudonym"
   and not fl.disclosure_view({}, TN)["ceiling_set"])

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
                   "gamingpc": {"target": {"through": "ovc/gamingpc"}},
                   "draussen": {"target": {"direct": "https://api.example.com/"}}},
         "T-x": {"geheim-ziel": {"target": {"via": "frei/x"}}}}
INST = {"ais-chat": {"tenant": "T-ais", "destinations": {"llm": "ais-oaapi01"}},
        "ais-mg": {"tenant": "T-ais", "destinations": {"gpu": "gamingpc"}},
        "ais-ovc": {"tenant": "T-ais", "name": "ovc"},
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
ok("through: Tunnel-App per Registerschluessel (Ziel sagt `ovc`, oaap-demo 14.11.), Ziel mit Namen",
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

print("=== was eine Tunnel-App sagt (0.11, RFC-0087 §8.1) ===")
ans = fl.read_link_answer({"v": 1, "tunnel": {"state": "ok", "since": "x", "remote": "1.2.3.4"},
                           "targets": {"gamingpc": {"state": "down", "addr": "10.8.0.5"},
                                       "nas": {"state": "kaputt"}, "pc2": "ok"},
                           "extra": "geheim"})
ok("Antwort gelesen: nur tunnel/targets, nur ok/down -- Rest bleibt auf dem Knoten",
   ans == {"tunnel": "ok", "targets": {"gamingpc": "down"}}, ans)
ok("keine Antwort: falsche Version, kein Objekt",
   fl.read_link_answer({"v": 2}) is None and fl.read_link_answer([]) is None
   and fl.read_link_answer(None) is None)
ok("Tunnel in unbekanntem Wort: unknown",
   fl.read_link_answer({"v": 1, "tunnel": {"state": "verbunden"}})["tunnel"] == "unknown")
TS = fl.through_state
ok("through_state: ohne Tuer nichts; paused/unknown durchgereicht",
   TS(None, "g") == "" and TS("paused", "g") == "paused" and TS("unknown", "g") == "unknown")
ok("through_state: Tunnel down schlaegt jedes Ziel",
   TS({"tunnel": "down", "targets": {"g": "ok"}}, "g") == "down-tunnel")
ok("through_state: Tunnel ok, Ziel ok/down/ungenannt",
   TS({"tunnel": "ok", "targets": {"g": "ok"}}, "g") == "ok"
   and TS({"tunnel": "ok", "targets": {"g": "down"}}, "g") == "down-target"
   and TS({"tunnel": "ok", "targets": {}}, "g") == "unknown")
ok("through_state: Tunnel unbekannt, Ziel ok -> unknown (kein falsches ok)",
   TS({"tunnel": "unknown", "targets": {"g": "ok"}}, "g") == "unknown")
REP = {"ais-ovc": {"tunnel": "ok", "targets": {"gamingpc": "down"}}}
links_r, _b, _i = fl.build(CONF, STATE, DESTS, INST, TENANTS, SET, HOSTS, resolve, reports=REP)
gr = [l for l in links_r if "through" in l][0]
ok("build: through traegt state down-target (Bericht unter dem Registerschluessel)",
   gr["through"] == {"instance": "ais-ovc", "target": "gamingpc", "state": "down-target"}, gr)
links_rp, _b, _i = fl.build(CONF, STATE, DESTS, INST, TENANTS,
                            {"ceiling": "pseudonym", "salt": "s"}, HOSTS, resolve, reports=REP)
grp = [l for l in links_rp if "through" in l][0]
ok("auf pseudonym: Zustand gefunden am echten Namen, hinaus geht das Pseudonym",
   grp["through"]["state"] == "down-target" and grp["through"]["target"].startswith("x-")
   and "gamingpc" not in json.dumps(links_rp), grp)
links_n, _b, _i = fl.build(CONF, STATE, DESTS, INST, TENANTS, SET, HOSTS, resolve,
                           reports={"andere-app": "paused"})
ok("build: Tunnel-App ohne Tuer -> through ohne state (wie vor 0.11)",
   "state" not in [l for l in links_n if "through" in l][0]["through"])

print("=== was eine App von ihren Assets sagt (0.12, RFC-0087 §8.2) ===")
RA = fl.read_assets_answer
raw = {"v": 1, "assets": [
    {"kind": "source", "name": "gaming", "state": "down", "destination": "GPU",
     "url": "http://10.8.0.1:11434/v1", "light": "green"},
    {"kind": "source", "name": "anthropic", "state": "idle"},
    {"kind": "source", "name": "gaming", "state": "ok"},
    {"kind": "model", "name": "chat-gaming", "state": "down", "uses": ["gaming", "fremd", 7],
     "destination": "gpu", "upstream": "llama3.3:70b"},
    {"kind": "key", "name": "laptop-joerg"},
    {"kind": "source", "name": "mit leer", "state": "ok"},
    {"kind": "source", "name": "komisch", "state": "verbunden"},
    "kein-objekt"]}
got = RA(raw)
g = {(a["kind"], a["name"]): a for a in got}
ok("gelesen: nur model/source mit gueltigem Namen, je einmal",
   sorted(g) == [("model", "chat-gaming"), ("source", "anthropic"), ("source", "gaming"),
                 ("source", "komisch")], sorted(g))
ok("nur die erlaubten Felder -- keine Adresse, Ampel, Modellkennung",
   all(set(a) <= {"kind", "name", "state", "uses", "destination"} for a in got)
   and "10.8.0.1" not in json.dumps(got) and "llama" not in json.dumps(got), got)
ok("uses nur auf Quellen derselben Antwort; destination nur an einer Quelle",
   g[("model", "chat-gaming")].get("uses") == ["gaming"]
   and "destination" not in g[("model", "chat-gaming")]
   and g[("source", "gaming")]["destination"] == "GPU", g)
ok("ein unbekanntes Zustandswort faellt weg", "state" not in g[("source", "komisch")])
ok("keine Antwort: falsche Version, keine Liste, kein Objekt",
   RA({"v": 2, "assets": []}) is None and RA({"v": 1}) is None and RA([]) is None
   and RA(None) is None)
ok("hoechstens 200", len(RA({"v": 1, "assets": [{"kind": "source", "name": f"q{i}"}
                                               for i in range(300)]})) == 200)
ASSETS = {"ais-mg": got}
links_a, _b, _i = fl.build(CONF, STATE, DESTS, INST, TENANTS, SET, HOSTS, resolve,
                           reports=REP, assets=ASSETS)
apps = [l for l in links_a if l["kind"] == "app"]
ok("eine app-Zeile fuer ais-mg mit Mandant und state ok",
   len(apps) == 1 and apps[0]["instance"] == "ais-mg" and apps[0]["tenant"] == "ais"
   and apps[0]["state"] == "ok", apps)
ga = {(a["kind"], a["name"]): a for a in apps[0]["assets"]}
ok("auf names: die Quelle geht ueber die Destination gamingpc (Bedarf gpu, GPU geschrieben)",
   ga[("source", "gaming")].get("destination") == "gamingpc", ga)
ok("der Modellname nutzt gaming, traegt seinen Zustand",
   ga[("model", "chat-gaming")] == {"kind": "model", "name": "chat-gaming", "state": "down",
                                    "uses": ["gaming"]}, ga)
ok("eine Quelle ohne Destination nennt keine", "destination" not in ga[("source", "anthropic")])
links_ap, _b, _i = fl.build(CONF, STATE, DESTS, INST, TENANTS,
                            {"ceiling": "pseudonym", "salt": "s"}, HOSTS, resolve,
                            reports=REP, assets=ASSETS)
ap = [l for l in links_ap if l["kind"] == "app"][0]
dp = [l for l in links_ap if "through" in l][0]
src_p = [a for a in ap["assets"] if a["kind"] == "source" and a.get("destination")][0]
mod_p = [a for a in ap["assets"] if a["kind"] == "model"][0]
ok("auf pseudonym: a-Pseudonyme, die Destination heisst wie ihre eigene Zeile",
   all(a["name"].startswith("a-") for a in ap["assets"]) and src_p["destination"] == dp["name"]
   and mod_p["uses"] == [src_p["name"]], ap)
ok("auf pseudonym: kein echter Name im Dokument",
   not any(x in json.dumps(links_ap) for x in ("chat-gaming", "anthropic", '"gaming"')), ap)
ok("dasselbe Asset, dasselbe Pseudonym (die Zentrale erkennt es wieder)",
   fl.build(CONF, STATE, DESTS, INST, TENANTS, {"ceiling": "pseudonym", "salt": "s"}, HOSTS,
            resolve, assets=ASSETS)[0][-1]["assets"] == ap["assets"])
links_k, _b, _i = fl.build(CONF, STATE, DESTS, INST, TENANTS, SET, HOSTS, resolve,
                           assets={"kunde-app": [{"kind": "source", "name": "q"}]})
ok("Mandant auf count: keine app-Zeile", not [l for l in links_k if l["kind"] == "app"])
links_s, _b, _i = fl.build(CONF, STATE, DESTS, INST, TENANTS, SET, HOSTS, resolve,
                           assets={"ais-mg": "paused", "ais-chat": "unknown", "weg": []})
st = {l["instance"]: l for l in links_s if l["kind"] == "app"}
ok("schlafend/stumm: paused bzw. unknown ohne Liste; unbekannte Instanz keine Zeile",
   st["ais-mg"] == {"kind": "app", "instance": "ais-mg", "tenant": "ais", "state": "paused"}
   and st["ais-chat"]["state"] == "unknown" and "assets" not in st["ais-chat"]
   and "weg" not in st, st)
INST2 = dict(INST, **{"ais-mg": {"tenant": "T-ais",
                                 "destinations": {"gpu": "gamingpc", "raus": "draussen"}}})
links_d, _b, _i = fl.build(CONF, STATE, DESTS, INST2, TENANTS, SET, HOSTS, resolve,
                           assets={"ais-mg": [{"kind": "source", "name": "a", "destination": "raus"},
                                              {"kind": "source", "name": "b",
                                               "destination": "nicht-gebunden"}]})
dd = [l for l in links_d if l["kind"] == "app"][0]["assets"]
ok("eine direkte (ohne eigene Zeile) oder ungebundene Destination wird nicht genannt",
   all("destination" not in a for a in dd), dd)
ok("ohne assets: keine app-Zeile (wie 0.11)",
   not [l for l in links if l["kind"] == "app"])

print("=== das Dokument ===")
doc = fleet_view.build_document("n", "0.1.281", [], "t", [], [], [], [],
                                links=links, tenants=block)
ok("Schema 0.12, links und tenants drin", doc["schema"] == "oaap.fleet.status/0.12"
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

    print("=== die Seite Flotte: fleet_disclose_job (Host) ===")
    J = a.fleet_disclose_job
    r = J({"op": "tenant", "args": {"level": "count"}}, "T2", "tenant_admin", "kunde-admin")
    ok("tenant_admin setzt seinen eigenen Mandanten",
       r[0] and a.fleet_disclose_load()["tenants"].get("T2") == "count" and r[2] == "T2", r)
    r1 = J({"op": "tenant", "args": {"level": "count", "tenant": "ais"}}, "T2", "tenant_admin", "x")
    r2 = J({"op": "tenant", "args": {"level": "count", "tenant": "gibtsnicht"}}, "T2",
           "tenant_admin", "x")
    ok("tenant_admin nennt einen fremden Mandanten: abgelehnt, derselbe Satz wie fuer keinen",
       not r1[0] and not r2[0] and r1[1].replace("ais", "?") == r2[1].replace("gibtsnicht", "?")
       and a.fleet_disclose_load()["tenants"].get("T1") == "names", (r1, r2))
    r = J({"op": "ceiling", "args": {"level": "count"}}, "T2", "tenant_admin", "x")
    ok("tenant_admin setzt keine Obergrenze", not r[0] and a.fleet_disclose_load()["ceiling"] == "names", r)
    r = J({"op": "tenant", "args": {"level": "count"}}, "T2", "admin", "x")
    ok("eine App-Rolle setzt gar nichts", not r[0], r)
    r = J({"op": "ceiling", "args": {"level": "pseudonym"}}, "T1", "server_admin", "root-admin")
    ok("server_admin setzt die Obergrenze", r[0] and a.fleet_disclose_load()["ceiling"] == "pseudonym", r)
    ok("... und die hoehere Wahl eines Mandanten bleibt gespeichert",
       a.fleet_disclose_load()["tenants"].get("T1") == "names")
    r = J({"op": "tenant", "args": {"level": "names", "tenant": "kunde"}}, "T1", "server_admin", "x")
    ok("ueber die Obergrenze auch fuer den server_admin: abgelehnt, mit dem Satz der Kommandozeile",
       not r[0] and "ceiling" in r[1] and a.fleet_disclose_load()["tenants"].get("T2") == "count", r)
    r = J({"op": "tenant", "args": {"level": "default", "tenant": "kunde"}}, "T1", "server_admin", "x")
    ok("server_admin nimmt die Wahl eines Mandanten zurueck",
       r[0] and "T2" not in a.fleet_disclose_load()["tenants"], r)
    r = J({"op": "alles", "args": {"level": "names"}}, "T1", "server_admin", "x")
    ok("unbekannte Operation: abgelehnt", not r[0], r)
    flog = open(a.FLEET_LOG, encoding="utf-8").read() if os.path.exists(a.FLEET_LOG) else ""
    ok("die Obergrenze aus dem Portal steht im Flottenprotokoll mit Person und Rolle",
       '"root-admin"' in flog and '"server_admin"' in flog, flog[-300:])

print(f"\n{'ALLE GRUEN' if not fails else str(fails) + ' FEHLER'}")
sys.exit(1 if fails else 0)
