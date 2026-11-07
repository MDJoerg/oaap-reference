#!/usr/bin/env python3
"""RFC-0073 T6: Bindungen folgen einem Adresswechsel desselben Realms (0.1.241).

Gemessen am 07.11. auf oaap-test: Der Realm bekam eine https-Adresse,
der Aussteller (issuer) aenderte sich, und jede Bindung war ungueltig --
anna und bert waeren als Fremde ohne Rechte zurueckgekommen. Von Hand
umgehaengt; jetzt tut `oaap idp provision` das selbst, aber NUR wenn
OAAP weiss, dass es derselbe Realm ist:

1. derselbe Konnektor, derselbe Name, dieselbe Id beim Anbieter
   -> alle Bindungen des Mandanten unter dem alten Aussteller folgen,
2. KOEDER: ein Realm, gleich benannt, aber NEU angelegt (neue Id)
   -> nichts folgt, die Bindungen bleiben ungueltig,
3. ein anderer Konnektor -> nichts folgt,
4. ein Satz von vor 0.1.241 (keine Id): je Person beim Anbieter
   nachsehen; nur gefundene folgen; eine enge Vollmacht (403) haengt
   niemanden um,
5. Bindungen eines ANDEREN Mandanten werden nie angefasst.

Aufruf: python3 test/test_idp_rebind.py
"""
import json
import os
import re
import sys
import tempfile
import threading
import types
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-idp-rebind-test-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, os.path.join(HERE, "..", "platform"))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services"))

import appctl as m                                            # noqa: E402
import idp                                                    # noqa: E402

m.reload_gateway = lambda: None
m.IDENTITY_CONTAINER = "oaap-test-gibt-es-nicht-rebind"
fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:600]}")


STATE = {"realm_id": "rid-1", "users": {"sub-anna", "sub-bert"},
         "users_forbidden": False, "user_gets": 0}


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *_a):
        pass

    def _json(self, doc, status=200):
        raw = json.dumps(doc).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/admin/serverinfo":
            return self._json({"systemInfo": {"version": idp.KEYCLOAK_PINNED}})
        mm = re.match(r"^/admin/realms/([^/]+)$", path)
        if mm:
            return self._json({"realm": mm.group(1), "id": STATE["realm_id"]})
        if re.match(r"^/admin/realms/[^/]+/clients$", path):
            return self._json([{"id": "cid-1", "clientId": "oaap-knoten"}])
        if path.endswith("/client-secret"):
            return self._json({"type": "secret", "value": "vom-anbieter-geheim"})
        mm = re.match(r"^/admin/realms/[^/]+/users/([^/]+)$", path)
        if mm and mm.group(1) == "profile":          # provision liest das selbst
            return self._json({"attributes": []})
        if mm and mm.group(1) == "count":            # ... und zaehlt am Ende
            return self._json(len(STATE["users"]))
        if mm:
            STATE["user_gets"] += 1
            if STATE["users_forbidden"]:
                return self._json({"error": "forbidden"}, 403)
            if mm.group(1) in STATE["users"]:
                return self._json({"id": mm.group(1), "username": "x"})
            return self._json({"error": "not found"}, 404)
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        if self.path.endswith("/protocol/openid-connect/token"):
            return self._json({"access_token": "t"})
        return self._json({}, 201)

    def do_PUT(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        return self._json({}, 204)


def server():
    srv = HTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_port}"


OLD_BASE, NEW_BASE = server(), server()     # zwei Adressen, ein "Keycloak"

os.makedirs(m.APPS_DIR, exist_ok=True)
m.ensure_default_tenant()
tid, _t = m.tenant_create("verein", name="Verein")
other, _o = m.tenant_create("anderer", name="Anderer")
os.makedirs(os.path.dirname(m.EXTERNAL_FILE), exist_ok=True)
with open(m.EXTERNAL_FILE, "w", encoding="utf-8") as f:
    json.dump({"host": "knoten.example.org", "edge": ""}, f)


def connector(base, name="kc"):
    m.connector_remove(name)
    good, msg = m.connector_add(name, "keycloak", base, "client", "oaap-admin",
                                "V0llmacht-lang-genug")
    assert good, msg


def provision(name="kc", tenant="verein"):
    m.cmd_idp(types.SimpleNamespace(
        action="provision", name=name, tenant=tenant, space="verein",
        client_id="oaap-knoten", idp_label="", accept_version=None,
        dry_run=False, plain_callback=False))


def key_of(base):
    return f"oidc|{base}/realms/verein"


def bind_people(provider_key):
    users = [
        {"username": "anna", "tenant": tid,
         "idp": {"provider": provider_key, "subject": "sub-anna"}},
        {"username": "bert", "tenant": tid,
         "idp": {"provider": provider_key, "subject": "sub-bert"}},
        {"username": "geist", "tenant": tid,
         "idp": {"provider": provider_key, "subject": "sub-weg"}},
        {"username": "fremd", "tenant": other,
         "idp": {"provider": provider_key, "subject": "sub-anna"}},
    ]
    path = m._identity_users_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(users, f)


def providers():
    return {u["username"]: u["idp"]["provider"] for u in m._read_identity_users()}


def start(at_old_with_id=True):
    """Der Mandant meldet sich am alten Ort an; die Leute sind gebunden."""
    connector(OLD_BASE)
    provision()
    rec = m.load_tenants()[tid]["idp"]
    if not at_old_with_id:
        rec.pop("space_id", None)
        tenants = m.load_tenants()
        tenants[tid]["idp"] = rec
        m.save_tenants(tenants)
    bind_people(idp.provider_key(idp.provider_of(m.load_tenants()[tid])))


print("=== Voraussetzung ===")
connector(OLD_BASE)
provision()
ok("die Id des Realms steht im Mandantensatz",
   m.load_tenants()[tid]["idp"].get("space_id") == "rid-1",
   m.load_tenants()[tid]["idp"])
ok("der Schluessel einer Bindung ist der Aussteller (alter Ort)",
   idp.provider_key(idp.provider_of(m.load_tenants()[tid])) == key_of(OLD_BASE),
   idp.provider_key(idp.provider_of(m.load_tenants()[tid])))

print("\n=== 1. derselbe Realm, neue Adresse ===")
start()
connector(NEW_BASE)                  # Konnektor zeigt jetzt auf die neue Adresse
provision()
p = providers()
ok("anna, bert und geist folgen dem neuen Aussteller",
   all(p[n] == key_of(NEW_BASE) for n in ("anna", "bert", "geist")), p)
ok("KOEDER: der andere Mandant wird nicht angefasst", p["fremd"] == key_of(OLD_BASE), p)
ok("niemand wurde einzeln nachgesehen (die Id genuegt)", STATE["user_gets"] == 0,
   STATE["user_gets"])
audit = open(os.path.join(m.tenant_dir(tid), "audit.jsonl"), encoding="utf-8").read() \
    if os.path.exists(os.path.join(m.tenant_dir(tid), "audit.jsonl")) else ""

print("\n=== 2. KOEDER: gleicher Name, neu angelegter Realm ===")
STATE["realm_id"] = "rid-1"
start()
STATE["realm_id"] = "rid-NEU"
connector(NEW_BASE)
provision()
p = providers()
ok("niemand folgt -- es sind andere Leute",
   all(p[n] == key_of(OLD_BASE) for n in ("anna", "bert", "geist")), p)
STATE["realm_id"] = "rid-1"

print("\n=== 3. ein anderer Konnektor ===")
start()
connector(NEW_BASE, name="kc2")
provision(name="kc2")
p = providers()
ok("niemand folgt", all(p[n] == key_of(OLD_BASE) for n in ("anna", "bert")), p)
m.connector_remove("kc2")

print("\n=== 4. ein Satz von vor 0.1.241 (keine Id) ===")
start(at_old_with_id=False)
STATE["user_gets"] = 0
connector(NEW_BASE)
provision()
p = providers()
ok("gefundene Personen folgen (anna, bert)",
   p["anna"] == key_of(NEW_BASE) and p["bert"] == key_of(NEW_BASE), p)
ok("eine nicht gefundene bleibt ungueltig (geist)", p["geist"] == key_of(OLD_BASE), p)
ok("je Person genau ein Blick beim Anbieter", STATE["user_gets"] == 3, STATE["user_gets"])
ok("der andere Mandant bleibt", p["fremd"] == key_of(OLD_BASE), p)

start(at_old_with_id=False)
STATE["users_forbidden"] = True
connector(NEW_BASE)
provision()
p = providers()
ok("enge Vollmacht (403): niemand wird umgehaengt",
   all(p[n] == key_of(OLD_BASE) for n in ("anna", "bert", "geist")), p)
STATE["users_forbidden"] = False

print("\n=== die Regel selbst ===")
import move                                                   # noqa: E402
rec = {"connector": "kc", "space": "verein", "space_id": "rid-1"}
ok("gleicher Aussteller: nichts zu tun",
   move.same_space_rebind_refusal(rec, "kc", "verein", "rid-1", "a", "a") != "")
ok("fehlende Id beim Anbieter: nein",
   move.same_space_rebind_refusal(rec, "kc", "verein", "", "a", "b") != "")
ok("anderer Name: nein",
   move.same_space_rebind_refusal(rec, "kc", "andere", "rid-1", "a", "b") != "")
ok("alles gleich: ja", move.same_space_rebind_refusal(rec, "kc", "verein", "rid-1",
                                                      "a", "b") == "")

print("")
print("ALLE PRUEFUNGEN BESTANDEN" if not fails else f"{fails} FEHLER")
sys.exit(1 if fails else 0)
