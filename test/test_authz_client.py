#!/usr/bin/env python3
"""Der Referenz-Client fuer Fachrechte (oaap.core.authorization 0.1, 2.4).

Ein Client, der "ja" sagt, wenn er die Antwort nicht bekommt, ist eine Tuer,
die aufgeht, wenn das Schloss kaputt ist. Hier wird jede Art von Ausfall
durchgespielt, und jede muss "nein" heissen.

Geprueft wird:
    - Fail closed: nicht erreichbar, HTTP-Fehler, Muell, falscher Benutzer in
      der Antwort, Antwort einer anderen App, kein Schluessel, keine URL;
    - die Frage selbst: ohne Kontext gefragt heisst nein bei einem Recht,
      das auf einen Kontext beschraenkt ist; fremder Kontext heisst nein;
    - der Zwischenspeicher: haelt hoechstens so lange wie `fresh_for`, nie
      laenger als 30 Sekunden, und gar nicht bei fresh_for 0;
    - der Client und der Kern (authorization.may) antworten gleich auf
      dieselben Faelle -- zwei Fassungen derselben Regel sind eine Gelegenheit,
      dass eine abweicht.

Braucht weder Docker noch Flask.

Aufruf: python3 test/test_authz_client.py
"""
import itertools
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "platform"))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services"))

import authz_client as ac                                       # noqa: E402
import authorization as az                                      # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:400]}")


GRANTS = [
    {"object": "team", "activities": ["read", "edit_lineup"],
     "fields": {"team": ["mB"]}},
    {"object": "news", "activities": ["read", "publish"],
     "fields": {"area": ["news"]}},
    {"object": "kasse", "activities": ["read"], "fields": {}},
]
STATE = {"mode": "ok", "calls": 0, "fresh": 30, "app": "vereinsportal",
         "user": None}


class H(BaseHTTPRequestHandler):
    def log_message(self, *_a):
        pass

    def do_GET(self):
        STATE["calls"] += 1
        mode = STATE["mode"]
        if mode == "500":
            self.send_response(500)
            self.end_headers()
            return
        if mode == "garbage":
            raw = b"<html>nope</html>"
        else:
            uid = self.path.split("user=")[1].split("&")[0]
            doc = {"user": STATE["user"] or uid, "app": STATE["app"],
                   "tenant": "t", "grants": GRANTS,
                   "fresh_for": STATE["fresh"]}
            if mode == "no-grants-key":
                doc.pop("grants")
            raw = json.dumps(doc).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


srv = HTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
URL = f"http://127.0.0.1:{srv.server_port}/authz"
T = [1000.0]


def client(**kw):
    kw.setdefault("url", URL)
    kw.setdefault("key", "oaapk_x_y")
    kw.setdefault("app", "vereinsportal")
    return ac.Authz(clock=lambda: T[0], **kw)


print("Die Antwort kommt an")
c = client()
ok("Ben darf in mB aufstellen", c.may("u-ben", "team.edit_lineup", team="mB"))
ok("... nicht in mC", not c.may("u-ben", "team.edit_lineup", team="mC"))
ok("KOEDER: ohne Mannschaft gefragt heisst nein",
   not c.may("u-ben", "team.edit_lineup"))
ok("... und 'irgendwo' sagt man ausdruecklich",
   c.may_any("u-ben", "team.edit_lineup"))
ok("ein Recht ohne Feldbeschraenkung gilt ohne Frage nach dem Feld",
   c.may("u-ben", "kasse.read"))
ok("unbekanntes Objekt: nein", not c.may("u-ben", "lager.read"))
ok("unbekannte Aktivitaet: nein", not c.may("u-ben", "team.fly", team="mB"))

print("Fail closed")
STATE["mode"] = "ok"
ok("keine URL: nein", not client(url="").may("u", "kasse.read"))
ok("kein Schluessel: nein", not client(key="", url=URL).may("u", "kasse.read"))
ok("keine Benutzer-ID: nein", not client().may("", "kasse.read"))
dead = ac.Authz(url="http://127.0.0.1:9/authz", key="k", timeout=1)
ok("nicht erreichbar: nein", not dead.may("u", "kasse.read"))
STATE["mode"] = "500"
ok("HTTP 500: nein", not client().may("u", "kasse.read"))
STATE["mode"] = "garbage"
ok("Muell statt JSON: nein", not client().may("u", "kasse.read"))
STATE["mode"] = "no-grants-key"
ok("Antwort ohne 'grants': nein", not client().may("u", "kasse.read"))
STATE["mode"] = "ok"
STATE["user"] = "jemand-anderes"
ok("KOEDER: Antwort ueber einen ANDEREN Benutzer: nein",
   not client().may("u-ben", "kasse.read"))
STATE["user"] = None
STATE["app"] = "andere-app"
ok("KOEDER: Antwort einer ANDEREN App: nein",
   not client().may("u-ben", "kasse.read"))
ok("ohne App-Kennung im Client wird die App nicht geprueft (bewusst)",
   client(app=None).may("u-ben", "kasse.read"))
STATE["app"] = "vereinsportal"
ok("danach geht es wieder", client().may("u-ben", "kasse.read"))

print("Zwischenspeicher (A4: hoechstens 30 Sekunden)")
STATE.update(mode="ok", fresh=10)
c = client()
STATE["calls"] = 0
c.may("u-ben", "kasse.read")
c.may("u-ben", "team.read", team="mB")
ok("zwei Fragen, eine Anfrage", STATE["calls"] == 1, STATE["calls"])
T[0] += 9
c.may("u-ben", "kasse.read")
ok("nach 9 s noch aus dem Speicher", STATE["calls"] == 1)
T[0] += 2
c.may("u-ben", "kasse.read")
ok("nach 11 s wird neu gefragt (fresh_for war 10)", STATE["calls"] == 2)
STATE["fresh"] = 3600
c = client()
STATE["calls"] = 0
c.may("u-ben", "kasse.read")
T[0] += 31
c.may("u-ben", "kasse.read")
ok("KOEDER: fresh_for 3600 wird auf 30 begrenzt", STATE["calls"] == 2,
   STATE["calls"])
STATE["fresh"] = 0
c = client()
STATE["calls"] = 0
c.may("u-ben", "kasse.read")
c.may("u-ben", "kasse.read")
ok("fresh_for 0: nie aus dem Speicher", STATE["calls"] == 2)
STATE["fresh"] = "viel"
c = client()
STATE["calls"] = 0
c.may("u-ben", "kasse.read")
c.may("u-ben", "kasse.read")
ok("ein unlesbares fresh_for heisst: nicht speichern", STATE["calls"] == 2)

print("Client und Kern antworten gleich")
asks = [("team.edit_lineup", {"team": "mB"}), ("team.edit_lineup", {"team": "mC"}),
        ("team.edit_lineup", {}), ("team.read", {"team": "mB"}),
        ("news.publish", {"area": "news"}), ("news.publish", {"area": "x"}),
        ("news.publish", {}), ("kasse.read", {}), ("kasse.read", {"x": "y"}),
        ("lager.read", {}), ("team.fly", {"team": "mB"}), ("", {}),
        ("team", {}), ("team.", {"team": "mB"})]
diff = [(p, f) for p, f in asks
        if ac.may_in(GRANTS, p, **f) != az.may(GRANTS, p, **f)]
ok("dieselbe Antwort auf alle Faelle (may)", diff == [], diff)
diff = [p for p, _ in asks
        if ac.may_any_in(GRANTS, p) != az.may_any(GRANTS, p)]
ok("dieselbe Antwort auf alle Faelle (may_any)", diff == [], diff)
grid = itertools.product(["team.read", "news.read"], [{}, {"team": "mB"},
                                                       {"area": "news"},
                                                       {"team": "mB",
                                                        "area": "news"}])
diff = [(p, f) for p, f in grid
        if ac.may_in(GRANTS, p, **f) != az.may(GRANTS, p, **f)]
ok("... auch auf der Kreuztabelle", diff == [], diff)

print("")
print("ALLE PRUEFUNGEN BESTANDEN" if not fails else f"{fails} FEHLER")
sys.exit(1 if fails else 0)
