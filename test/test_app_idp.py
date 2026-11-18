#!/usr/bin/env python3
"""Der Client einer App (RFC-0091 Stufe 3, oaap.core.tenant 2.9.2,
oaap.apps.runtime 2.35, Konformitaetstest 31).

    python3 test/test_app_idp.py

Gegen einen Papier-Keycloak, ohne Docker. Jede Zeile hat einen Koeder:

A. Das Manifest
   1. identity.oidc_client: Pfade lokal, nicht unter /auth/ oder /platform/,
      eins bis vier, groups_claim nur true/false, nichts Unbekanntes.
B. Der Konnektor (services/idp_admin.py)
   2. Name, Body, Plan: vertraulich, nur Code-Fluss, Version zuerst, kein
      DELETE; Gruppen-Mapper nur auf Wunsch.
   3. Anlegen, zweiter Lauf schreibt nichts.
   4. Oeffentlicher Client unter dem Namen: abgelehnt, unveraendert.
   5. Vorhandener vertraulicher Client: wieder an, unsere Adressen dazu,
      seine eigenen bleiben, Mapper nachgetragen, sonst nichts.
   6. Abschalten: PUT enabled false, nie DELETE; fehlend ist kein Fehler.
C. Die Plattform (appctl.py)
   7. Ablehnungen vor jedem Aufruf.
   8. Probelauf ruft nichts; Einrichten schreibt drei Variablen, Register
      ohne Geheimnis, Zeile app.idp; Adressen https unter allen Namen.
   9. Reserviert, aufgehobene instance.env, Entfernen schaltet ab.
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
os.environ["OAAP_DATA_DIR"] = tempfile.mkdtemp(prefix="oaap-app-idp-test-")
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
CID = a.app_client_for("vuebung-git")
URIS = ["https://git.vuebung.example/user/oauth2/oaap/callback"]


def admin():
    return a.Admin("keycloak", BASE, "client", "oaap-admin", "V0llmacht-lang-genug")


def make(groups=True, uris=URIS, space="vuebung"):
    return admin().app_client(space, CID, uris, groups, accept_version=PIN)


def has_groups(c):
    return a.has_group_mapper("keycloak", c)


def manifest(identity):
    return {"oaap_manifest": "0.16", "app": {"id": "forgejo", "name": "F",
            "version": "1.0.0", "type": "wrapped"},
            "services": {"web": {"image": "x", "port": 3000}},
            "routes": [{"path": "/", "roles": ["public"]}],
            "identity": identity}


# ------------------------------------------------------------------ A.1
print("A.1 Das Manifest")
GOOD = {"oidc_client": {"redirect_paths": ["/user/oauth2/oaap/callback"],
                        "groups_claim": True}}
ok("gute Erklaerung: kein Fehler, normalisiert",
   m.oidc_client_error(manifest(GOOD)) == ""
   and m.oidc_client_of(manifest(GOOD)) == {
       "redirect_paths": ["/user/oauth2/oaap/callback"], "groups_claim": True})
ok("groups_claim fehlt: false", m.oidc_client_of(manifest(
    {"oidc_client": {"redirect_paths": ["/cb"]}}))["groups_claim"] is False)
for bad, why in (
        ({"oidc_client": {"redirect_paths": []}}, "one to four"),
        ({"oidc_client": {"redirect_paths": ["/a", "/b", "/c", "/d", "/e"]}}, "one to four"),
        ({"oidc_client": {"redirect_paths": ["https://evil.example/cb"]}}, "not a local path"),
        ({"oidc_client": {"redirect_paths": ["//evil.example/cb"]}}, "not a local path"),
        ({"oidc_client": {"redirect_paths": ["/a/../b"]}}, "not a local path"),
        ({"oidc_client": {"redirect_paths": ["/cb?x=1"]}}, "not a local path"),
        ({"oidc_client": {"redirect_paths": ["/auth/callback"]}}, "belong to the platform"),
        ({"oidc_client": {"redirect_paths": ["/platform/x"]}}, "belong to the platform"),
        ({"oidc_client": {"redirect_paths": ["/cb"], "groups_claim": "yes"}}, "true or false"),
        ({"oidc_client": {"redirect_paths": ["/cb"], "admin": True}}, "only 'redirect_paths'"),
        ({"saml": {}}, "only 'oidc_client'"),
        ("ja", "object expected")):
    err = [m.oidc_client_error(manifest(bad))]
    ok(f"KOEDER {json.dumps(bad)[:60]}: abgelehnt ({why})",
       any(why in e for e in err) and m.oidc_client_of(manifest(bad)) == {}, err)
ok("die Plattform kennt Manifest 0.16", m.MANIFEST_MINOR >= 16)
VSRC = open(m.__file__, encoding="utf-8").read()
VSRC = VSRC[VSRC.index("def validate_manifest"):]
ok("validate_manifest fragt oidc_client_error",
   "err = oidc_client_error(m)" in VSRC[:VSRC.index(chr(10) + "def ")])
ok("ein Manifest ohne identity: kein Wunsch", m.oidc_client_of(
    {"oaap_manifest": "0.15"}) == {})

# ------------------------------------------------------------------ B.2
print("B.2 Name, Body, Plan")
ok("Name nach der Instanz", CID == "oaap-app-vuebung-git"
   and a.app_client_for("Git X/1") == "oaap-app-git-x-1" and a.app_client_for("") == "")
ok("der Konnektor erklaert app_clients, und es ist ein bekanntes Verb",
   a.declares("keycloak", "app_clients") and "app_clients" in a.KNOWN_VERBS
   and a.app_refusal("keycloak") == "" and a.incoherent_verbs("keycloak") == ())
plan = a.app_client_plan("keycloak", "vuebung", CID, True)
ok("der Plan ist zulaessig, Version zuerst, kein DELETE",
   a.plan_refusal(plan) == "" and plan[0]["verb"] == "version"
   and all(s["method"] in ("GET", "POST", "PUT") for s in plan), plan)
ok("ohne Gruppen-Wunsch kein Mapper-Schritt",
   not any("client_mappers" in s["path"] or "mappers" in s["path"]
           for s in a.app_client_plan("keycloak", "vuebung", CID, False)))
ok("Abschalt-Plan: Version zuerst, nur GET und PUT",
   a.plan_refusal(a.app_off_plan("keycloak", "vuebung", CID)) == ""
   and {s["method"] for s in a.app_off_plan("keycloak", "vuebung", CID)} == {"GET", "PUT"})
b = a._keycloak_app_client_body(CID, URIS, True)
ok("vertraulich, Code-Fluss, kein implicit, kein Passwort, kein Dienstkonto",
   b["publicClient"] is False and b["standardFlowEnabled"] is True
   and not b["implicitFlowEnabled"] and not b["directAccessGrantsEnabled"]
   and not b["serviceAccountsEnabled"] and b["webOrigins"] == []
   and b["redirectUris"] == URIS, b)
ok("Gruppen-Mapper wie beim Mandanten-Client, nur auf Wunsch",
   has_groups(b) and not has_groups(a._keycloak_app_client_body(CID, URIS, False)))

# ------------------------------------------------------------------ B.3
print("B.3 Anlegen")
reset()
good, res, msg = make()
c = by_id(CID)
ok("gelingt", good and c is not None, msg)
ok("in diesem Realm, vertraulich, mit Adressen und Gruppen",
   c["realm"] == "vuebung" and c["publicClient"] is False
   and c["redirectUris"] == URIS and has_groups(c), c)
ok("das Ergebnis traegt Issuer, Client und Geheimnis",
   res["issuer"] == BASE + "/realms/vuebung" and res["client_id"] == CID
   and res["secret"] == "geheim-" + c["id"], res)
ok("der Mandanten-Client ist nicht angefasst",
   not any(by_id("oaap-node")["id"] in w for w in S["writes"]), S["writes"])
S["writes"].clear()
good, _r, msg = make()
ok("zweiter Lauf gelingt und schreibt nichts", good and S["writes"] == [], S["writes"])
reset(realms=())
good, _r, msg = make()
ok("ohne Realm: abgelehnt, nichts geschrieben", not good and "makes no space" in msg
   and S["writes"] == [], msg)

# ------------------------------------------------------------------ B.4
print("B.4 Oeffentlicher Client unter dem Namen")
reset()
add_client(CID, publicClient=True, description="jemandes")
before = copy.deepcopy(by_id(CID))
good, _r, msg = make()
ok("abgelehnt, unveraendert, nichts geschrieben", not good and "PUBLIC client" in msg
   and by_id(CID) == before and S["writes"] == [], (msg, S["writes"]))

# ------------------------------------------------------------------ B.5
print("B.5 Vorhandener vertraulicher Client")
reset()
add_client(CID, publicClient=False, enabled=False, description="meins",
           redirectUris=["https://alt.example/cb"], attributes={"x": "y"})
good, _r, msg = make()
c = by_id(CID)
ok("gelingt", good, msg)
ok("wieder an, unsere Adresse dazu, KOEDER: seine eigene bleibt",
   c["enabled"] is True and c["redirectUris"] == ["https://alt.example/cb"] + URIS, c)
ok("Gruppen-Mapper nachgetragen, sonst nichts angefasst",
   has_groups(c) and c["description"] == "meins" and c["attributes"] == {"x": "y"}, c)
ok("kein DELETE", not any(w.startswith("DELETE") for w in S["writes"]), S["writes"])

# ------------------------------------------------------------------ B.6
print("B.6 Abschalten")
reset()
make()
S["writes"].clear()
good, msg = admin().app_client_off("vuebung", CID, PIN)
ok("abgeschaltet, nicht geloescht", good and by_id(CID)["enabled"] is False
   and all(w.startswith("PUT") for w in S["writes"]) and len(S["writes"]) == 1,
   (msg, S["writes"]))
S["writes"].clear()
good, _m = admin().app_client_off("vuebung", CID, PIN)
good2, _m2 = admin().app_client_off("vuebung", "gibt-es-nicht", PIN)
ok("zweites Mal und ein fehlender Client: gelingt, schreibt nichts",
   good and good2 and S["writes"] == [], S["writes"])
ok("KOEDER: nie ein DELETE gesendet", not any(c.startswith("DELETE") for c in S["calls"]))
reset(version="99.0.0")
good, _r, msg = admin().app_client("vuebung", CID, URIS)
ok("KOEDER: falsche Version bricht vor jedem Schreiben ab", not good
   and S["writes"] == [], msg)

# ------------------------------------------------------------------ C
print("C.7 Die Plattform lehnt vorher ab")
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
WANT = {"redirect_paths": ["/user/oauth2/oaap/callback"], "groups_claim": True}


def rec(name, tenant, want=WANT, **extra):
    c = f"oaap-app-{name}"
    r = {"app_id": "forgejo", "version": "15.0.0", "channel": "production",
         "port": 8130, "container": c, "svc_port": 3000, "tenant": tenant,
         "id": name.replace("-", "")[:12].ljust(12, "x"), "name": name.split("-", 1)[1],
         "services": [{"service": "", "container": c, "image": "", "build": "", "port": 3000}],
         "routes": [{"path": "/", "roles": ["public"]}], "visibility": {},
         "oidc_client": dict(want) if want else {}, "storage": [],
         "aliases": []}
    r.update(extra)
    return r


reg = m.load_registry()
reg.setdefault("instances", {})
reg["instances"]["vuebung-git"] = rec("vuebung-git", TID,
                                      address="git.vuebung.example")
reg["instances"]["vuebung-crm"] = rec("vuebung-crm", TID, want=None)
reg["instances"]["vuebung-git-probe"] = rec("vuebung-git-probe", TID,
                                            rehearsal={"of": "vuebung-git",
                                                       "expires": "2099-01-01T00:00:00Z"})
reg["instances"]["hand-git"] = rec("hand-git", "t-hand0000000")
reg["instances"]["leer-git"] = rec("leer-git", "t-leer0000000")
m.save_registry(reg)
RECREATED = []
m.recreate_instance_containers = lambda name, *a_, **k: RECREATED.append(name)
m.load_external_conf = lambda: ("oaap-test.example", "10.10.10.75")

for inst, conn, want in (("nix", "", "no instance"),
                         ("vuebung-crm", "", "does not sign persons in itself"),
                         ("vuebung-git-probe", "", "rehearsal"),
                         ("leer-git", "", "has no provider yet"),
                         ("hand-git", "", "named by hand"),
                         ("vuebung-git", "anderer", "not by 'anderer'")):
    S["calls"].clear()
    good, said = m.app_idp_apply(inst, connector_name=conn, out=lambda *_: None)
    ok(f"{inst}{' / ' + conn if conn else ''}: abgelehnt ({want}), nichts gerufen",
       not good and want in said and S["calls"] == [], (said, S["calls"]))

print("C.8 Probelauf, Einrichten, Abschalten")
CID = a.app_client_for("git")      # nach dem lokalen Namen, wie beim MCP-Gateway
uris = m.app_redirect_uris("vuebung-git", m.load_registry()["instances"]["vuebung-git"],
                           WANT["redirect_paths"])
ok("KOEDER (hinter einem Edge): https unter allen Namen, auch der automatischen",
   uris == ["https://git.vuebung.example/user/oauth2/oaap/callback",
            "https://git.vuebung.oaap-test.example/user/oauth2/oaap/callback"], uris)
S["calls"].clear()
lines = []
good, said = m.app_idp_apply("vuebung-git", "auth", dry_run=True, out=lines.append)
ok("Probelauf: Plan und Adressen gedruckt, nichts gerufen",
   good and S["calls"] == [] and any(uris[0] in ln for ln in lines)
   and not m.load_env("vuebung-git") and RECREATED == [], (lines, S["calls"]))
good, said = m.app_idp_apply("vuebung-git", "auth", who="joerg", out=lambda *_: None)
env = m.load_env("vuebung-git", m.load_registry()["instances"]["vuebung-git"])
ok("Einrichten gelingt", good, said)
ok("genau die drei Variablen", env.get("OAAP_OIDC_ISSUER") == BASE + "/realms/vuebung"
   and env.get("OAAP_OIDC_CLIENT_ID") == CID
   and env.get("OAAP_OIDC_CLIENT_SECRET", "").startswith("geheim-")
   and sorted(k for k in env if k.startswith("OAAP_")) == sorted(m.APP_OIDC_VARS.values()),
   sorted(env))
ok("im Realm: beide Adressen, Gruppen-Mapper", by_id(CID)["redirectUris"] == uris
   and has_groups(by_id(CID)), by_id(CID))
r = m.load_registry()["instances"]["vuebung-git"]["oidc_idp"]
ok("das Register sagt wer, was, wo -- und KOEDER: nie das Geheimnis",
   r["connector"] == "auth" and r["space"] == "vuebung" and r["client_id"] == CID
   and r["by"] == "joerg" and r["redirect_uris"] == sorted(uris) and "secret" not in r
   and env["OAAP_OIDC_CLIENT_SECRET"] not in json.dumps(m.load_registry()), r)
ok("Container einmal neu erzeugt", RECREATED == ["vuebung-git"], RECREATED)
log = [json.loads(ln) for ln in open(m.TENANT_LOG, encoding="utf-8")]
ok("eine Zeile app.idp im Protokoll des Mandanten, ohne Geheimnis",
   any(e["action"] == "app.idp" and e["tenant"] == TID and e["result"] == "ok"
       and e["who"] == "joerg" for e in log)
   and env["OAAP_OIDC_CLIENT_SECRET"] not in open(m.TENANT_LOG, encoding="utf-8").read(),
   log[-1:])
good, said = m.app_idp_apply("vuebung-git", "auth", out=lambda *_: None)
ok("zweites Mal: kein neuer Container", good and RECREATED == ["vuebung-git"], RECREATED)
f, u = m.tenant_check_providers(m.load_tenants())
ok("tenant check: alles da", f == [] and u == [], (f, u))
by_id(CID)["enabled"] = False
f, u = m.tenant_check_providers(m.load_tenants())
ok("tenant check: ein abgeschalteter App-Client ist ein Befund",
   len(f) == 1 and CID in f[0] and "disabled" in f[0], f)
by_id(CID)["enabled"] = True
good, said = m.app_idp_apply("vuebung-git", "auth", off=True, out=lambda *_: None)
env = m.load_env("vuebung-git", m.load_registry()["instances"]["vuebung-git"])
r = m.load_registry()["instances"]["vuebung-git"]["oidc_idp"]
ok("Abschalten: Client aus, Variablen weg, Register sagt 'off'",
   good and by_id(CID)["enabled"] is False
   and not any(k.startswith("OAAP_OIDC_") for k in env) and r.get("off"), (said, env, r))

print("C.9 Reserviert, aufgehobene instance.env, Entfernen")
ok("die drei sind Plattform-Variablen", set(m.APP_OIDC_VARS.values()) <= m.RESERVED_ENV)
src = open(m.__file__, encoding="utf-8").read()
ok("eine aufgehobene instance.env nimmt sie nicht mit",
   "and k not in APP_OIDC_VARS.values()" in src)
ok("eine Neuinstallation ohne Erklaerung oder als Generalprobe laesst sie fallen",
   "not (oidc_client_of(m) and not is_artefact and not rehearsal)" in src)
m.app_idp_apply("vuebung-git", "auth", out=lambda *_: None)
inst = m.load_registry()["instances"]["vuebung-git"]
ok("wieder einschalten setzt 'off' zurueck", not inst["oidc_idp"].get("off")
   and by_id(CID)["enabled"] is True, inst["oidc_idp"])
S["writes"].clear()
buf = _io.StringIO()
with contextlib.redirect_stdout(buf):
    done = m.app_idp_retire("vuebung-git", inst)
ok("beim Entfernen: abgeschaltet, nicht geloescht", done
   and by_id(CID)["enabled"] is False and all(w.startswith("PUT") for w in S["writes"]),
   (buf.getvalue(), S["writes"]))
held = m.load_idp_connectors()
m.save_idp_connectors({})
buf = _io.StringIO()
with contextlib.redirect_stdout(buf):
    done = m.app_idp_retire("vuebung-git", inst)
ok("KOEDER: Konnektor weg -- kein stilles OK, der Name genannt",
   not done and CID in buf.getvalue(), buf.getvalue())
m.save_idp_connectors(held)
ok("das Entfernen ruft app_idp_retire", "app_idp_retire(name, inst)" in src)
hint = m.app_idp_hint("vuebung-git", TID)
ok("die Installation nennt das Kommando", "oaap idp app auth --instance vuebung-git"
   in hint, hint)
ok("... und ohne Konnektor-Anbieter: eigene Konten",
   "own accounts" in m.app_idp_hint("hand-git", "t-hand0000000"))

print("")
print("ALLE PRUEFUNGEN BESTANDEN" if not fails else f"{fails} FEHLER")
sys.exit(1 if fails else 0)
