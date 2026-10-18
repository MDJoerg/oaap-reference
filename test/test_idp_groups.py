#!/usr/bin/env python3
"""Gruppenpfade im Realm anlegen (RFC-0056 §6, oaap.core.authorization 0.4 §2.11).

Das Verb `groups` legt Gruppen an, sonst nichts. Darum stehen hier die Koeder
vorn, nicht die Freude, dass etwas entsteht:

    - eine Gruppe gleichen Namens an ANDERER Stelle ist nicht die gesuchte;
    - eine vorhandene Gruppe wird nicht angefasst (Mitglieder, Rollen,
      Attribute), und ein zweiter Lauf schreibt nichts;
    - ein Pfad mit gemeinsamem Elternteil legt den Elternteil nur einmal an;
    - Fassung und Realm werden VOR dem ersten Schreiben geprueft;
    - kein Aufruf beruehrt Mitglieder, Benutzer oder Rollen, nie ein DELETE;
    - ein verbotenes Anlegen (403) sagt es, statt weiterzumachen;
    - was berichtet wird, ist das, was der Realm danach antwortet.

Braucht kein Docker und keinen Knoten.

Aufruf: python3 test/test_idp_groups.py
"""
import json
import os
import re
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-idpgroups-test-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services"))
sys.path.insert(0, os.path.join(HERE, "..", "platform"))

import idp_admin as a                                          # noqa: E402

PIN = a.connector_of("keycloak")["pinned"]
fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:500]}")


STATE = {}


def reset(**over):
    STATE.clear()
    STATE.update({
        "realms": {"hbvp"}, "groups": {}, "calls": [], "writes": [],
        "version": PIN, "forbid_post": False, "n": 0,
    })
    STATE.update(over)


def add_group(name, parent="", **extra):
    STATE["n"] += 1
    gid = f"g-{STATE['n']}"
    path = (STATE["groups"][parent]["path"] if parent else "") + "/" + name
    STATE["groups"][gid] = dict(id=gid, name=name, path=path, parent=parent,
                                members=["person-1"], roles=["r1"],
                                attrs={"x": "y"}, **extra)
    return gid


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

    def _public(self, g):
        return {"id": g["id"], "name": g["name"], "path": g["path"]}

    def do_GET(self):
        STATE["calls"].append("GET " + self.path)
        path = self.path.split("?")[0]
        if not self._auth():
            return self._json({"error": "x"}, 401)
        if path == "/admin/serverinfo":
            return self._json({"systemInfo": {"version": STATE["version"]}})
        m = re.match(r"^/admin/realms/([^/]+)$", path)
        if m:
            return (self._json({}) if m.group(1) in STATE["realms"]
                    else self._json({"error": "nf"}, 404))
        if path == "/admin/realms/hbvp/groups":
            hit = re.search(r"search=([^&]+)", self.path)
            want = hit.group(1) if hit else ""
            # wie das Produkt: die Suche liefert auch den OBEREN Eintrag, unter
            # dem ein Treffer liegt (Keycloak antwortet mit der Hierarchie)
            def top_of(g):
                while g["parent"]:
                    g = STATE["groups"][g["parent"]]
                return g
            hits = {top_of(g)["id"]: top_of(g) for g in STATE["groups"].values()
                    if g["name"] == want}
            return self._json([self._public(g) for g in hits.values()])
        m = re.match(r"^/admin/realms/hbvp/groups/([^/]+)/children$", path)
        if m:
            return self._json([self._public(g) for g in STATE["groups"].values()
                               if g["parent"] == m.group(1)])
        return self._json({"error": "nf"}, 404)

    def do_POST(self):
        STATE["calls"].append("POST " + self.path)
        body = self._body()
        if self.path.endswith("/protocol/openid-connect/token"):
            return self._json({"access_token": "fake-admin-token"})
        if not self._auth():
            return self._json({"error": "x"}, 401)
        STATE["writes"].append("POST " + self.path)
        if STATE["forbid_post"]:
            return self._json({"error": "forbidden"}, 403)
        if self.path == "/admin/realms/hbvp/groups":
            add_group(body["name"])
            return self._json({}, 201)
        m = re.match(r"^/admin/realms/hbvp/groups/([^/]+)/children$", self.path)
        if m and m.group(1) in STATE["groups"]:
            add_group(body["name"], m.group(1))
            return self._json({}, 201)
        return self._json({"error": "nf"}, 404)

    def do_PUT(self):
        STATE["calls"].append("PUT " + self.path)
        STATE["writes"].append("PUT " + self.path)
        return self._json({}, 204)

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


def paths_now():
    return sorted(g["path"] for g in STATE["groups"].values())


# ---------------------------------------------------------------------------
print("Der Vertrag")
ok("'groups' ist ein deklariertes, bekanntes Verb",
   a.declares("keycloak", "groups") and "groups" in a.KNOWN_VERBS)
ok("der Vertrag ist weiter in sich stimmig", a.kind_refusal("keycloak") == ""
   and a.incoherent_verbs("keycloak") == ())
ok("`users` bleibt abgeschworen",
   "users" in a.connector_of("keycloak")["never"]
   and "users" not in a.KNOWN_VERBS)
plan = a.groups_plan("keycloak", "hbvp", ["Verein/Trainer", "Verein/Betreuer"])
ok("der Plan ist zulaessig (Fassung zuerst)", a.plan_refusal(plan) == "",
   a.plan_refusal(plan))
first_write = next(i for i, s in enumerate(plan) if s["writes"])
ok("der Plan liest, bevor er schreibt, und schreibt nur per POST",
   first_write > 2 and all(s["method"] in ("GET", "POST") for s in plan))
ok("kein Pfad des Plans fasst Mitglieder, Benutzer oder Rollen an",
   not any(re.search(r"/(users|members|role-mappings|roles)", s["path"])
           for s in plan), [s["path"] for s in plan])
ok("Verein steht im Plan nur EINMAL, obwohl zwei Pfade darunter liegen",
   sum(1 for s in plan if s["method"] == "POST"
       and s["path"].endswith("/groups")) == 1, [s["why"] for s in plan])

print("Segmente")
for good, want in (("Verein/Trainer", ["Verein", "Trainer"]),
                   ("/Verein/Trainer", ["Verein", "Trainer"]),
                   ("a", ["a"]), ("a/b/c", ["a", "b", "c"]),
                   ("Verein/Hallen sprecher", ["Verein", "Hallen sprecher"])):
    ok(f"'{good}' ist ein Pfad", a.group_segments(good) == want,
       a.group_segments(good))
for bad in ("", "/", "a//b", "a/b/c/d", "a/ b", "a /b", "a/" + "x" * 65,
            "a/\x01b"):
    ok(f"KOEDER: {bad[:12]!r} ist keiner", a.group_segments(bad) is None)
ok("die Ablehnung kommt VOR dem ersten Aufruf",
   a.groups_refusal("keycloak", ["a/b/c/d"]) != ""
   and a.groups_plan("keycloak", "hbvp", ["a/b/c/d"]) == [])

print("Anlegen")
reset()
adm = admin()
ok_, res, msg = adm.ensure_groups("hbvp", ["Verein/Trainer", "Verein/Betreuer"],
                                  accept_version=PIN)
ok("ein Lauf gelingt", ok_, msg)
ok("drei Gruppen: der Elternteil EINMAL, zwei Kinder",
   paths_now() == ["/Verein", "/Verein/Betreuer", "/Verein/Trainer"],
   paths_now())
ok("berichtet wird, was der Realm antwortet (Kennungen vorhanden)",
   set(res.get("ids", {})) == {"Verein/Trainer", "Verein/Betreuer"}
   and all(res["ids"].values()), res)
ok("'created' nennt alle drei", sorted(res["created"]) ==
   ["Verein", "Verein/Betreuer", "Verein/Trainer"], res)
ok("kein Aufruf beruehrte Mitglieder, Benutzer oder Rollen, nie ein DELETE/PUT",
   not any(re.search(r"/(users|members|role-mappings|roles)", c)
           or c.startswith(("DELETE", "PUT")) for c in STATE["calls"]),
   STATE["calls"])
n_writes = len(STATE["writes"])
ok_, res, msg = admin().ensure_groups(
    "hbvp", ["Verein/Trainer", "Verein/Betreuer"], accept_version=PIN)
ok("ein zweiter Lauf schreibt NICHTS", ok_ and len(STATE["writes"]) == n_writes
   and res["created"] == [], (res, STATE["writes"][n_writes:]))
ok("und sagt 'already there'", len(res["present"]) == 3, res)

print("Vorhandenes bleibt")
reset()
gv = add_group("Verein")
gt = add_group("Trainer", gv)
before = json.dumps(STATE["groups"][gt], sort_keys=True)
ok_, res, msg = admin().ensure_groups("hbvp", ["Verein/Trainer"],
                                      accept_version=PIN)
ok("eine vorhandene Gruppe: nichts geschrieben", ok_ and STATE["writes"] == [],
   (msg, STATE["writes"]))
ok("ihre Mitglieder, Rollen und Attribute sind unveraendert",
   json.dumps(STATE["groups"][gt], sort_keys=True) == before)

print("Koeder: derselbe Name an anderer Stelle")
reset()
other = add_group("Anders")
add_group("Trainer", other)           # /Anders/Trainer
ok_, res, msg = admin().ensure_groups("hbvp", ["Verein/Trainer"],
                                      accept_version=PIN)
ok("/Anders/Trainer ist nicht /Verein/Trainer: sie wird angelegt",
   ok_ and "/Verein/Trainer" in paths_now(), paths_now())
reset()
add_group("Trainer")                  # /Trainer (oberste Ebene)
ok_, res, msg = admin().ensure_groups("hbvp", ["Verein/Trainer"],
                                      accept_version=PIN)
ok("auch /Trainer (oberste Ebene) ist nicht /Verein/Trainer",
   ok_ and "/Verein/Trainer" in paths_now(), paths_now())
reset()
add_group("Verein")
ok_, res, msg = admin().ensure_groups("hbvp", ["Trainer"], accept_version=PIN)
ok("und /Verein ist nicht /Trainer: oberste Ebene wird angelegt",
   ok_ and "/Trainer" in paths_now(), paths_now())

reset()
other = add_group("Anders")
add_group("Trainer", other)           # /Anders/Trainer, die Suche nach "Trainer" liefert /Anders
ok_, res, msg = admin().ensure_groups("hbvp", ["Trainer"], accept_version=PIN)
ok("KOEDER: die Suche liefert den OBEREN Eintrag /Anders; /Trainer wird trotzdem angelegt",
   ok_ and "/Trainer" in paths_now() and res["ids"]["Trainer"] !=
   next(g["id"] for g in STATE["groups"].values() if g["path"] == "/Anders"),
   (msg, paths_now(), res))

print("Vor dem ersten Schreiben")
reset(version="25.0.0")
ok_, res, msg = admin().ensure_groups("hbvp", ["Verein/Trainer"])
ok("eine andere Fassung: abgelehnt, null Schreibzugriffe",
   not ok_ and STATE["writes"] == [] and "version" in msg.lower(),
   (msg, STATE["writes"]))
reset()
ok_, res, msg = admin().ensure_groups("keinrealm", ["Verein/Trainer"],
                                      accept_version=PIN)
ok("kein solcher Realm: abgelehnt, null Schreibzugriffe, sagt dass `provision` ihn macht",
   not ok_ and STATE["writes"] == [] and "provision" in msg, (msg, STATE["writes"]))
reset()
ok_, res, msg = admin().ensure_groups("hbvp", ["a/b/c/d"], accept_version=PIN)
ok("ein kaputter Pfad: abgelehnt, KEIN Aufruf",
   not ok_ and STATE["calls"] == [], (msg, STATE["calls"]))
reset()
ok_, res, msg = admin().ensure_groups("hbvp", [], accept_version=PIN)
ok("kein Pfad: abgelehnt", not ok_ and STATE["calls"] == [], msg)

print("Verbotenes Anlegen")
reset(forbid_post=True)
ok_, res, msg = admin().ensure_groups("hbvp", ["Verein/Trainer"],
                                      accept_version=PIN)
ok("403: gelingt nicht, sagt es, legt nichts an",
   not ok_ and paths_now() == [] and "403" not in msg
   and ("not" in msg or "may" in msg), (msg, paths_now()))

print("Ein Elternteil fehlt, das Kind soll folgen")
reset()
ok_, res, msg = admin().ensure_groups("hbvp", ["A/B/C"], accept_version=PIN)
ok("drei Ebenen entstehen in der richtigen Reihenfolge",
   ok_ and paths_now() == ["/A", "/A/B", "/A/B/C"], (msg, paths_now()))

print("\nALLE PRUEFUNGEN BESTANDEN" if not fails
      else f"\n{fails} PRUEFUNG(EN) FEHLGESCHLAGEN")
sys.exit(1 if fails else 0)
