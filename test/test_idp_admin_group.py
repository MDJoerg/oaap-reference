#!/usr/bin/env python3
"""Die Verwaltergruppe im Realm (RFC-0056, I-28).

Gemessen am 03.10.2026 an Keycloak 26.7.4: wer Benutzer verwalten darf,
darf eine VORHANDENE Realm-Rolle zuweisen, die ueber `realm-admin`
zusammengesetzt ist. Darum steht hier an erster Stelle nicht, dass die
Gruppe entsteht, sondern dass sie NICHT entsteht, wo diese Falle liegt --
und dass dabei nichts geschrieben wurde.

    Der Vertrag    'admin_group' ist ein deklariertes Verb; die Reihenfolge
                   im Plan liest die Rollen VOR dem ersten Schreiben;
                   `users` bleibt abgeschworen.
    Die Gruppe     entsteht mit genau vier Rollen, wird nachgelesen, ein
                   zweiter Lauf schreibt nichts, Fremdes bleibt stehen.
    Die Falle      eine zusammengesetzte Rolle ueber `realm-admin` -- auch
                   verschachtelt -- verweigert den Lauf, null Schreibzugriffe.
    Der Mensch     kein Benutzer wird angelegt, nichts wird geloescht.
    Der Befehl     `oaap idp admin-group` laeuft ueber denselben Kern und
                   schreibt einen Eintrag ins Mandantenprotokoll.

Braucht kein Docker und keinen Knoten.

Aufruf: python3 test/test_idp_admin_group.py
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
DATA = tempfile.mkdtemp(prefix="oaap-idpag-test-")
os.environ["OAAP_DATA_DIR"] = DATA
SERVICES = os.path.join(HERE, "..", "platform", "services")
PLATFORM = os.path.join(HERE, "..", "platform")
sys.path.insert(0, SERVICES)
sys.path.insert(0, PLATFORM)

import idp_admin as a                                          # noqa: E402

PIN = a.connector_of("keycloak")["pinned"]
fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:500]}")


MGMT = "mgmt-uuid"
ROLE_NAMES = ["manage-users", "view-users", "query-users", "query-groups",
              "manage-clients", "realm-admin"]
STATE = {}


def reset():
    STATE.clear()
    STATE.update({
        "realms": {"hbvp": {}},
        "clients": {MGMT: {"id": MGMT, "clientId": "realm-management",
                           "realm": "hbvp"}},
        "client_roles": [{"id": "r-" + n, "name": n, "clientRole": True,
                          "containerId": MGMT} for n in ROLE_NAMES],
        "realm_roles": [
            {"name": "offline_access", "composite": False},
            {"name": "default-roles-hbvp", "composite": True},
        ],
        "composites": {"default-roles-hbvp": [
            {"name": "offline_access", "clientRole": False},
            {"name": "view-profile", "clientRole": True,
             "containerId": "account-uuid"}]},
        "groups": {}, "group_roles": {},
        "calls": [], "writes": [], "forbidden_group_post": False,
    })


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

    def _auth(self):
        return self.headers.get("Authorization") == "Bearer fake-admin-token"

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except ValueError:
            return {}

    def do_GET(self):
        STATE["calls"].append("GET " + self.path)
        path = self.path.split("?")[0]
        if not self._auth():
            return self._json({"error": "x"}, 401)
        if path == "/admin/serverinfo":
            return self._json({"systemInfo": {"version": PIN}})
        if path == "/admin/realms/hbvp":
            return self._json(STATE["realms"]["hbvp"])
        if path == "/admin/realms/hbvp/clients":
            hit = re.search(r"clientId=([^&]+)", self.path)
            want = hit.group(1) if hit else ""
            return self._json([c for c in STATE["clients"].values()
                               if c["clientId"] == want])
        if path == f"/admin/realms/hbvp/clients/{MGMT}/roles":
            return self._json(STATE["client_roles"])
        if path == "/admin/realms/hbvp/roles":
            return self._json(STATE["realm_roles"])
        m = re.match(r"^/admin/realms/hbvp/roles/([^/]+)/composites$", path)
        if m:
            return self._json(STATE["composites"].get(m.group(1), []))
        if path == "/admin/realms/hbvp/groups":
            hit = re.search(r"search=([^&]+)", self.path)
            want = hit.group(1) if hit else ""
            return self._json([g for g in STATE["groups"].values()
                               if g["name"] == want])
        m = re.match(r"^/admin/realms/hbvp/groups/([^/]+)/role-mappings/"
                     r"clients/([^/]+)$", path)
        if m:
            return self._json(STATE["group_roles"].get(m.group(1), []))
        return self._json({"error": "nf"}, 404)

    def do_POST(self):
        STATE["calls"].append("POST " + self.path)
        body = self._body()
        if self.path.endswith("/protocol/openid-connect/token"):
            return self._json({"access_token": "fake-admin-token"})
        if not self._auth():
            return self._json({"error": "x"}, 401)
        STATE["writes"].append("POST " + self.path)
        if self.path == "/admin/realms/hbvp/groups":
            if STATE["forbidden_group_post"]:
                return self._json({"error": "forbidden"}, 403)
            gid = "g-%d" % (len(STATE["groups"]) + 1)
            STATE["groups"][gid] = {"id": gid, "name": body["name"],
                                    "path": "/" + body["name"]}
            return self._json({}, 201)
        m = re.match(r"^/admin/realms/hbvp/groups/([^/]+)/role-mappings/"
                     r"clients/([^/]+)$", self.path)
        if m:
            held = STATE["group_roles"].setdefault(m.group(1), [])
            for r in body:
                full = next(x for x in STATE["client_roles"]
                            if x["name"] == r["name"])
                held.append(full)
            return self._json({}, 204)
        return self._json({"error": "nf"}, 404)

    def do_DELETE(self):
        STATE["calls"].append("DELETE " + self.path)
        STATE["writes"].append("DELETE " + self.path)
        return self._json({}, 204)


srv = HTTPServer(("127.0.0.1", 0), Fake)
BASE = f"http://127.0.0.1:{srv.server_port}"
threading.Thread(target=srv.serve_forever, daemon=True).start()


def admin():
    return a.Admin("keycloak", BASE, "client", "oaap-admin",
                   "V0llmacht-lang-genug")


# ---------------------------------------------------------------------------
print("Der Vertrag")
ok("'admin_group' ist deklariert und bekannt",
   a.declares("keycloak", "admin_group") and "admin_group" in a.KNOWN_VERBS)
ok("der Vertrag ist weiter in sich stimmig",
   a.kind_refusal("keycloak") == "")
g = a.admin_group_of("keycloak")
ok("genau vier Rollen, alle aus dem Verwaltungs-Client, kein realm-admin",
   len(g["roles"]) == 4 and g["roles_client"] == "realm-management"
   and not {"realm-admin", "manage-clients", "manage-realm",
            "manage-identity-providers"} & set(g["roles"]), g)
ok("`users` bleibt abgeschworen", "users" in a.connector_of("keycloak")["never"]
   and "users" not in a.KNOWN_VERBS)
plan = a.admin_group_plan("keycloak", "hbvp")
ok("der Plan ist zulaessig (Fassung zuerst, keine verbotene Methode)",
   a.plan_refusal(plan) == "", a.plan_refusal(plan))
first_write = next(i for i, s in enumerate(plan) if s["writes"])
guard = next(i for i, s in enumerate(plan)
             if s["path"].endswith("/roles") and not s["writes"])
ok("die Rollen werden gelesen, BEVOR das erste Mal geschrieben wird",
   guard < first_write, (guard, first_write))
ok("der Plan enthaelt kein DELETE und keinen Benutzer-Pfad",
   all(s["method"] != "DELETE" for s in plan)
   and not any("/users" in s["path"] for s in plan))
ok("ein Verb, das ein anderer Anbieter nicht deklariert, wird verweigert",
   a.admin_group_refusal("gibtesnicht") != "")

# ---------------------------------------------------------------------------
print("Die Gruppe entsteht")
reset()
good, res, msg = admin().admin_group("hbvp", accept_version=PIN)
ok("Lauf auf frischem Realm gelingt", good, msg)
ok("die Gruppe heisst wie die Tabelle sagt", res.get("group") == g["name"], res)
ok("sie traegt genau die vier Rollen, nachgelesen",
   res.get("roles") == sorted(g["roles"]) and res.get("extra") == [], res)
ok("realm-admin und manage-clients sind NICHT bei der Gruppe",
   not {"realm-admin", "manage-clients"} & set(res.get("roles", [])))
ok("geschrieben wurde: eine Gruppe, ein Rollenpaket -- sonst nichts",
   len(STATE["writes"]) == 2
   and STATE["writes"][0].endswith("/groups")
   and "role-mappings" in STATE["writes"][1], STATE["writes"])
ok("kein Benutzer wurde angelegt, nichts geloescht",
   not any("/users" in c or c.startswith("DELETE") for c in STATE["calls"]),
   STATE["calls"])

print("Zweiter Lauf")
STATE["writes"].clear()
good, res, msg = admin().admin_group("hbvp", accept_version=PIN)
ok("derselbe Lauf noch einmal gelingt", good, msg)
ok("und schreibt nichts", STATE["writes"] == [], STATE["writes"])

print("Fremdes bleibt")
gid = next(iter(STATE["groups"]))
STATE["group_roles"][gid].append(next(
    r for r in STATE["client_roles"] if r["name"] == "manage-clients"))
STATE["writes"].clear()
good, res, msg = admin().admin_group("hbvp", accept_version=PIN)
ok("eine zusaetzliche Rolle der Gruppe wird gemeldet", good
   and res["extra"] == ["manage-clients"], res)
ok("und nicht entfernt, es wird nichts geschrieben",
   STATE["writes"] == [] and any(
       r["name"] == "manage-clients" for r in STATE["group_roles"][gid]))

print("Teilweise vorhandene Gruppe wird ergaenzt")
reset()
STATE["groups"]["g-9"] = {"id": "g-9", "name": g["name"],
                          "path": "/" + g["name"]}
STATE["group_roles"]["g-9"] = [next(
    r for r in STATE["client_roles"] if r["name"] == "view-users")]
good, res, msg = admin().admin_group("hbvp", accept_version=PIN)
ok("fehlende Rollen kommen dazu", good
   and res["roles"] == sorted(g["roles"]), res)
ok("die Gruppe wird nicht noch einmal angelegt",
   not any(w.endswith("/groups") for w in STATE["writes"]), STATE["writes"])

# ---------------------------------------------------------------------------
print("Die Falle (gemessen 03.10.2026)")
for label, roles, comps in (
    ("direkt ueber realm-admin",
     [{"name": "falle", "composite": True}],
     {"falle": [{"name": "realm-admin", "clientRole": True,
                 "containerId": MGMT}]}),
    ("verschachtelt (Realm-Rolle in Realm-Rolle)",
     [{"name": "aussen", "composite": True},
      {"name": "innen", "composite": True}],
     {"aussen": [{"name": "innen", "clientRole": False}],
      "innen": [{"name": "manage-clients", "clientRole": True,
                 "containerId": MGMT}]}),
):
    reset()
    STATE["realm_roles"] += roles
    STATE["composites"].update(comps)
    good, res, msg = admin().admin_group("hbvp", accept_version=PIN)
    ok(f"{label}: der Lauf wird verweigert", not good and "refused" in msg,
       msg)
    ok(f"{label}: NULL Schreibzugriffe", STATE["writes"] == [],
       STATE["writes"])
    ok(f"{label}: die Meldung nennt die Rolle",
       "'falle'" in msg or "'aussen'" in msg, msg)

reset()
STATE["realm_roles"].append({"name": "kreis", "composite": True})
STATE["composites"]["kreis"] = [{"name": "kreis", "clientRole": False}]
good, res, msg = admin().admin_group("hbvp", accept_version=PIN)
ok("eine Rolle, die sich selbst enthaelt, haengt den Lauf nicht auf",
   good, msg)

reset()
STATE["realm_roles"].append({"name": "harmlos", "composite": True})
STATE["composites"]["harmlos"] = [{"name": "view-profile",
                                   "clientRole": True,
                                   "containerId": "account-uuid"}]
good, res, msg = admin().admin_group("hbvp", accept_version=PIN)
ok("eine zusammengesetzte Rolle OHNE Verwaltungsrechte stoert nicht",
   good, msg)

# ---------------------------------------------------------------------------
print("Ehrliche Fehler")
reset()
STATE["forbidden_group_post"] = True
good, res, msg = admin().admin_group("hbvp", accept_version=PIN)
ok("403 beim Anlegen heisst 'nicht unser', nicht 'fehlt'",
   not good and "may not" in msg, msg)
reset()
STATE["realms"].clear()
STATE["realms"]["anderer"] = {}
good, res, msg = a.Admin("keycloak", BASE, "client", "x",
                         "V0llmacht-lang-genug").admin_group(
    "hbvp", accept_version=PIN)
ok("ein Realm, den es nicht gibt, wird nicht angelegt",
   STATE["writes"] == [], STATE["writes"])
reset()
good, res, msg = admin().admin_group("master", accept_version=PIN)
ok("der Server-Realm 'master' ist nie ein Mandanten-Realm",
   not good and STATE["writes"] == [], msg)

# ---------------------------------------------------------------------------
print("Der Befehl")
import appctl as m                                             # noqa: E402

m.reload_gateway = lambda: None
os.makedirs(m.APPS_DIR, exist_ok=True)
m.ensure_default_tenant()
tid, _t = m.tenant_create("hbvp", name="Handball Verein Probe")
good, msg = m.connector_add("kc", "keycloak", BASE, "client", "oaap-admin",
                            "V0llmacht-lang-genug")
ok("der Konnektor steht", good, msg)


def run(**kw):
    args = types.SimpleNamespace(
        action="admin-group", name="kc", tenant="hbvp", space=None,
        client_id=None, idp_label=None, accept_version=PIN, dry_run=False,
        self_registration=None, second_factor=None, idp_reason=None)
    for k, v in kw.items():
        setattr(args, k, v)
    try:
        m.cmd_idp(args)
        return True
    except SystemExit as e:
        return not e.code


reset()
ok("--dry-run ruft nichts auf und schreibt nichts",
   run(dry_run=True) and STATE["calls"] == [], STATE["calls"])
reset()
ok("der Befehl laeuft", run())
ok("und legt die Gruppe an", any(x["name"] == g["name"]
                                 for x in STATE["groups"].values()))
rows = [r for r in m.read_tenant_log(tid, limit=50)
        if r.get("action") == "tenant.idp-admin-group"]
ok("das steht im Mandantenprotokoll (Aktion tenant.idp-admin-group)",
   len(rows) == 1 and g["name"] in rows[0].get("detail", ""), rows)
reset()
STATE["realm_roles"].append({"name": "falle", "composite": True})
STATE["composites"]["falle"] = [{"name": "realm-admin", "clientRole": True,
                                 "containerId": MGMT}]
ok("an der Falle endet der Befehl mit Fehler und ohne Schreiben",
   (not run()) and STATE["writes"] == [], STATE["writes"])

print("")
print("ALLE PRUEFUNGEN BESTANDEN" if not fails else f"{fails} FEHLER")
sys.exit(1 if fails else 0)
