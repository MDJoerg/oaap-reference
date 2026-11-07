#!/usr/bin/env python3
"""Die Clients eines MCP-Gateways (RFC-0073 Stufe 3, oaap.core.tenant 2.9.1,
oaap.apps.runtime 2.32.2).

    python3 test/test_mcp_idp.py

Gegen einen Papier-Keycloak, ohne Docker. Jede Zeile hat einen Koeder:

A. Der Konnektor (services/idp_admin.py)
   1. Namen, Bodies und Plan: Version zuerst, kein DELETE, die Formen aus
      der Messung von Stufe 2 (vertraulich nur Tausch; oeffentlich, PKCE,
      nur localhost:33418; Audience-Mapper je Richtung).
   2. Anlegen, zweiter Lauf schreibt nichts.
   3. Kein Realm, kein Plattform-Client: abgelehnt, nichts geschrieben.
   4. Fremde Art unter einem der Namen: abgelehnt, unveraendert, und der
      ANDERE Client wird auch nicht angelegt (nichts halb).
   5. Vorhandener, abgeschalteter Client: wieder an, Tausch an, Mapper
      nachgetragen, sonst nichts angefasst.
   6. Abschalten: PUT enabled false, nie DELETE; fehlender Client ist kein
      Fehler; danach wieder einschalten geht.
   7. 403 heisst "nicht unserer", falsche Version bricht vor jedem Schreiben ab.
B. Die Plattform (appctl.py)
   8. Ablehnungen vor jedem Aufruf (keine Quellen, Generalprobe, kein
      Anbieter, von Hand, anderer Konnektor, Konnektor weg).
   9. Probelauf ruft nichts; Einrichten schreibt fuenf Variablen, Register
      ohne Geheimnis, eine Zeile im Mandantenprotokoll, Container neu --
      beim zweiten Mal nicht mehr; Abschalten nimmt alles zurueck.
  10. Plattform-Variablen sind reserviert und gehen nicht in eine
      aufgehobene instance.env; beim Entfernen werden die Clients
      abgeschaltet oder die Namen genannt.
  11. `tenant check` (D13): fehlender Plattform-Client ist ein Befund,
      ein nicht erreichbarer Anbieter wird genannt.
  12. Der Portal-Auftrag verlangt server_admin, bevor irgendetwas laeuft.
"""
import contextlib
import copy
import io as _io
import json
import os
import re
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["OAAP_DATA_DIR"] = tempfile.mkdtemp(prefix="oaap-mcp-idp-test-")
sys.path.insert(0, os.path.join(HERE, "..", "platform"))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services"))

import appctl as m                                             # noqa: E402
import idp_admin as a                                          # noqa: E402

PIN = a.connector_of("keycloak")["pinned"]
fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:600]}")


S = {}


def reset(realms=("vuebung",), platform="oaap-node", version=PIN):
    S.clear()
    S.update({"realms": set(realms), "clients": {}, "calls": [], "writes": [],
              "version": version, "forbidden": set()})
    if platform:
        add_client(platform, publicClient=False)


def add_client(cid, realm="vuebung", **kw):
    uid = "cid-%d" % (len(S["clients"]) + 1)
    S["clients"][uid] = dict({"id": uid, "clientId": cid, "realm": realm,
                              "enabled": True}, **kw)
    return uid


def by_id(cid):
    return next((c for c in S["clients"].values() if c["clientId"] == cid), None)


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *_a):
        pass

    def _json(self, doc, status=200):
        raw = json.dumps(doc).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except ValueError:
            return {}

    def _realm(self):
        mm = re.match(r"^/admin/realms/([^/?]+)", self.path)
        return mm.group(1) if mm else ""

    def _guard(self):
        if self.headers.get("Authorization") != "Bearer fake-admin-token":
            return self._json({}, 401) or True
        if self._realm() in S["forbidden"] and "/clients" in self.path:
            return self._json({}, 403) or True
        return False

    def do_GET(self):
        S["calls"].append("GET " + self.path)
        path = self.path.split("?")[0]
        if self._guard():
            return
        if path == "/admin/serverinfo":
            return self._json({"systemInfo": {"version": S["version"]}})
        mm = re.match(r"^/admin/realms/([^/]+)$", path)
        if mm:
            return self._json({}, 200 if mm.group(1) in S["realms"] else 404)
        mm = re.match(r"^/admin/realms/([^/]+)/clients$", path)
        if mm:
            want = re.search(r"clientId=([^&]+)", self.path)
            want = want.group(1) if want else ""
            return self._json([copy.deepcopy(c) for c in S["clients"].values()
                               if c["clientId"] == want and c["realm"] == mm.group(1)])
        mm = re.match(r"^/admin/realms/[^/]+/clients/([^/]+)/client-secret$", path)
        if mm:
            return self._json({"type": "secret", "value": "geheim-" + mm.group(1)})
        return self._json({}, 404)

    def do_POST(self):
        S["calls"].append("POST " + self.path)
        body = self._body()
        if self.path.endswith("/protocol/openid-connect/token"):
            return self._json({"access_token": "fake-admin-token"})
        if self._guard():
            return
        S["writes"].append("POST " + self.path)
        mm = re.match(r"^/admin/realms/([^/]+)/clients$", self.path)
        if mm:
            add_client(body["clientId"], realm=mm.group(1),
                       **{k: v for k, v in body.items() if k != "clientId"})
            return self._json({}, 201)
        mm = re.match(r"^/admin/realms/[^/]+/clients/([^/]+)/protocol-mappers/models$",
                      self.path)
        if mm and mm.group(1) in S["clients"]:
            mps = S["clients"][mm.group(1)].setdefault("protocolMappers", [])
            if any(x["name"] == body["name"] for x in mps):
                return self._json({"errorMessage": "exists"}, 409)
            mps.append(body)
            return self._json({}, 201)
        return self._json({}, 404)

    def do_PUT(self):
        S["calls"].append("PUT " + self.path)
        body = self._body()
        if self._guard():
            return
        S["writes"].append("PUT " + self.path)
        mm = re.match(r"^/admin/realms/[^/]+/clients/([^/]+)$", self.path)
        if mm and mm.group(1) in S["clients"]:
            keep = S["clients"][mm.group(1)].get("protocolMappers")
            S["clients"][mm.group(1)].update(body)
            if keep is not None:
                S["clients"][mm.group(1)]["protocolMappers"] = keep
            return self._json({}, 204)
        return self._json({}, 404)

    def do_DELETE(self):
        S["calls"].append("DELETE " + self.path)
        S["writes"].append("DELETE " + self.path)
        return self._json({}, 204)


srv = HTTPServer(("127.0.0.1", 0), Fake)
BASE = f"http://127.0.0.1:{srv.server_port}"
threading.Thread(target=srv.serve_forever, daemon=True).start()
GW, SI = a.mcp_clients_for("vuebung-mcpgw")


def admin():
    return a.Admin("keycloak", BASE, "client", "oaap-admin", "V0llmacht-lang-genug")


def make(**kw):
    return admin().mcp_clients(kw.get("space", "vuebung"), GW, SI,
                               kw.get("platform", "oaap-node"), accept_version=PIN)


def aud_of(c):
    return [mp["config"]["included.client.audience"] for mp in c.get("protocolMappers", [])
            if mp.get("protocolMapper") == "oidc-audience-mapper"]


# ------------------------------------------------------------------ A.1
print("A.1 Namen, Bodies, Plan")
ok("Namen nach der Instanz", (GW, SI) == ("oaap-mcp-vuebung-mcpgw",
                                          "oaap-mcp-vuebung-mcpgw-login"))
ok("KOEDER: Grossbuchstaben und Fremdzeichen werden zu Bindestrichen",
   a.mcp_clients_for("Gw x/1") == ("oaap-mcp-gw-x-1", "oaap-mcp-gw-x-1-login")
   and a.mcp_clients_for("") == ("", ""))
ok("der Konnektor erklaert mcp_clients, und es ist ein bekanntes Verb",
   a.declares("keycloak", "mcp_clients") and "mcp_clients" in a.KNOWN_VERBS
   and a.kind_refusal("keycloak") == "" and a.mcp_refusal("keycloak") == "")
plan = a.mcp_clients_plan("keycloak", "vuebung", GW, SI, "oaap-node")
ok("der Plan ist zulaessig, Version zuerst, kein DELETE",
   a.plan_refusal(plan) == "" and plan[0]["verb"] == "version"
   and all(s["method"] in ("GET", "POST", "PUT") for s in plan), plan)
ok("er fragt Realm und Plattform-Client, bevor er schreibt",
   [s["writes"] for s in plan[:3]] == [False] * 3
   and "clientId=oaap-node" in plan[2]["path"], plan[:3])
off_plan = a.mcp_off_plan("keycloak", "vuebung", [GW, SI])
ok("der Abschalt-Plan: Version zuerst, nur GET und PUT",
   a.plan_refusal(off_plan) == "" and {s["method"] for s in off_plan} == {"GET", "PUT"})
g = a._keycloak_mcp_gateway_body(GW, "oaap-node")
ok("Gateway-Client: vertraulich, kein Browser, kein Passwort, kein Dienstkonto",
   g["publicClient"] is False and not g["standardFlowEnabled"]
   and not g["directAccessGrantsEnabled"] and not g["serviceAccountsEnabled"]
   and not g["implicitFlowEnabled"] and g["redirectUris"] == [], g)
ok("... Tausch an, Audience = Plattform-Client",
   g["attributes"] == {"standard.token.exchange.enabled": "true"} and aud_of(g) == ["oaap-node"])
si = a._keycloak_mcp_signin_body(SI, GW, a.MCP_SIGNIN_REDIRECTS)
ok("Anmelde-Client: oeffentlich, Code + PKCE S256, kein Passwort-Grant",
   si["publicClient"] is True and si["standardFlowEnabled"]
   and not si["directAccessGrantsEnabled"] and not si["implicitFlowEnabled"]
   and si["attributes"] == {"pkce.code.challenge.method": "S256"}, si)
ok("... Ruecksprung NUR http://localhost:33418/callback, Audience = Gateway",
   si["redirectUris"] == ["http://localhost:33418/callback"] and si["webOrigins"] == []
   and aud_of(si) == [GW], si)
ok("Audience-Mapper nur ins Access-Token",
   si["protocolMappers"][0]["config"]["access.token.claim"] == "true"
   and si["protocolMappers"][0]["config"]["id.token.claim"] == "false")
ok("has_audience_mapper erkennt nach Wirkung, nicht nach Namen",
   a.has_audience_mapper("keycloak", {"protocolMappers": [
       dict(g["protocolMappers"][0], name="von-hand")]}, "oaap-node")
   and not a.has_audience_mapper("keycloak", g, "anderer"))

# ------------------------------------------------------------------ A.2
print("A.2 Anlegen")
reset()
good, res, msg = make()
ok("gelingt", good, msg)
gc, sc = by_id(GW), by_id(SI)
ok("beide da, in diesem Realm, mit den Formen", gc and sc
   and gc["publicClient"] is False and sc["publicClient"] is True
   and gc["attributes"]["standard.token.exchange.enabled"] == "true"
   and aud_of(gc) == ["oaap-node"] and aud_of(sc) == [GW], S["clients"])
ok("das Ergebnis traegt, was die Plattform braucht",
   res["issuer"] == BASE + "/realms/vuebung" and res["audience"] == GW
   and res["signin"] == SI and res["platform_audience"] == "oaap-node"
   and res["secret"] == "geheim-" + gc["id"], res)
ok("der Plattform-Client ist nicht angefasst",
   not any(by_id("oaap-node")["id"] in w for w in S["writes"]), S["writes"])
S["writes"].clear()
good, res2, msg = make()
ok("zweiter Lauf gelingt und schreibt nichts", good and S["writes"] == [], S["writes"])

# ------------------------------------------------------------------ A.3
print("A.3 Kein Realm, kein Plattform-Client")
reset(realms=())
good, _r, msg = make()
ok("ohne Realm: abgelehnt, nichts geschrieben", not good and "makes no space" in msg
   and S["writes"] == [], (msg, S["writes"]))
reset(platform="")
good, _r, msg = make()
ok("KOEDER (Befund 1 von Stufe 2): ohne Plattform-Client abgelehnt, mit dem Weg",
   not good and "oaap idp provision" in msg and S["writes"] == [], (msg, S["writes"]))

# ------------------------------------------------------------------ A.4
print("A.4 Fremde Art unter unserem Namen")
reset()
add_client(SI, publicClient=True)
add_client(GW, publicClient=True, description="jemandes")
before = copy.deepcopy(by_id(GW))
good, _r, msg = make()
ok("oeffentlicher Client unter dem Gateway-Namen: abgelehnt", not good
   and "PUBLIC client" in msg, msg)
ok("... unveraendert, und nichts geschrieben", by_id(GW) == before and S["writes"] == [],
   S["writes"])
reset()
add_client(SI, publicClient=False)
good, _r, msg = make()
ok("KOEDER: vertraulicher unter dem Anmelde-Namen: abgelehnt, und der Gateway-Client "
   "wurde NICHT schon angelegt", not good and "CONFIDENTIAL" in msg
   and by_id(GW) is None and S["writes"] == [], (msg, S["writes"]))

# ------------------------------------------------------------------ A.5
print("A.5 Vorhandene Clients")
reset()
add_client(GW, publicClient=False, enabled=False, description="meins",
           attributes={"x": "y"})
add_client(SI, publicClient=True, redirectUris=["http://localhost:9999/cb"])
good, _r, msg = make()
gc, sc = by_id(GW), by_id(SI)
ok("gelingt", good, msg)
ok("Gateway-Client wieder an, Tausch an, Mapper nachgetragen",
   gc["enabled"] is True and gc["attributes"]["standard.token.exchange.enabled"] == "true"
   and aud_of(gc) == ["oaap-node"], gc)
ok("... sonst nichts angefasst (Beschreibung, eigenes Attribut)",
   gc["description"] == "meins" and gc["attributes"]["x"] == "y", gc)
ok("Anmelde-Client: unser Ruecksprung dazu, der alte bleibt",
   sc["redirectUris"] == ["http://localhost:9999/cb", "http://localhost:33418/callback"]
   and aud_of(sc) == [GW], sc)
ok("kein DELETE", not any(w.startswith("DELETE") for w in S["writes"]), S["writes"])

# ------------------------------------------------------------------ A.6
print("A.6 Abschalten")
reset()
make()
S["writes"].clear()
adm = admin()
good, msg = adm.mcp_clients_off("vuebung", [GW, SI], PIN)
ok("gelingt: beide abgeschaltet, keiner geloescht", good
   and by_id(GW)["enabled"] is False and by_id(SI)["enabled"] is False
   and len(S["clients"]) == 3 and all(w.startswith("PUT") for w in S["writes"]),
   (msg, S["writes"]))
ok("KOEDER: nie ein DELETE gesendet", not any(c.startswith("DELETE") for c in S["calls"]))
S["writes"].clear()
good, _msg = admin().mcp_clients_off("vuebung", [GW, SI, "gibt-es-nicht"], PIN)
ok("zweites Mal und ein fehlender Client: gelingt, schreibt nichts",
   good and S["writes"] == [], S["writes"])
good, _r, msg = make()
ok("wieder einschalten", good and by_id(GW)["enabled"] and by_id(SI)["enabled"], msg)
states, err = admin().client_states("vuebung", [GW, "oaap-node", "weg"])
ok("client_states: enabled / absent", err == "" and states == {
    GW: "enabled", "oaap-node": "enabled", "weg": "absent"}, states)

# ------------------------------------------------------------------ A.7
print("A.7 Fremd und falsche Version")
reset()
S["forbidden"].add("vuebung")
good, _r, msg = make()
ok("403 heisst 'nicht unserer', nicht 'fehlt'", not good and "not ours" in msg
   and S["writes"] == [], msg)
reset(version="99.0.0")
good, _r, msg = admin().mcp_clients("vuebung", GW, SI, "oaap-node")
ok("KOEDER: falsche Version bricht vor jedem Schreiben ab", not good
   and S["writes"] == [] and not any("/clients" in c for c in S["calls"]), (msg, S["calls"]))

# ------------------------------------------------------------------ B
print("B.8 Die Plattform lehnt vorher ab")
reset()
TID = "t-vuebung000"
tenants = m.load_tenants()
tenants[TID] = {"label": "vuebung", "name": "Übung",
                "idp": {"kind": "oidc", "issuer": BASE + "/realms/vuebung",
                        "client_id": "oaap-node", "connector": "auth", "space": "vuebung"}}
tenants["t-hand0000000"] = {"label": "hand", "name": "Hand",
                            "idp": {"kind": "oidc", "issuer": "https://x/realms/hand",
                                    "client_id": "c"}}
tenants["t-leer0000000"] = {"label": "leer", "name": "Leer"}
m.save_tenants(tenants)
m.save_idp_connectors({"auth": {"kind": "keycloak", "base_url": BASE, "auth": "client",
                                "admin_id": "oaap-admin",
                                "admin_secret": "V0llmacht-lang-genug",
                                "accept_version": ""}})


def rec(name, tenant, sources=True, **extra):
    c = f"oaap-app-{name}"
    r = {"app_id": "mcp-gateway", "version": "0.6.0", "channel": "production",
         "port": 8120, "container": c, "svc_port": 8000, "tenant": tenant,
         "id": name.replace("-", "")[:12].ljust(12, "x"), "name": name,
         "services": [{"service": "", "container": c, "image": "", "build": "", "port": 8000}],
         "routes": [{"path": "/", "roles": ["admin"]}], "visibility": {},
         "mcp": {}, "mcp_sources": sources, "storage": []}
    r.update(extra)
    return r


reg = m.load_registry()
reg.setdefault("instances", {})
reg["instances"]["vuebung-mcpgw"] = rec("vuebung-mcpgw", TID)
reg["instances"]["vuebung-crm"] = rec("vuebung-crm", TID, sources=False)
reg["instances"]["vuebung-gw-probe"] = rec("vuebung-gw-probe", TID,
                                           rehearsal={"of": "vuebung-mcpgw",
                                                      "expires": "2099-01-01T00:00:00Z"})
reg["instances"]["hand-gw"] = rec("hand-gw", "t-hand0000000")
reg["instances"]["leer-gw"] = rec("leer-gw", "t-leer0000000")
m.save_registry(reg)
RECREATED = []
m.recreate_instance_containers = lambda name, *a_, **k: RECREATED.append(name)
m.config_view_write = lambda *a_, **k: None

for inst, conn, want in (("nix", "", "no instance"),
                         ("vuebung-crm", "", "does not read MCP sources"),
                         ("vuebung-gw-probe", "", "rehearsal"),
                         ("leer-gw", "", "has no provider yet"),
                         ("hand-gw", "", "named by hand"),
                         ("vuebung-mcpgw", "anderer", "not by 'anderer'")):
    S["calls"].clear()
    good, said = m.mcp_idp_apply(inst, connector_name=conn, out=lambda *_: None)
    ok(f"{inst}{' / ' + conn if conn else ''}: abgelehnt ({want}), nichts gerufen",
       not good and want in said and S["calls"] == [], (said, S["calls"]))
held = m.load_idp_connectors()
m.save_idp_connectors({})
good, said = m.mcp_idp_apply("vuebung-mcpgw", out=lambda *_: None)
ok("Konnektor nicht mehr auf dem Knoten: abgelehnt", not good
   and "no longer on this node" in said, said)
m.save_idp_connectors(held)

print("B.9 Probelauf, Einrichten, Abschalten")
S["calls"].clear()
lines = []
good, said = m.mcp_idp_apply("vuebung-mcpgw", "auth", dry_run=True, out=lines.append)
ok("Probelauf: Plan gedruckt, nichts gerufen, nichts geschrieben",
   good and S["calls"] == [] and any("POST" in ln for ln in lines)
   and not m.load_env("vuebung-mcpgw") and RECREATED == [], (lines, S["calls"]))
good, said = m.mcp_idp_apply("vuebung-mcpgw", "auth", who="joerg", out=lambda *_: None)
env = m.load_env("vuebung-mcpgw", m.load_registry()["instances"]["vuebung-mcpgw"])
ok("Einrichten gelingt", good, said)
ok("die fuenf Variablen stehen in der Umgebung",
   env.get("OAAP_MCP_IDP_ISSUER") == BASE + "/realms/vuebung"
   and env.get("OAAP_MCP_IDP_AUDIENCE") == GW and env.get("OAAP_MCP_IDP_SIGNIN") == SI
   and env.get("OAAP_MCP_IDP_PLATFORM_AUDIENCE") == "oaap-node"
   and env.get("OAAP_MCP_IDP_SECRET", "").startswith("geheim-"), sorted(env))
r = m.load_registry()["instances"]["vuebung-mcpgw"]["mcp_idp"]
ok("das Register sagt wer, was, wo -- und KOEDER: nie das Geheimnis",
   r["connector"] == "auth" and r["space"] == "vuebung" and r["audience"] == GW
   and r["signin"] == SI and r["by"] == "joerg" and "secret" not in r
   and env["OAAP_MCP_IDP_SECRET"] not in json.dumps(m.load_registry()), r)
ok("Container einmal neu erzeugt", RECREATED == ["vuebung-mcpgw"], RECREATED)
log = [json.loads(ln) for ln in open(m.TENANT_LOG, encoding="utf-8")]
ok("eine Zeile im Protokoll des Mandanten, ohne Geheimnis",
   any(e["action"] == "mcp.idp" and e["tenant"] == TID and e["result"] == "ok"
       and e["who"] == "joerg" for e in log)
   and env["OAAP_MCP_IDP_SECRET"] not in open(m.TENANT_LOG, encoding="utf-8").read(), log[-1:])
good, said = m.mcp_idp_apply("vuebung-mcpgw", "auth", out=lambda *_: None)
ok("zweites Mal: kein neuer Container", good and RECREATED == ["vuebung-mcpgw"], RECREATED)
good, said = m.mcp_idp_apply("vuebung-mcpgw", "auth", off=True, out=lambda *_: None)
env = m.load_env("vuebung-mcpgw", m.load_registry()["instances"]["vuebung-mcpgw"])
r = m.load_registry()["instances"]["vuebung-mcpgw"]["mcp_idp"]
ok("Abschalten: Clients aus, Variablen weg, Register sagt 'off'",
   good and by_id(GW)["enabled"] is False and by_id(SI)["enabled"] is False
   and not any(k.startswith("OAAP_MCP_IDP_") for k in env) and r.get("off")
   and RECREATED[-1] == "vuebung-mcpgw", (said, env, r))

print("B.10 Reserviert, aufgehobene instance.env, Entfernen")
ok("die fuenf sind Plattform-Variablen", set(m.MCP_IDP_VARS.values()) <= m.RESERVED_ENV)
src = open(m.__file__, encoding="utf-8").read()
ok("eine aufgehobene instance.env nimmt sie nicht mit",
   "and k not in MCP_IDP_VARS.values()" in src)
ok("eine Neuinstallation ohne mcp_sources oder als Generalprobe laesst sie fallen",
   "not (mcp_sources_wanted(m) and not is_artefact and not rehearsal)" in src)
m.mcp_idp_apply("vuebung-mcpgw", "auth", out=lambda *_: None)
inst = m.load_registry()["instances"]["vuebung-mcpgw"]
S["writes"].clear()
buf = _io.StringIO()
with contextlib.redirect_stdout(buf):
    done = m.mcp_idp_retire("vuebung-mcpgw", inst)
ok("beim Entfernen: abgeschaltet, nicht geloescht", done
   and by_id(GW)["enabled"] is False and all(w.startswith("PUT") for w in S["writes"]),
   (buf.getvalue(), S["writes"]))
m.save_idp_connectors({})
buf = _io.StringIO()
with contextlib.redirect_stdout(buf):
    done = m.mcp_idp_retire("vuebung-mcpgw", inst)
ok("KOEDER: Konnektor weg -- kein stilles OK, beide Namen genannt",
   not done and GW in buf.getvalue() and SI in buf.getvalue(), buf.getvalue())
m.save_idp_connectors(held)

print("B.11 tenant check fragt den Anbieter (D13)")
reset()
make()
f, u = m.tenant_check_providers(m.load_tenants())
ok("alles da: kein Befund, nichts ungefragt", f == [] and u == [], (f, u))
del S["clients"][by_id("oaap-node")["id"]]
f, u = m.tenant_check_providers(m.load_tenants())
ok("KOEDER (Befund 1 von Stufe 2): fehlender Plattform-Client ist ein Befund, mit dem Weg",
   len(f) == 1 and "oaap-node" in f[0] and "absent" in f[0]
   and "oaap idp provision auth --tenant vuebung" in f[0], f)
reset()
make()
by_id(SI)["enabled"] = False
f, u = m.tenant_check_providers(m.load_tenants())
ok("ein abgeschalteter Anmelde-Client einer eingerichteten Instanz ist ein Befund",
   len(f) == 1 and SI in f[0] and "disabled" in f[0], f)
cs = m.load_idp_connectors()
cs["auth"]["base_url"] = "http://127.0.0.1:9"
m.save_idp_connectors(cs)
f, u = m.tenant_check_providers(m.load_tenants())
ok("nicht erreichbar: genannt, nicht als bestanden gezaehlt", f == [] and len(u) == 1
   and "vuebung" in u[0], (f, u))
m.save_idp_connectors(held)

print("B.12 Der Portal-Auftrag")
br = src[src.index('elif action == "mcp_idp":'):]
br = br[:br.index("elif action == ", 1)]
ok("server_admin wird geprueft, bevor die Funktion laeuft",
   br.index('act_role != "server_admin"') < br.index("mcp_idp_apply("), br[:400])
ok("eine Ablehnung steht im Protokoll", 'result="denied"' in br)

print("")
print("ALLE PRUEFUNGEN BESTANDEN" if not fails else f"{fails} FEHLER")
sys.exit(1 if fails else 0)
