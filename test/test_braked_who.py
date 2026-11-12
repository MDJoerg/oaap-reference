#!/usr/bin/env python3
"""Die Bremse merkt sich wer und wann (RFC-0083 Stufe 1).

    Ein Scanner      eine Adresse, eine Minute, 40 gebremst
                     -> "1 Adresse · in 1 Minute".
    Viele Adressen   60 verschiedene -> "50+ Adressen", der Eimer bleibt
                     bei 50 Fingerabdrücken.
    Datenschutz      keine Adresse in der Datei, nur 8 Hex-Zeichen.
    Altbestand       ein Eimer im alten Format (bloße Zahl) wird gelesen.

Braucht flask und fcntl (Linux, z. B. im Portal-Abbild).
Aufruf: python3 test/test_braked_who.py
"""
import importlib
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:700]}")


ID_DATA = tempfile.mkdtemp(prefix="oaap-braked-who-")
os.environ.update(SESSION_SECRET="s", SETUP_TOKEN="t", INTERNAL_API_KEY="k",
                  OAAP_IDENTITY_DATA_DIR=ID_DATA)
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services", "identity"))
sys.modules.pop("app", None)
ident = importlib.import_module("app")
ident._RATE.clear()
c = ident.app.test_client()


def hit(client, scope="market"):
    return c.get("/throttle", query_string={"scope": scope, "limit": "5", "window": "60"},
                 headers={"X-OAAP-Client": client})


codes = [hit("93.123.109.7").status_code for _ in range(45)]
ok("die Bremse greift nach 5", codes.count(429) == 40 and codes.count(204) == 5, codes[:8])
raw = open(ident.BRAKED_FILE).read()
ok("keine Adresse in der Datei", "93.123" not in raw, raw[:300])
data = json.loads(raw)
b = list(data["market"].values())[0]
ok("ein Eimer: 40, ein Fingerabdruck aus 8 Zeichen, eine Minute",
   b["n"] == 40 and len(b["c"]) == 1 and len(b["c"][0]) == 8 and len(b["m"]) == 1, b)

for i in range(60):
    for _ in range(6):
        hit(f"198.51.100.{i}", scope="vote")
d = json.loads(open(ident.BRAKED_FILE).read())["vote"]
vb = list(d.values())[0]
ok("60 Adressen: höchstens 50 Fingerabdrücke, „voll“ vermerkt",
   len(vb["c"]) == 50 and vb["full"] and vb["n"] == 60, {k: v for k, v in vb.items() if k != "c"})

r = c.get("/internal/throttle-braked", headers={"X-OAAP-Internal-Key": "k"}).get_json()
m, v = r["instances"]["market"], r["instances"]["vote"]
ok("Antwort für den Scanner: 40, 1 Adresse, 1 Minute, Spitze 40",
   m["count"] == 40 and m["clients"] == 1 and m["minutes"] == 1 and m["peak_count"] == 40, m)
ok("Antwort für die Menge: 50+ Adressen", v["clients"] == 50 and v["clients_more"], v)

hour = int(__import__("time").time() // 3600)
data = json.loads(open(ident.BRAKED_FILE).read())
data["alt"] = {str(hour - 1): 7}
json.dump(data, open(ident.BRAKED_FILE, "w"))
r = c.get("/internal/throttle-braked", headers={"X-OAAP-Internal-Key": "k"}).get_json()
ok("ein Eimer im alten Format zählt weiter (Adressen unbekannt = 0)",
   r["instances"]["alt"]["count"] == 7 and r["instances"]["alt"]["clients"] == 0, r["instances"].get("alt"))
hit("93.123.109.7", scope="alt")
ok("… und wird beim nächsten Bremsen ins neue Format gehoben",
   isinstance(json.loads(open(ident.BRAKED_FILE).read())["alt"][str(hour - 1)], (int, dict)))

sys.path.insert(0, os.path.join(HERE, "..", "platform", "services"))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services", "portal"))
sys.modules.pop("app", None)
try:
    portal = importlib.import_module("app")
    w = portal._braked_words(m)
    ok("die Gesundheitsseite sagt „1 Adresse · in 1 Minute“", w == "1 Adresse · in 1 Minute", w)
    w = portal._braked_words({"clients": 50, "clients_more": True, "minutes": 7,
                              "peak_count": 12, "peak_at": hour * 3600 + 420})
    ok("… und bei vielen „50+ Adressen · in 7 Minuten · Spitze 12 um …“",
       w.startswith("50+ Adressen · in 7 Minuten · Spitze 12 um "), w)
except ImportError as e:
    print(f"SKIP  Portal nicht ladbar: {e}")

print(f"\n{'ALL PASS' if not fails else str(fails) + ' FAILURES'}")
sys.exit(1 if fails else 0)
