#!/usr/bin/env python3
"""Der Gruppen-Anspruch am Client (RFC-0045 Stufe 3, Konnektor).

Gemessen am 03.10.2026: Der Client, den OAAP im Realm anlegt, hatte keinen
Mapper -- das Token trug keine `groups`, und eine Abbildung "Gruppe -> Rolle"
haette nie etwas zu lesen bekommen. Ohne diesen Schritt ist Stufe 3 ein
Schalter, der an nichts angeschlossen ist.

Geprueft wird (gegen einen Fake-Keycloak, ohne Docker):
    - ein neu angelegter Client traegt den Mapper (voller Pfad, ID-Token);
    - ein VORHANDENER Client bekommt ihn nachgetragen, und sonst aendert
      sich an ihm nichts;
    - hat er schon einen gleichwertigen (auch unter anderem Namen), wird
      nichts geschrieben;
    - hat er einen Mapper gleichen Namens, der etwas anderes tut: Fehler
      mit Satz, kein stilles "ok", und der fremde Mapper bleibt wie er ist;
    - der Plan nennt den Schritt, nach der Versionspruefung, ohne DELETE;
    - ein zweiter Lauf schreibt nichts mehr.

Aufruf: python3 test/test_idp_group_mapper.py
"""
import copy
import json
import os
import re
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["OAAP_DATA_DIR"] = tempfile.mkdtemp(prefix="oaap-mapper-test-")
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services"))

import idp_admin as a                                          # noqa: E402

PIN = a.connector_of("keycloak")["pinned"]
fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:500]}")


S = {}


def reset():
    S.clear()
    S.update({"realms": {}, "clients": {}, "calls": [], "writes": []})


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

    def _ok(self):
        return self.headers.get("Authorization") == "Bearer fake-admin-token"

    def do_GET(self):
        S["calls"].append("GET " + self.path)
        path = self.path.split("?")[0]
        if not self._ok():
            return self._json({}, 401)
        if path == "/admin/serverinfo":
            return self._json({"systemInfo": {"version": PIN}})
        m = re.match(r"^/admin/realms/([^/]+)$", path)
        if m:
            return self._json({}, 200) if m.group(1) in S["realms"] \
                else self._json({}, 404)
        m = re.match(r"^/admin/realms/([^/]+)/clients$", path)
        if m:
            want = re.search(r"clientId=([^&]+)", self.path)
            want = want.group(1) if want else ""
            return self._json([c for c in S["clients"].values()
                               if c["clientId"] == want])
        m = re.match(r"^/admin/realms/[^/]+/clients/([^/]+)/client-secret$",
                     path)
        if m:
            return self._json({"type": "secret", "value": "geheim-" + m.group(1)})
        return self._json({}, 404)

    def do_POST(self):
        S["calls"].append("POST " + self.path)
        body = self._body()
        if self.path.endswith("/protocol/openid-connect/token"):
            return self._json({"access_token": "fake-admin-token"})
        if not self._ok():
            return self._json({}, 401)
        S["writes"].append("POST " + self.path)
        if self.path == "/admin/realms":
            S["realms"][body["realm"]] = body
            return self._json({}, 201)
        m = re.match(r"^/admin/realms/[^/]+/clients$", self.path)
        if m:
            uid = "cid-%d" % (len(S["clients"]) + 1)
            S["clients"][uid] = dict(body, id=uid)
            return self._json({}, 201)
        m = re.match(r"^/admin/realms/[^/]+/clients/([^/]+)/protocol-mappers/"
                     r"models$", self.path)
        if m and m.group(1) in S["clients"]:
            cl = S["clients"][m.group(1)]
            mappers = cl.setdefault("protocolMappers", [])
            if any(x["name"] == body["name"] for x in mappers):
                return self._json({"errorMessage": "exists"}, 409)
            mappers.append(body)
            return self._json({}, 201)
        return self._json({}, 404)

    def do_PUT(self):
        S["calls"].append("PUT " + self.path)
        body = self._body()
        S["writes"].append("PUT " + self.path)
        m = re.match(r"^/admin/realms/[^/]+/clients/([^/]+)$", self.path)
        if m and m.group(1) in S["clients"]:
            keep = S["clients"][m.group(1)].get("protocolMappers")
            S["clients"][m.group(1)].update(body)
            if keep is not None:
                S["clients"][m.group(1)]["protocolMappers"] = keep
            return self._json({}, 204)
        return self._json({}, 404)

    def do_DELETE(self):
        S["calls"].append("DELETE " + self.path)
        S["writes"].append("DELETE " + self.path)
        return self._json({}, 204)


srv = HTTPServer(("127.0.0.1", 0), Fake)
BASE = f"http://127.0.0.1:{srv.server_port}"
threading.Thread(target=srv.serve_forever, daemon=True).start()
URI = ["https://hbvp.node.example/auth/oidc/callback"]


def provision():
    adm = a.Admin("keycloak", BASE, "client", "oaap-admin",
                  "V0llmacht-lang-genug")
    return adm.provision("hbvp", "oaap-node", URI, accept_version=PIN)


def mappers():
    cl = next(iter(S["clients"].values()), {})
    return cl.get("protocolMappers", [])


print("Der Mapper selbst")
mp = a._keycloak_group_mapper()
ok("er heisst oaap-groups und liest die Gruppen als vollen Pfad",
   mp["name"] == "oaap-groups" and mp["config"]["full.path"] == "true"
   and mp["config"]["claim.name"] == "groups", mp)
ok("er wirkt im ID-Token (das liest OAAP) und im Access-Token",
   mp["config"]["id.token.claim"] == "true"
   and mp["config"]["access.token.claim"] == "true")
ok("has_group_mapper erkennt ihn", a.has_group_mapper(
    "keycloak", {"protocolMappers": [mp]}))
ok("... auch einen von Hand gemachten unter anderem Namen",
   a.has_group_mapper("keycloak", {"protocolMappers": [dict(mp, name="egal")]}))
off = copy.deepcopy(mp)
off["config"]["id.token.claim"] = "false"
ok("KOEDER: einer, der nicht ins ID-Token schreibt, zaehlt nicht",
   not a.has_group_mapper("keycloak", {"protocolMappers": [off]}))
other = copy.deepcopy(mp)
other["config"]["claim.name"] = "grp"
ok("KOEDER: einer mit anderem Anspruchsnamen zaehlt nicht",
   not a.has_group_mapper("keycloak", {"protocolMappers": [other]}))
ok("kein Mapper: nein", not a.has_group_mapper("keycloak", {})
   and not a.has_group_mapper("keycloak", None))

print("Der Plan")
plan = a.provision_plan("keycloak", "hbvp", "oaap-node")
ok("der Plan ist weiter zulaessig", a.plan_refusal(plan) == "")
step = [s for s in plan if "client_mappers" in s["path"]
        or "protocol-mappers" in s["path"]]
ok("er nennt den Schritt, nur wenn er fehlt, mit Schreiben",
   len(step) == 1 and step[0]["when"] == "different" and step[0]["writes"]
   and step[0]["method"] == "POST", step)
ok("nach der Versionspruefung, ohne DELETE",
   plan.index(step[0]) > 0 and all(s["method"] != "DELETE" for s in plan))

print("Neuer Realm, neuer Client")
reset()
good, res, msg = provision()
ok("Provisionieren gelingt", good, msg)
ok("der neue Client traegt den Mapper schon beim Anlegen",
   a.has_group_mapper("keycloak", next(iter(S["clients"].values()))),
   S["clients"])
ok("und es gab keinen eigenen Mapper-Aufruf dafuer",
   not any("protocol-mappers" in w for w in S["writes"]), S["writes"])
S["writes"].clear()
good, res, msg = provision()
ok("ein zweiter Lauf gelingt", good, msg)
ok("... und schreibt nichts am Client (ausser dem, was schon stand)",
   not any("protocol-mappers" in w for w in S["writes"]), S["writes"])

print("Vorhandener Client ohne Mapper")
reset()
S["realms"]["hbvp"] = {}
S["clients"]["cid-1"] = {"id": "cid-1", "clientId": "oaap-node",
                         "publicClient": False, "redirectUris": ["https://alt/x"],
                         "webOrigins": ["https://alt"], "description": "meins"}
before = copy.deepcopy(S["clients"]["cid-1"])
good, res, msg = provision()
ok("Provisionieren gelingt", good, msg)
ok("der Mapper wurde nachgetragen", a.has_group_mapper(
    "keycloak", S["clients"]["cid-1"]), S["clients"]["cid-1"])
cl = S["clients"]["cid-1"]
ok("sonst ist am Client nichts angefasst (Beschreibung, Origins, alte Adresse)",
   cl["description"] == "meins" and cl["webOrigins"] == ["https://alt"]
   and "https://alt/x" in cl["redirectUris"]
   and URI[0] in cl["redirectUris"], cl)
ok("genau ein Mapper-Aufruf, kein DELETE",
   sum("protocol-mappers" in w for w in S["writes"]) == 1
   and not any(w.startswith("DELETE") for w in S["writes"]), S["writes"])
S["writes"].clear()
good, res, msg = provision()
ok("zweiter Lauf: kein weiterer Mapper", good and not any(
    "protocol-mappers" in w for w in S["writes"]) and len(mappers()) == 1,
   S["writes"])

print("Vorhandener Client mit gleichwertigem Mapper unter anderem Namen")
reset()
S["realms"]["hbvp"] = {}
S["clients"]["cid-1"] = {"id": "cid-1", "clientId": "oaap-node",
                         "redirectUris": URI,
                         "protocolMappers": [dict(mp, name="meine-gruppen")]}
good, res, msg = provision()
ok("nichts wird geschrieben, es gibt schon einen",
   good and not any("protocol-mappers" in w for w in S["writes"]), S["writes"])

print("Vorhandener Client mit einem Mapper gleichen Namens, der etwas anderes tut")
reset()
S["realms"]["hbvp"] = {}
S["clients"]["cid-1"] = {"id": "cid-1", "clientId": "oaap-node",
                         "redirectUris": URI,
                         "protocolMappers": [dict(other, name="oaap-groups")]}
snapshot = copy.deepcopy(S["clients"]["cid-1"]["protocolMappers"])
good, res, msg = provision()
ok("KOEDER: kein stilles 'ok' -- Fehler mit Satz", not good
   and "does not put the groups" in msg, msg)
ok("der fremde Mapper ist unveraendert", mappers() == snapshot, mappers())

print("")
print("ALLE PRUEFUNGEN BESTANDEN" if not fails else f"{fails} FEHLER")
sys.exit(1 if fails else 0)
