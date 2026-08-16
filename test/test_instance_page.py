#!/usr/bin/env python3
"""Die Objektseite einer Instanz — Kopfbereich und Reiter.

Prueft die Regeln aus den Design-Guidelines 6.2.1/6.2.2 an der echten
Vorlage aus `portal/app.py`, ohne Flask, ohne Container, ohne Knoten:
die Vorlage wird aus der Quelle gelesen und mit Jinja2 gerendert.

Festgehalten wird bewusst die REGEL, nicht das heutige Aussehen:

* genau ein Abschnitt ist sichtbar, aber ALLE stehen im Dokument
  (faellt das Stylesheet aus, ist die Seite lang statt kaputt),
* der erste Reiter ist lesend — kein Formular,
* eine anstehende Bestaetigung steht UEBER den Reitern und ist damit
  in jedem Reiter sichtbar,
* jedes Formular traegt seinen Reiter mit, damit das Speichern dorthin
  zurueckfuehrt, wo es ausgeloest wurde,
* die gefaehrliche Aktion liegt allein im letzten Reiter,
* leere Abschnitte sagen, warum sie leer sind, statt zu verschwinden.

Aufruf: python3 test/test_instance_page.py
"""
import ast
import io
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "platform", "services", "portal"))
import instance_view as iv                                   # noqa: E402

try:
    from jinja2 import Environment
except ImportError:                                          # pragma: no cover
    print("jinja2 fehlt — pip install jinja2")
    sys.exit(1)

APP_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "..", "platform", "services", "portal", "app.py")

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:500]}")


def template(name):
    """Die Vorlage aus app.py holen, ohne app.py zu importieren.

    Der Import zoege Flask und requests herein — beides steht im
    Container, nicht auf jedem Rechner, und diese Pruefung soll ueberall
    laufen."""
    tree = ast.parse(io.open(APP_PY, encoding="utf-8").read())
    for node in tree.body:
        if (isinstance(node, ast.Assign)
                and any(getattr(t, "id", "") == name for t in node.targets)):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} nicht in app.py gefunden")


ENV = Environment(autoescape=True)
BODY = ENV.from_string(template("INSTANCE_EDIT_BODY"))

TESTINSTANZ = {
    "app_id": "bdt-hub", "app_name": "BDT Hub", "version": "0.4.1",
    "channel": "test",
    "routes": [{"path": "/", "roles": ["keyuser", "admin"]},
               {"path": "/api/", "roles": ["public"]}],
    "storage": [{"name": "data", "mount": "/data"}],
    "services": [{"service": "", "port": 8000}],
    "source": {"kind": "artifact", "version": "0.4.1", "stored": "0.4.1-ab12.zip",
               "received": "2026-08-16T09:12:00Z", "sha256": "a" * 64},
    "visibility": {"groups": ["buero"]},
    "config": [{"key": "HUB_URL", "label": "Adresse des Hubs", "secret": False}],
    "roles": ["keyuser", "admin"],
    "address": "hub.example.de", "aliases": ["hub2.example.de"],
}


def render(tab, inst=None, **over):
    inst = dict(inst or TESTINSTANZ)
    label, lines = iv.source_view(inst)
    i = {
        "name": "bdt-hub-test", "app_name": inst["app_name"],
        "version": inst["version"], "app_id": inst["app_id"],
        "description": "Zentrale fuer die Aussendienst-App",
        "channel_label": "Test" if inst["channel"] == "test" else "Produktiv",
        "is_test": inst["channel"] == "test",
        "address_host": inst.get("address", ""),
        "address_url": "https://" + inst["address"] if inst.get("address") else "",
        "visibility_label": iv.visibility_label(inst),
        "tile_visible": True, "tile_mode": "auto",
        "tile_reason": "Die App bezeichnet sich selbst als Anwendung mit Oberflaeche.",
        "source_label": label, "source_lines": lines,
        "route_rows": iv.route_rows(inst),
        "storage": inst.get("storage") or [],
        "services": inst.get("services") or [],
        "groups": (inst.get("visibility") or {}).get("groups") or [],
        "roles": inst.get("roles") or [],
        "config": [dict(c, value="", is_set=False) for c in inst.get("config") or []],
        "token_created": "2026-08-16 07:00",
        "artifacts": [{"file": "0.4.1-ab12.zip", "running": True,
                       "received": "2026-08-16 09:12"},
                      {"file": "0.4.0-99cc.zip", "running": False,
                       "received": "2026-08-15 18:03"}],
        "pending": None, "hook_url": "https://knoten.example.de/deploy/bdt-hub-test",
        "address": inst.get("address", ""), "aliases": inst.get("aliases") or [],
        "auto_address": "bdt-hub-test.knoten.example.de",
        "has_public_route": any("public" in (r.get("roles") or [])
                                for r in inst.get("routes") or []),
        "links": ["ollama"], "link_candidates": ["studio"],
        "endpoints": [], "node_exposed": False,
        "throttle_mode": "default", "throttle_rate": "",
        "throttle_default": "300 Anfragen pro 60 Sekunden",
    }
    i.update(over)
    return BODY.render(i=i, tabs=iv.TABS, tab=tab, msg=None, error=None)


def panels(html):
    """{Reiter-Reihenfolge: sichtbar?} — der aktive Abschnitt traegt
    zusaetzlich die Klasse `active`."""
    return [("active" in m) for m in re.findall(r'<section class="panel ([^"]*)"', html)]


print("=== Die Reiter selbst ===")
html = render(iv.DEFAULT_TAB)
ok("es gibt genau so viele Abschnitte wie Reiter",
   len(panels(html)) == len(iv.TABS), f"{len(panels(html))} vs {len(iv.TABS)}")
ok("jeder Reiter ist ein Link mit ?tab=",
   all(f'?tab={key}"' in html for key, _ in iv.TABS))
ok("kein JavaScript im Spiel", "<script" not in html and "onclick" not in html)
for key, _ in iv.TABS:
    h = render(key)
    ok(f"„{key}“ oeffnet genau einen Abschnitt", sum(panels(h)) == 1,
       str(panels(h)))
    ok(f"„{key}“ ist im Reiterband markiert",
       f'?tab={key}" class="active' in h or f'?tab={key}"\n     class="active' in h,
       h[h.find('<nav class="tabs">'):][:600])

print("\n=== Ein unbekannter Reiter ist kein Fehler ===")
ok("Unfug faellt auf den Lese-Reiter zurueck",
   iv.valid_tab("../../etc/passwd", iv.DEFAULT_TAB) == iv.DEFAULT_TAB)
ok("und ohne Angabe bleibt es leer (fuer den Redirect nach dem Speichern)",
   iv.valid_tab("", "") == "" and iv.valid_tab(None) == "")
ok("ein gueltiger Reiter kommt unveraendert durch",
   iv.valid_tab("netz", iv.DEFAULT_TAB) == "netz")

print("\n=== Nichts geht durch die Gruppierung verloren ===")
# Der Vorwurf, den Reiter am ehesten verdienen: eine Karte, die
# niemand mehr findet. Alles bleibt im Dokument, egal welcher Reiter
# offen ist — nur sichtbar ist eben einer.
KARTEN = ["Sichtbarkeit", "Kachel im Launchpad", "Eigene Adresse",
          "Drosselung öffentlicher Routen", "Verbindungen zu anderen Apps",
          "Deploy-Token", "Hochgeladene Pakete", "Konfiguration",
          "Instanz entfernen", "Was die App mitbringt", "Herkunft"]
for tab_key in (iv.DEFAULT_TAB, "verwaltung"):
    h = render(tab_key)
    fehlt = [k for k in KARTEN if k not in h]
    ok(f"in „{tab_key}“ steht jede Karte im Dokument", not fehlt, str(fehlt))

print("\n=== Der Kopfbereich beantwortet das Wichtigste ===")
h = render(iv.DEFAULT_TAB)
kopf = h[h.find('<div class="objhead">'):h.find('<nav class="tabs">')]
for was, text in [("Instanzname", "bdt-hub-test"), ("App und Version", "0.4.1"),
                  ("Kanal", "Test"), ("Adresse", "hub.example.de"),
                  ("Sichtbarkeit", "Gruppen buero"), ("Kachel", "im Launchpad"),
                  ("Herkunft", "Hochgeladenes Paket"),
                  ("Deploy-Token", "seit 2026-08-16"),
                  ("Verbindungen", "ollama")]:
    ok(f"der Kopf nennt {was}", text in kopf, kopf[-900:])
ok("und traegt selbst kein Formular", "<form" not in kopf)

print("\n=== Der erste Reiter ist lesend ===")
ueberblick = h[h.find('<section class="panel active"'):h.find('<section class="panel ">')]
ok("im Überblick steht kein Formular", "<form" not in ueberblick,
   ueberblick[:400])
ok("er zeigt die Routen mit ihren Rollen",
   "/api/" in ueberblick and "ohne Anmeldung" in ueberblick)
ok("er sagt, dass das die Selbstauskunft der App ist",
   "Manifest" in ueberblick)

print("\n=== Speichern fuehrt in denselben Reiter zurueck ===")
# Jedes Formular traegt seinen Abschnitt mit; ohne das landet man nach
# jedem Speichern wieder oben und sucht die Stelle erneut.
for tab_key, action in [("zugang", "/visibility"), ("zugang", "/tile"),
                        ("zugang", "/throttle"), ("netz", "/address"),
                        ("netz", "/link"), ("deployment", "/token"),
                        ("deployment", "/rollback"),
                        ("konfiguration", "/config"),
                        ("verwaltung", "/remove")]:
    formulare = [f for f in html.split("<form ")[1:] if action + '"' in f.split(">")[0]]
    ok(f"{action} traegt „{tab_key}“ mit",
       formulare and all(f'name="tab" value="{tab_key}"' in f for f in formulare),
       str(formulare[:1])[:300])

print("\n=== Eine anstehende Entscheidung steht ueber den Reitern ===")
h = render("konfiguration", pending={"version": "0.5.0", "manifest_sha": "b" * 64,
                                     "reasons": ["neue oeffentliche Route /api/"]})
ok("die Warnkarte steht vor dem Reiterband",
   0 < h.find("wartet auf Bestätigung") < h.find('<nav class="tabs">'))
ok("sie nennt den Grund", "neue oeffentliche Route /api/" in h)
ok("und der Kopf zeigt es als Merker", "Bestätigung offen" in h)
ok("ohne Anstehendes ist da nichts", "wartet auf Bestätigung" not in render("netz"))

print("\n=== Leere Abschnitte sagen, warum sie leer sind ===")
h = render("konfiguration", config=[], endpoints=[], artifacts=[])
ok("ohne Konfigurationsschluessel steht da eine Begruendung",
   "erklärt in ihrem Manifest keine Konfigurationswerte" in h)
ok("und kein Eingabefeld", 'name="cfg-' not in h)
ok("ohne Direktport steht da eine Begruendung",
   "keinen Port am Gateway vorbei" in h)

print("\n=== Produktiv bekommt kein Token, sagt aber warum ===")
prod = dict(TESTINSTANZ, channel="production")
h = render("deployment", prod, is_test=False, channel_label="Produktiv",
           token_created="")
ok("kein Knopf zum Erzeugen", 'action="/instances/bdt-hub-test/token"' not in h)
ok("dafuer der Grund", "wechselt über den Store" in h)
ok("und der Kopf sagt es auch", "nur für Test-Instanzen" in h)

print("\n=== Die gefaehrliche Aktion liegt allein im letzten Reiter ===")
h = render("verwaltung")
letzter = h[h.rfind('<section class="panel '):]
ok("„Instanz entfernen“ steht im letzten Abschnitt", "Instanz entfernen" in letzter)
ok("und der ist sichtbar, wenn man ihn waehlt", 'class="panel active"' in letzter)
ok("sonst steht dort nichts weiter", letzter.count("<h2>") == 1,
   str(letzter.count("<h2>")))
ok("der Reiter ist als heikel gekennzeichnet", "danger" in h)

print("\n=== Herkunft wird nicht erfunden ===")
label, lines = iv.source_view({})
ok("ohne Angabe heisst es unbekannt", label == "Unbekannte Herkunft", label)
ok("und die Seite sagt, warum", "bevor die Plattform" in " ".join(lines))
label, lines = iv.source_view({"source": {"kind": "git", "url": "https://x/y",
                                          "path": "", "ref": ""}})
ok("Git ohne Angabe heisst Standardbranch",
   "Branch oder Tag: Standardbranch" in lines, str(lines))

print(f"\n{'ALLE PRUEFUNGEN BESTANDEN' if not fails else str(fails) + ' FEHLER'}")
sys.exit(1 if fails else 0)
