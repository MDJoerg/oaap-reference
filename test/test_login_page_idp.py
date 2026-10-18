#!/usr/bin/env python3
"""Anmeldeseite mit Anbieter: der Knoten-Login ist eingeklappt (2026-10-18).

Wo ein Anbieter (IdP) hinterlegt ist, soll der Anbieter-Knopf auffallen und
Benutzername/Passwort der Plattform hinter einer eingeklappten Gruppe stehen.
Ohne Anbieter bleibt das Formular offen. Nach einem Fehlversuch bleibt es offen
(sonst sieht man nicht, warum es nicht ging). Gerendert wird die echte Vorlage.

Aufruf: python3 test/test_login_page_idp.py
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("OAAP_DATA_DIR", tempfile.mkdtemp(prefix="oaap-login-"))
os.environ.setdefault("SESSION_SECRET", "t")
os.environ.setdefault("SETUP_TOKEN", "t")
os.environ.setdefault("INTERNAL_API_KEY", "t")
os.environ.setdefault("OAAP_IDENTITY_DATA_DIR", tempfile.mkdtemp(prefix="oaap-login-id-"))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services", "identity"))
sys.path.insert(0, os.path.join(HERE, "..", "platform"))

try:
    import jinja2
    import app as ident                                           # noqa: E402
except ImportError as e:
    print("SKIP  " + str(e))
    sys.exit(0)

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print("      " + str(detail)[:400])


class Face:
    title, address, logo_url = "Verein", "v.example.org", ""


def page(**kw):
    ctx = dict(provider="", error="", has_users=True, at_place=True, face=Face(),
               show_face=True, theme_style="", next="")
    ctx.update(kw)
    return jinja2.Template(ident.LOGIN_PAGE).render(**ctx)


p = page(provider="Mit dem Vereinskonto anmelden")
ok("mit Anbieter: der Anbieter-Knopf steht da", "Mit dem Vereinskonto anmelden" in p)
ok("das Formular der Plattform steht in einer EINGEKLAPPTEN Gruppe",
   '<details class="local">' in p and "<details class=\"local\" open" not in p
   and p.index("<details") < p.index('action="/auth/login"') < p.index("</details>"), p)
ok("der Anbieter-Knopf steht VOR der Gruppe und nicht darin",
   p.index("/auth/oidc/start") < p.index("<details"))
ok("kein Autofokus im versteckten Feld", "autofocus" not in p)

p = page(provider="X", error="Falsches Passwort")
ok("nach einem Fehlversuch ist die Gruppe offen und zeigt die Meldung",
   '<details class="local" open>' in p and "Falsches Passwort" in p and "autofocus" in p, p)

p = page()
ok("ohne Anbieter: kein Einklappen, Autofokus bleibt",
   "<details" not in p and "autofocus" in p and 'action="/auth/login"' in p)
ok("ohne Anbieter: kein Anbieter-Knopf", "/auth/oidc/start" not in p)

print("\nALLE PRUEFUNGEN BESTANDEN" if not fails
      else f"\n{fails} PRUEFUNG(EN) FEHLGESCHLAGEN")
sys.exit(1 if fails else 0)
