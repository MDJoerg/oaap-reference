#!/usr/bin/env python3
"""Zwei Mandanten, derselbe Vorschlag des Anbieters -- zwei Namen (I-9).

Benutzernamen sind knotenweit eindeutig, und `preferred_username` ist in
jedem Kunden-Realm dasselbe Wort. Bis 0.1.167 bekam der zweite `max` ein
`-2` -- ein Name, den niemand gewaehlt hat, und ein Hinweis an Kunde B,
dass es bei Kunde A ein `max` gibt. Entschieden (Variante a):

    Neue Konten ausserhalb des Standard-Mandanten heissen
    `<kuerzel>.<vorschlag>`. Bestehende Konten bleiben, wie sie sind.

Geprueft wird der echte Weg: `_idp_principal` des Identitaetsdienstes,
zweimal mit denselben Anspruechen in zwei Mandanten -- nicht nur die
Namensfunktion. Braucht Flask wie der Dienst selbst.

Aufruf: python3 test/test_idp_username_prefix.py
"""
import importlib
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
IDENTITY_DIR = os.path.join(HERE, "..", "platform", "services", "identity")
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services"))

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:400]}")


import idp  # noqa: E402

print("=== die Namensfunktion ===")
p = idp.tenant_name_prefix({"label": "sgl"})
ok("ein Kunden-Mandant bekommt `<kuerzel>.`", p == "sgl.", p)
ok("der Standard-Mandant bekommt keinen", idp.tenant_name_prefix({"label": "default"}) == "")
ok("ein Satz ohne Kuerzel auch nicht", idp.tenant_name_prefix({}) == "")
claims = {"preferred_username": "max"}
ok("derselbe Vorschlag, zwei Mandanten, zwei Namen",
   idp.local_username(claims, [], prefix="sgl.") == "sgl.max"
   and idp.local_username(claims, [], prefix="cls.") == "cls.max")
ok("ohne Praefix bleibt es, wie es war",
   idp.local_username(claims, ["max"]) == "max-2")
ok("im selben Mandanten wird nummeriert",
   idp.local_username(claims, ["sgl.max"], prefix="sgl.") == "sgl.max-2")
long_p = "k" * 31 + "."
n = idp.local_username({"preferred_username": "x" * 50}, [], prefix=long_p)
ok("ein langes Kuerzel haelt die Grenze von 40 Zeichen und bleibt gueltig",
   len(n) <= 40 and idp.USERNAME_RE.match(n) and n.startswith(long_p), n)
ok("auch der Rueckfall aus dem sub traegt das Praefix",
   idp.local_username({"sub": "abc"}, [], prefix="sgl.") == "sgl.user-abc")

print("\n=== der echte Weg: _idp_principal in zwei Mandanten ===")
try:
    import flask  # noqa: F401
except ImportError as e:
    print(f"SKIP  Flask nicht importierbar ({e}).")
    sys.exit(1 if fails else 0)

DATA = tempfile.mkdtemp(prefix="oaap-prefix-test-")
os.environ["SESSION_SECRET"] = "test-session-secret"
os.environ["SETUP_TOKEN"] = "test-setup-token"
os.environ["OAAP_IDENTITY_DATA_DIR"] = DATA
os.environ.pop("INTERNAL_API_KEY", None)
sys.path.insert(0, IDENTITY_DIR)
sys.modules.pop("app", None)
app = importlib.reload(importlib.import_module("app"))

tfile = os.path.join(DATA, "tenants.json")
with open(tfile, "w", encoding="utf-8") as f:
    json.dump({"tenants": {
        "t-default": {"label": "default"},
        "t-sgl": {"label": "sgl"},
        "t-cls": {"label": "cls"}}}, f)
app.TENANTS_FILE = tfile
audited = []
app.audit = lambda *a, **k: audited.append((a, k))

# Ein Konto, das es schon gab -- ohne Praefix, und es muss so bleiben.
app.save_users([{"username": "max", "password_hash": "", "roles": [],
                 "groups": [], "active": True, "kind": "human",
                 "tenant": "t-default", "id": "u0", "session_epoch": 0}])


def provider(issuer):
    return {"kind": "keycloak", "issuer": issuer, "client_id": "oaap"}


def login(tid, sub, name="max"):
    u, err = app._idp_principal(
        tid, provider("https://auth.example/realms/" + tid),
        {"sub": sub, "preferred_username": name})
    return (u or {}).get("username"), err


a, ea = login("t-sgl", "sub-a")
b, eb = login("t-cls", "sub-b")
c, ec = login("t-default", "sub-c")
ok("Kunde A bekommt `sgl.max`", a == "sgl.max", (a, ea))
ok("Kunde B bekommt `cls.max` -- kein `-2`, kein Rueckschluss auf A",
   b == "cls.max" and "-2" not in (b or ""), (b, eb))
ok("der Standard-Mandant behaelt schlichte Namen (`max` war schon da)",
   c == "max-2", (c, ec))
ok("das bestehende Konto `max` blieb unveraendert",
   any(u["username"] == "max" and u["id"] == "u0" for u in app.load_users()))
a2, _ = login("t-sgl", "sub-a")
ok("dieselbe Person bleibt beim zweiten Login dieselbe (Bindung, nicht Name)",
   a2 == "sgl.max" and len([u for u in app.load_users()
                            if u["username"].startswith("sgl.")]) == 1)
a3, _ = login("t-sgl", "sub-z")
ok("eine zweite `max` im selben Mandanten wird dort nummeriert",
   a3 == "sgl.max-2", a3)
ok("der vergebene Name steht im Protokoll des Mandanten",
   any(k[0][0] == "user.idp-first-login" and k[0][2] == "sgl.max"
       for k in [(x[0], x[1]) for x in audited]) if audited else False, audited[:2])

print(f"\n{'OK' if not fails else 'FEHLER'}: {fails} Fehler")
sys.exit(1 if fails else 0)
