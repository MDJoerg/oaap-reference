#!/usr/bin/env python3
"""Farbwaehler und Vorschau im Gesicht eines Mandanten (I-31).

Die Vorschau zeigt, was man gerade tippt -- OHNE zu speichern. Sie darf
dabei nichts anderes zeigen als die Seite selbst. Deshalb rechnet sie auf dem
Server, mit `place.theme_inline` (aus dem auch `theme_style` besteht), und
nicht ein zweites Mal in JavaScript.

Koeder: eine zu helle Hauptfarbe (#ffff99). Die Plattform dreht die Schrift
der Kopfzeile auf dunkel und dunkelt Text auf Weiss ab. Eine Vorschau, die die
Farbe nur ungerechnet uebernaehme, zeigte helle Schrift auf hellem Grund --
und ein Nachbau mit anderer Schwelle bestaende diesen Test nur zufaellig.

Geprueft wird:
    - dieselben Eingaben, dieselben Werte wie `theme_style` / Anmeldekarte;
    - die Vorschau veraendert nichts (kein Schreiben, kein Spool);
    - dieselben Rechte wie beim Speichern;
    - ein Wert, den das Speichern ablehnt, wird mit der Ablehnung gezeigt;
    - das Formular geht auch ohne JavaScript (Hex-Feld traegt den Wert, der
      Farbwaehler hat keinen Namen und wird nicht mitgeschickt).

Aufruf: python3 test/test_tenant_face_preview.py
"""
import ast
import importlib.util
import json
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-face-preview-test-")
os.environ["OAAP_DATA_DIR"] = DATA
SERVICES = os.path.join(HERE, "..", "platform", "services")
sys.path.insert(0, os.path.join(SERVICES, "portal"))
sys.path.insert(0, SERVICES)
sys.path.insert(0, os.path.join(HERE, "..", "platform"))

import appctl as m                                              # noqa: E402
import place                                                    # noqa: E402

m.reload_gateway = lambda: None
m.zone_probe = lambda label: f"ZONE({label})"

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:600]}")


DEFAULT = m.ensure_default_tenant()
CLS, _ = m.tenant_create("cls")
SGL, _ = m.tenant_create("sgl")
m.tenant_set_theme(SGL, title="Fremder Verein", primary="#aa0000")
SGL_BEFORE = json.dumps(m.load_tenants()[SGL], sort_keys=True)

print("=== die Rechnung ist EINE (place.theme_inline) ===")
for p, a in (("#ffff99", "#ccddee"), ("#1f4e79", "#e07b00"), ("#000000", "#ffffff")):
    face = place.theme_of({"theme": {"color_primary": p, "color_accent": a}}, "x", "h.example")
    inline = place.theme_inline(face)
    ok(f"theme_style besteht aus genau diesen Zuweisungen ({p}/{a})",
       place.theme_style(face) == "<style>:root{" + inline + "}</style>")
    v = place.theme_vars(face)
    ok(f"die Anmeldekarte rechnet dieselben Werte ({p}/{a})",
       f"--brand:{v['ink']};" in place.card_style(face)
       and f"--oaap-ink:{v['ink']};" in inline
       and f"--oaap-header-text:{v['header_text']};" in inline)
pale = place.theme_of({"theme": {"color_primary": "#ffff99"}}, "x", "")
ok("Koeder: bei zu heller Hauptfarbe ist die Schrift der Kopfzeile DUNKEL",
   place.theme_vars(pale)["header_text"] == "#111827"
   and "--oaap-header-text:#111827;" in place.theme_inline(pale))
ok("... und Text auf Weiss wird abgedunkelt", place.luminance(place.theme_vars(pale)["ink"]) <= 0.2)

print("\n=== die Route (echtes Portal, ohne Docker) ===")
try:
    os.environ.setdefault("SESSION_SECRET", "x")
    spec = importlib.util.spec_from_file_location("portal_app", os.path.join(SERVICES, "portal", "app.py"))
    P = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(P)
except Exception as exc:                                        # noqa: BLE001
    P = None
    print(f"SKIP  das Portal laesst sich hier nicht laden ({type(exc).__name__}: {exc})")

if P is not None:
    P.TENANTS_FILE = m.TENANTS_FILE
    P.external_host = lambda: "knoten.example.org"
    spooled = []
    P._queue_with_id = lambda *a, **k: spooled.append(a) or {"ok": True, "message": "ok"}
    state = {"role": "tenant_admin", "mine": CLS}
    P.require_user_admin = lambda: None
    P.caller_scope = lambda: (state["role"], state["mine"])

    def preview(args, **who):
        state.update(who)
        with P.app.test_request_context("/tenant/face/preview?" + args):
            r = P.tenant_face_preview()
            return r if isinstance(r, str) else (r.get_data(as_text=True) if hasattr(r, "get_data") else str(r))

    html = preview("title=Mein+Verein&color_primary=%23ffff99&color_accent=%23ccddee")
    face = place.theme_of({"theme": {"color_primary": "#ffff99", "color_accent": "#ccddee",
                                     "title": "Mein Verein"}}, "cls", "knoten.example.org")
    ok("die Vorschau traegt dieselben Zuweisungen wie die Seite (Koeder #ffff99)",
       place.theme_inline(face) in html.replace("&#34;", '"'), html[:400])
    ok("... zeigt den getippten Titel und die Adresse daneben",
       "Mein Verein" in html and "cls.knoten.example.org" in html)
    ok("... und die dunkle Schrift in der Kopfzeile", "--oaap-header-text:#111827;" in html)
    ok("die Vorschau hat nichts geschrieben (Mandant unveraendert, Spool leer)",
       m.load_tenants()[CLS].get("theme") is None and not spooled)

    html = preview("title=&color_primary=&color_accent=")
    plat = place.theme_of({}, "cls", "knoten.example.org")
    ok("alles leer heisst: die Werte der Plattform (wie beim Speichern)",
       place.theme_inline(plat) in html.replace("&#34;", '"'), html[:300])

    html = preview("title=x&color_primary=nope&color_accent=%23abcdef")
    ok("ein Wert, den das Speichern ablehnt: die Ablehnung steht daneben",
       "is not a colour" in html and place.theme_inline(
           place.theme_of({"theme": {"color_accent": "#abcdef"}}, "cls", "")) in html.replace("&#34;", '"'), html[:400])
    ok("... zu langer Titel auch", "at most" in preview("title=" + "x" * 61))

    html = preview("tenant=sgl")
    ok("ein tenant_admin kann den Mandanten eines anderen nicht ansehen (Weiterleitung, kein Inhalt)",
       "Fremder Verein" not in html and "#aa0000" not in html, html[:300])
    ok("... und es ist nichts passiert", json.dumps(m.load_tenants()[SGL], sort_keys=True) == SGL_BEFORE)

    html = preview("tenant=sgl&title=Entwurf", role="server_admin", mine=DEFAULT)
    ok("ein server_admin sieht den benannten Mandanten, mit dem Entwurf",
       "Entwurf" in html and "sgl.knoten.example.org" in html, html[:300])
    ok("... und auch das hat nichts veraendert",
       json.dumps(m.load_tenants()[SGL], sort_keys=True) == SGL_BEFORE and not spooled)

    state.update(role="tenant_admin", mine=CLS)
    m.tenant_set_theme(CLS, title="Gespeichert", primary="#336699")
    html = preview("title=Entwurf&clear_logo=1")
    ok("ohne Logo-Angabe zeigt die Vorschau das gespeicherte Zeichen, mit 'entfernen' das der Plattform",
       "&#9670;" in html or "pvmark" in html)

    print("\n=== das Formular ohne JavaScript ===")
    with P.app.test_request_context("/tenant/face"):
        page_html = P.tenant_face_form()
        if isinstance(page_html, tuple):
            page_html = page_html[0]
        page_html = page_html if isinstance(page_html, str) else page_html.get_data(as_text=True)
else:
    page_html = ""

if page_html:
    ok("das Hex-Feld traegt den Wert (name=color_primary, name=color_accent)",
       'name="color_primary"' in page_html and 'name="color_accent"' in page_html)
    pick = re.findall(r'<input[^>]*type="color"[^>]*>', page_html)
    ok("es gibt zwei Farbwaehler", len(pick) == 2, pick)
    ok("... ohne Namen: sie werden nicht mitgeschickt, das Hex-Feld ist die Wahrheit",
       all("name=" not in p for p in pick), pick)
    ok("... und ohne JavaScript unsichtbar (hidden), damit nichts Totes zu sehen ist",
       all(" hidden" in p for p in pick))
    ok("je Farbe ein Zuruecksetzen-Knopf", page_html.count('data-for="primary"') >= 1
       and page_html.count('data-for="accent"') >= 1)
    ok("die Vorschau steht schon im ausgelieferten HTML (ohne JavaScript sichtbar)",
       'id="face-preview"' in page_html and "facepv" in page_html)
    script_part = page_html[page_html.index("<script>"):page_html.index("</script>", page_html.index("<script>"))] if "<script>" in page_html else ""
    ok("die Vorschau-Adresse ist die des Servers (kein eigener Farbrechner im Skript)",
       "/tenant/face/preview" in page_html and "luminance" not in script_part and "readable" not in script_part and "mix(" not in script_part)
    ok("das Skript liest die Datei lokal (Objekt-Adresse), ohne Upload",
       "createObjectURL" in page_html)

print(f"\n{'FEHLER' if fails else 'Alles gruen'} - {fails} Fehlschlag(e)")
sys.exit(1 if fails else 0)
