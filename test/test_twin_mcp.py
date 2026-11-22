"""Der Zwilling als MCP-Quelle (oaap.data.twin 0.9 §2.16, RFC-0118 P6, 0.1.341).

    python3 test/test_twin_mcp.py

A. appctl: mit dem Profil `store` nennt die Quellenliste eines verbrauchenden
   Gateways die Quelle `twin` (Tür = Adresse, Erkundungs-Tür daneben). Die
   Tür fragt identity mit den Leserollen des Zwillings, dem Mandanten des
   Gateways und dem Scope des Gateways, geht zu twin:8000/mcp und SETZT
   X-OAAP-Twin-Tenant. Ohne `store` keine Quelle; eine Instanz `twin` mit
   Tür behält den Namen; Generalprobe und Wartung: nichts.
B. mcp_door (rein): Katalog für jeden, den die Tür durchlässt; ein Aufruf
   nur für eine Person mit Leserolle; drei lesende Werkzeuge mit festen
   Beschreibungen; Fehler sind Sätze, nie Daten.
C. app.py (Quelltext): /mcp liest den Mandanten nur aus der Kopfzeile der
   Tür und lehnt ohne sie ab; /mcp liegt nicht unter /twin/; das Modul
   steht im Dockerfile.
"""
import ast
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TWIN_DIR = os.path.join(HERE, "..", "platform", "services", "twin")
DATA = tempfile.mkdtemp(prefix="oaap-twin-mcp-test-")
os.environ["OAAP_DATA_DIR"] = os.path.join(DATA, "appctl")
sys.path.insert(0, os.path.join(HERE, "..", "platform"))
sys.path.insert(0, TWIN_DIR)

import appctl as a                                            # noqa: E402
import mcp_door                                               # noqa: E402

a.reload_gateway = lambda: None
os.makedirs(a.CADDY_APPS_DIR, exist_ok=True)
fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:700]}")


def record(name, tenant, app_id="crm", port=8101, mcp="", sources=False, **extra):
    c = f"oaap-app-{name}"
    r = {"app_id": app_id, "app_name": app_id, "version": "1.0.0",
         "channel": "production", "port": port, "container": c, "svc_port": 8000,
         "image": f"oaap-app/{app_id}:1.0.0", "tenant": tenant,
         "id": name.replace("-", "")[:12].ljust(12, "x"), "name": name,
         "services": [{"service": "", "container": c, "image": "", "build": "", "port": 8000}],
         "routes": [{"path": "/", "roles": ["user", "admin"]}], "visibility": {},
         "mcp": {"path": mcp} if mcp else {}, "mcp_sources": sources}
    r.update(extra)
    return r


def site(name):
    with open(os.path.join(a.CADDY_APPS_DIR, f"{name}.caddy"), encoding="utf-8") as f:
        return f.read()


def block(body, opener):
    lines = body.splitlines()
    for i, ln in enumerate(lines):
        if ln.strip() == opener:
            out, depth = [], 0
            for x in lines[i + 1:]:
                if x == "\t}" and depth == 0:
                    return out
                s = x.strip()
                depth += s.endswith("{") - (s == "}")
                out.append(s)
    return None


def write_all():
    for n, rec in a.load_registry()["instances"].items():
        a.write_app_caddy(n, rec)


# ------------------------------------------------------------------ A. appctl
print("A. Die Quelle twin auf der Site des Gateways")
tid = a.ensure_default_tenant()
reg = a.load_registry()
reg["instances"]["gw"] = record("gw", tid, "mcp-gateway", 8120, sources=True)
reg["instances"]["help"] = record("help", tid, "help", 8121, mcp="/mcp")
a.save_registry(reg)
a.save_profiles([])
write_all()
srcs = a.mcp_sources_of(a.load_registry()["instances"], "gw")
ok("ohne Profil store: keine Quelle twin", [s["name"] for s in srcs] == ["help"], srcs)
ok("… und keine Tür für twin", "/platform/mcp/to/twin" not in site("gw"))

a.save_profiles(["store"])
ok("Profil store gesetzt", a.has_profile("store"), a.load_profiles())
write_all()
insts = a.load_registry()["instances"]
srcs = a.mcp_sources_of(insts, "gw")
tw = next((s for s in srcs if s["name"] == "twin"), None)
door_url = f"http://{a.GATEWAY_CONTAINER}:8120/platform/mcp/to/twin"
ok("mit store: die Liste nennt twin", tw is not None, srcs)
tw = tw or {}
ok("… als Plattform-Quelle oaap.data.twin 0.9",
   tw.get("app") == "oaap.data.twin" and tw.get("channel") == "platform"
   and tw.get("version") == a.TWIN_MCP_VERSION, tw)
ok("… Tür und Adresse sind dieselbe Tür auf der Site des Gateways (kein Agentenschlüssel)",
   tw.get("door") == door_url and tw.get("address") == door_url, tw)
ok("… die Erkundungs-Tür steht daneben",
   tw.get("discover") == f"http://{a.GATEWAY_CONTAINER}:8120/platform/mcp/discover/twin", tw)
body = site("gw")
for opener, kind in (("handle /platform/mcp/to/twin {", "act"),
                     ("handle /platform/mcp/discover/twin {", "discover")):
    d = block(body, opener)
    ok(f"die Site trägt {opener}", d is not None, body[-2500:])
    d = d or []
    fa = next((x for x in d if x.startswith("uri /verify")), "")
    ok(f"… fragt identity mit den Leserollen, dem Mandanten und ?{kind}= des Gateways",
       "roles=admin,keyuser,tenant_admin,user" in fa and f"tenant={tid}" in fa
       and "instance=twin" in fa and f"{kind}=oaap.mcp.sources:gw" in fa
       and "groups=" not in fa, fa)
    ok("… geht zu twin:8000 auf /mcp",
       "rewrite * /mcp" in d and "reverse_proxy twin:8000 {" in d, d)
    sets = [x for x in d if x.startswith("request_header X-OAAP-Twin-Tenant")]
    ok("… SETZT X-OAAP-Twin-Tenant auf den Mandanten des Gateways, nach forward_auth",
       sets == [f"request_header X-OAAP-Twin-Tenant {tid}"]
       and d.index(sets[0]) > next(i for i, x in enumerate(d) if x.startswith("forward_auth")),
       d)
    ok("… und reicht Schlüssel und Act-Token nicht weiter",
       "request_header -Authorization" in d and "request_header -X-OAAP-Act-Token" in d, d)
ok("die Tür einer App-Quelle setzt keine Zwillings-Kopfzeile",
   not any("Twin-Tenant" in x for x in block(body, "handle /platform/mcp/to/help {") or []))
ok("die Site der Hilfe (keine mcp_sources) trägt keine twin-Tür",
   "/platform/mcp/to/twin" not in site("help"))
ok("die Site mit twin-Tür gilt nicht als veraltet", not a._site_identity_stale(body))

reg = a.load_registry()
reg["instances"]["twin"] = record("twin", tid, "twin-app", 8122, mcp="/rpc")
a.save_registry(reg)
write_all()
srcs = a.mcp_sources_of(a.load_registry()["instances"], "gw")
tws = [s for s in srcs if s["name"] == "twin"]
ok("eine Instanz twin mit Tür behält den Namen: eine Quelle, die App",
   len(tws) == 1 and tws[0]["app"] == "twin-app", tws)
d = block(site("gw"), "handle /platform/mcp/to/twin {") or []
ok("… und ihre Tür geht zur App, ohne Zwillings-Kopfzeile",
   any("oaap-app-twin:8000" in x for x in d) and not any("Twin-Tenant" in x for x in d), d)
reg = a.load_registry()
del reg["instances"]["twin"]
reg["instances"]["gw"]["maintenance"] = {"since": "2026-11-21T00:00:00Z", "by": "root"}
a.save_registry(reg)
write_all()
ok("in Wartung: keine twin-Tür", "/platform/mcp/to/twin" not in site("gw"))
reg = a.load_registry()
reg["instances"]["gw"].pop("maintenance")
reg["instances"]["gw"]["rehearsal"] = {"of": "gw-prod", "since": "2026-11-21T00:00:00Z"}
a.save_registry(reg)
ok("Generalprobe: keine Quellen, also auch kein twin",
   a.mcp_sources_of(a.load_registry()["instances"], "gw") == [])

# ---------------------------------------------------------------- B. mcp_door
print("")
print("B. mcp_door")


class Store:
    def __init__(self):
        self.calls = []

    def types(self):
        self.calls.append("types")
        return [{"key": "firma", "kind": "object", "title": "Firma"}]

    def search(self, type_key, words, offset, limit):
        self.calls.append(("search", type_key, words, offset, limit))
        return [{"id": f"urn:oaap:obj:{i:08d}-0000-4000-8000-000000000000", "type": "firma",
                 "title": f"Firma {i}", "owner": "x"} for i in range(offset, offset + limit)][:60]

    def object(self, oid, at):
        self.calls.append(("object", oid, at))
        if oid.startswith("0"):
            return {"id": f"urn:oaap:obj:{oid}", "title": "Brandeis", "groups": {}}
        return None


def rq(method, mid=1, **params):
    return {"jsonrpc": "2.0", "id": mid, "method": method, "params": params}


PERSON = {"user": "anna", "roles": {"user"}}
MACHINE = {"user": "instance:gw", "roles": {"user"}}
st = Store()
init = mcp_door.answer(rq("initialize", protocolVersion="2025-06-18"), MACHINE, st)
ok("initialize antwortet auch dem Gateway (Erkundung)",
   init["result"]["protocolVersion"] == "2025-06-18"
   and init["result"]["serverInfo"]["name"] == "oaap-twin", init)
tl = mcp_door.answer(rq("tools/list"), MACHINE, st)["result"]["tools"]
ok("tools/list: die drei lesenden und (0.16, §2.28) die drei schreibenden Werkzeuge",
   [t["name"] for t in tl] == ["twin_types", "twin_search", "twin_object", "twin_set_group",
                               "twin_add_period", "twin_set_status"], tl)
ok("… die lesenden lesend, die schreibenden readOnlyHint false; keins zerstörend, keine "
   "offene Welt",
   all(t["annotations"]["readOnlyHint"] is (t["name"] in ("twin_types", "twin_search",
                                                          "twin_object"))
       and t["annotations"]["destructiveHint"] is False
       and t["annotations"]["openWorldHint"] is False for t in tl))
ok("… Beschreibungen fest (zweimal gelesen gleich)",
   tl == mcp_door.answer(rq("tools/list"), PERSON, st)["result"]["tools"])
r = mcp_door.answer(rq("tools/call", name="twin_types"), MACHINE, st)["result"]
ok("ohne Person: Aufruf abgelehnt, Satz statt Daten, der Speicher wird nicht gefragt",
   r["isError"] is True and "Person" in r["content"][0]["text"] and st.calls == [], r)
r = mcp_door.answer(rq("tools/call", name="twin_types"),
                    {"user": "remote:hub", "roles": {"user"}}, st)["result"]
ok("ein entfernter Leser ist keine Person", r["isError"] is True and st.calls == [])
r = mcp_door.answer(rq("tools/call", name="twin_types"), {"user": "bob", "roles": {"guest"}}, st)["result"]
ok("Person ohne Leserolle: abgelehnt", r["isError"] is True and "Rolle" in r["content"][0]["text"], r)
r = mcp_door.answer(rq("tools/call", name="twin_types"), PERSON, st)["result"]
ok("Person mit Rolle user: twin_types liefert die Typen",
   r["isError"] is False and '"firma"' in r["content"][0]["text"], r)
r = mcp_door.answer(rq("tools/call", name="twin_search",
                       arguments={"q": "  a  b c d e f g h i j ", "type": "firma", "offset": 5}),
                    PERSON, st)["result"]
ok("twin_search: höchstens acht Wörter, Typ, offset, eine Zeile mehr gefragt",
   st.calls[-1] == ("search", "firma", ["a", "b", "c", "d", "e", "f", "g", "h"], 5, 51), st.calls[-1])
import json                                                   # noqa: E402
res = json.loads(r["content"][0]["text"])
ok("… 50 je Seite und more=true, wenn es weitergeht",
   len(res["objects"]) == 50 and res["more"] is True and res["offset"] == 5, res.get("more"))
r = mcp_door.answer(rq("tools/call", name="twin_search", arguments={"offset": "x"}), PERSON, st)["result"]
ok("twin_search: offset keine Zahl → Satz", r["isError"] is True)
r = mcp_door.answer(rq("tools/call", name="twin_object",
                       arguments={"id": "urn:oaap:obj:0ABCDEF0-0000-4000-8000-000000000000",
                                  "at": "2026-11-21"}), PERSON, st)["result"]
ok("twin_object: URN angenommen, kleingeschrieben, Tag weitergereicht",
   r["isError"] is False and st.calls[-1] == ("object", "0abcdef0-0000-4000-8000-000000000000",
                                               "2026-11-21"), (r, st.calls[-1]))
for bad in ({"id": "nicht-da"}, {"id": "0abcdef0-0000-4000-8000-000000000000", "at": "morgen"},
            {}):
    r = mcp_door.answer(rq("tools/call", name="twin_object", arguments=bad), PERSON, st)["result"]
    ok(f"twin_object {bad}: Satz statt Absturz", r["isError"] is True, r)
r = mcp_door.answer(rq("tools/call", name="twin_object",
                       arguments={"id": "9abcdef0-0000-4000-8000-000000000000"}), PERSON, st)["result"]
ok("twin_object: unbekanntes Objekt → Satz", r["isError"] is True and "nicht" in r["content"][0]["text"])


# -- the writing tools (0.16, §2.28): roles, arguments; the rules are the twin's (DB test)
class WStore(Store):
    def set_group(self, user, oid, group, attrs):
        self.calls.append(("set_group", user, oid, group, attrs))
        return {"written": sorted(attrs)}

    def add_period(self, user, oid, group, body):
        self.calls.append(("add_period", user, oid, group, body))
        return {"written": True}

    def set_status(self, user, oid, to, reason):
        self.calls.append(("set_status", user, oid, to, reason))
        if to == "x":
            raise mcp_door.Refused("'active' -> 'x' is not a declared transition")
        return {"to": to, "written": True}


ws = WStore()
OID = "0abcdef0-0000-4000-8000-000000000000"
W_ARGS = {"twin_set_group": {"id": OID, "group": "werk.notiz", "attributes": {"notiz": "a"}},
          "twin_add_period": {"id": OID, "group": "werk.kpi", "attribute": "co2e", "value": 5,
                              "valid_from": "2025-01-01", "valid_to": "2025-12-31"},
          "twin_set_status": {"id": OID, "to": "active", "reason": "abgenommen"}}
for name, args in W_ARGS.items():
    r = mcp_door.answer(rq("tools/call", name=name, arguments=args), PERSON, ws)["result"]
    ok(f"KÖDER {name}: eine Person mit Leserolle (user), aber ohne Schreibrolle -> Satz, "
       "nichts gefragt", r["isError"] is True and "schreiben" in r["content"][0]["text"]
       and ws.calls == [], r)
    r = mcp_door.answer(rq("tools/call", name=name, arguments=args), MACHINE, ws)["result"]
    ok(f"KÖDER {name}: kein Gateway, keine Maschine", r["isError"] is True and ws.calls == [])
KEY = {"user": "kim", "roles": {"keyuser"}}
r = mcp_door.answer(rq("tools/call", name="twin_set_group", arguments=W_ARGS["twin_set_group"]),
                    KEY, ws)["result"]
ok("twin_set_group: keyuser schreibt als er selbst, die id kleingeschrieben",
   r["isError"] is False and ws.calls[-1] == ("set_group", "kim", OID, "werk.notiz",
                                              {"notiz": "a"}), ws.calls[-1:])
r = mcp_door.answer(rq("tools/call", name="twin_add_period", arguments=W_ARGS["twin_add_period"]),
                    KEY, ws)["result"]
ok("twin_add_period: nur die bekannten Felder gehen weiter",
   r["isError"] is False and ws.calls[-1][4] == {"attribute": "co2e", "value": 5,
                                                 "valid_from": "2025-01-01",
                                                 "valid_to": "2025-12-31"}, ws.calls[-1:])
r = mcp_door.answer(rq("tools/call", name="twin_set_status", arguments=dict(
    W_ARGS["twin_set_status"], to="x")), KEY, ws)["result"]
ok("twin_set_status: die Ablehnung des Zwillings kommt als Satz zurück",
   r["isError"] is True and "declared transition" in r["content"][0]["text"], r)
r = mcp_door.answer(rq("tools/call", name="twin_set_group", arguments={"id": OID,
                    "group": "werk.notiz", "attributes": {}}), KEY, ws)["result"]
ok("KÖDER twin_set_group ohne Werte: Satz", r["isError"] is True, r)
init = mcp_door.answer(rq("initialize"), PERSON, ws)["result"]["instructions"]
ok("die Anweisungen der Quelle nennen die schreibenden Werkzeuge und die Bestätigung",
   "twin_set_status" in init and "bestätigt" in init, init)


class Broken(Store):
    def types(self):
        raise RuntimeError("db weg: geheim")


r = mcp_door.answer(rq("tools/call", name="twin_types"), PERSON, Broken())["result"]
ok("ein Fehler im Speicher wird ein Satz ohne Einzelheiten",
   r["isError"] is True and "geheim" not in r["content"][0]["text"], r)
e = mcp_door.answer(rq("tools/call", name="twin_write"), PERSON, st)
ok("unbekanntes Werkzeug → JSON-RPC-Fehler", e.get("error", {}).get("code") == -32602, e)
ok("unbekannte Methode → -32601", mcp_door.answer(rq("sampling/x"), PERSON, st)["error"]["code"] == -32601)
ok("Benachrichtigung → keine Antwort",
   mcp_door.answer({"jsonrpc": "2.0", "method": "notifications/initialized"}, PERSON, st) is None)
ok("who_from liest Benutzer und Rollen",
   mcp_door.who_from({"X-OAAP-User": " anna ", "X-OAAP-Roles": "user, admin"})
   == {"user": "anna", "roles": {"user", "admin"}})

# ------------------------------------------------------------ C. app.py
print("")
print("C. Der Dienst (Quelltext)")
src = open(os.path.join(TWIN_DIR, "app.py"), encoding="utf-8").read()
ast.parse(src)
fn = "def " + src.split("\ndef mcp_source(", 1)[1].split("\n@app.", 1)[0]
ok("/mcp ist POST auf dem Dienst, nicht unter /twin/",
   '@app.post("/mcp")' in src and '"/twin/mcp"' not in src)
ok("der Mandant kommt nur aus X-OAAP-Twin-Tenant, geprüft, sonst 403",
   "request.headers.get(TWIN_TENANT_HEADER)" in fn and "TENANT_ID_RE.match(tenant_id)" in fn
   and "403" in fn and "request.args" not in fn and "msg.get(\"tenant" not in fn, fn)
ok("… der Speicher wird an diesen Mandanten gebunden", "_McpStore(tenant_id)" in fn)
store_src = src.split("class _McpStore:", 1)[1].split("\n@app.", 1)[0]
ok("die Suche nutzt dieselbe Titelregel wie die App-Liste",
   "_title_like(words)" in store_src and "aliases WHERE unmerged_at IS NULL" in store_src)
ok("der Speicher öffnet nur die Verbindung seines Mandanten",
   store_src.count("get_conn(") == 1 and "get_conn(self.tenant_id)" in store_src)
ok("die schreibenden Werkzeuge rufen dieselben Funktionen wie die Portal-Routen (§2.28)",
   all(f in src.split("def _mcp_set_group", 1)[1].split("\n@app.", 1)[0]
       for f in ("person_group_write(", "person_period_write(", "person_status_change("))
   and "person_group_write(conn, tenant_id, username, obj_id, group_key, body)" in src)
docker = open(os.path.join(TWIN_DIR, "Dockerfile"), encoding="utf-8").read()
ok("mcp_door.py steht im Dockerfile", "mcp_door.py" in docker)

print("")
print("ALLE PRUEFUNGEN BESTANDEN" if not fails else f"{fails} FEHLER")
sys.exit(1 if fails else 0)
