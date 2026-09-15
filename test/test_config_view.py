#!/usr/bin/env python3
"""Die Konfigurationskarte zeigt die WAHREN Werte -- und Speichern
schreibt nur, was jemand geaendert hat (oaap.core.portal 2.4, 0.1.100).

Der Anlass, 2026-09-15: Beim Umbau der Karte speicherte ein Live-Test auf
oaap-test ein unveraendertes Formular -- und der Knoten meldete vier
Schluessel als geaendert. Die Karte hatte jeden nicht-vertraulichen Wert
LEER angezeigt, und das Speichern hatte die leeren Felder zurueck-
geschrieben. Auf oaapx01 war genau das an diesem Tag schon dreimal an der
Test-Instanz eines Kunden passiert: Wer die CORS-Adresse eintrug, loeschte
damit Titel, Farbe und Upload-Grenze.

Die Ursache ist das Muster, das dieses Projekt schon dreimal bezahlt hat:
RFC-0026 zog die Instanzdaten nach `tenants/<tid>/instances/<iid>/`, und
ein Leser blieb am alten Ort. Das Portal las `/apps-registry/<key>/
instance.env` -- eine Datei, die es dort nicht mehr gibt. Kein Fehler,
kein Hinweis: ein leeres Feld sieht aus wie ein leerer Wert.

Die Reparatur hat zwei Haelften, und diese Datei haelt beide fest:

1. Der HOST schreibt die Werte in eine Ansicht neben die Registry, die das
   Portal lesen darf -- nicht-vertrauliche Werte, und bei jedem Schluessel
   nur, OB er gesetzt ist. Fehlt die Ansicht, bietet die Karte kein
   Speichern an.
2. Das PORTAL schickt nur Felder, deren Eingabe sich von der Anzeige
   unterscheidet. Eine falsche Ansicht kostet dann eine falsche Anzeige,
   nie einen Wert, den niemand angefasst hat.

Geprueft wird ueber die ECHTE Grenze: appctl schreibt die Ansicht in ein
Wegwerf-Datenverzeichnis, und der echte Portal-Code liest genau diese
Datei.

Braucht PyYAML (appctl) und jinja2, kein Docker, keinen Knoten.

Aufruf: python3 test/test_config_view.py
"""
import importlib
import json
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PLATFORM = os.path.join(HERE, "..", "platform")
PORTAL = os.path.join(PLATFORM, "services", "portal")
sys.path.insert(0, PLATFORM)

try:
    from jinja2 import Template
except ImportError:
    print("SKIP: jinja2 ist nicht installiert (pip install jinja2)")
    sys.exit(0)

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:500]}")


DATA = tempfile.mkdtemp(prefix="oaap-cfgview-test-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.modules.pop("appctl", None)
a = importlib.import_module("appctl")
sys.path.insert(0, PORTAL)
sys.modules.pop("instance_view", None)
iv = importlib.import_module("instance_view")

SRC = open(os.path.join(PORTAL, "app.py"), encoding="utf-8").read()
APPCTL = open(os.path.join(PLATFORM, "appctl.py"), encoding="utf-8").read()
MIGRATE = open(os.path.join(PLATFORM, "migrate.sh"), encoding="utf-8").read()

ORIGINS, TITLE, HOSTS = "GLISS_ALLOWED_ORIGINS", "GLISS_BRANDING_TITLE", "GLISS_HOSTS"
SECRET, EMPTY_SECRET = "GLISS_API_SECRET", "GLISS_EMPTY_SECRET"

print("")
print("Der Host schreibt, was die Karte zeigen darf")

tid = a.ensure_default_tenant()
reg = a.load_registry()
reg["instances"]["gliss"] = {
    "app_id": "gliss-viewer", "channel": "test", "tenant": tid,
    "id": "aaaaaaaaaaaa", "version": "0.1.40",
    "config": [
        {"key": ORIGINS, "label": "CORS", "secret": False},
        {"key": TITLE, "label": "Titel", "secret": False},
        {"key": HOSTS, "label": "Hosts", "secret": False, "multiline": True},
        {"key": SECRET, "label": "Geheimnis", "secret": True},
        {"key": EMPTY_SECRET, "label": "Leeres Geheimnis", "secret": True},
    ],
}
a.save_registry(reg)
inst = reg["instances"]["gliss"]
a.save_env("gliss", {ORIGINS: "https://demo.example:50001", TITLE: "Viewer",
                     HOSTS: "a.example;b.example", SECRET: "s3cr3t-wert",
                     EMPTY_SECRET: "", "OAAP_APP_SECRET": "plattform-eigen"},
           inst)

envfile = a.env_path("gliss", inst)
ok("die instance.env liegt im Mandantenbaum -- wie auf einem echten Knoten",
   os.sep + "tenants" + os.sep in envfile and os.path.isfile(envfile), envfile)
ok("und NICHT am alten Ort, den das Portal frueher las",
   not os.path.exists(os.path.join(a.APPS_DIR, "gliss", "instance.env")))

ok("die Ansicht liegt neben der Registry (im Mount des Portals)",
   os.path.dirname(a.CONFIG_VIEW) == a.APPS_DIR
   and os.path.isfile(a.CONFIG_VIEW), a.CONFIG_VIEW)
raw = open(a.CONFIG_VIEW, encoding="utf-8").read()
view = json.loads(raw)
mine = (view.get("instances") or {}).get("gliss") or {}
ok("sie traegt die nicht-vertraulichen Werte, genau wie gespeichert",
   mine.get("values") == {ORIGINS: "https://demo.example:50001",
                          TITLE: "Viewer", HOSTS: "a.example;b.example"},
   mine.get("values"))
ok("ein vertraulicher Wert steht NIRGENDS in der Datei",
   "s3cr3t-wert" not in raw, raw[:400])
ok("der plattform-eigene Schluessel auch nicht -- weder Wert noch Name",
   "plattform-eigen" not in raw and "OAAP_APP_SECRET" not in raw)
ok("bei jedem Schluessel steht nur, OB er gesetzt ist",
   mine.get("set") == sorted([ORIGINS, TITLE, HOSTS, SECRET]), mine.get("set"))
ok("sie sagt, wann sie geschrieben wurde", bool(view.get("written")))
if os.name == "posix":
    ok("lesbar fuer den Portal-Container (0644)",
       oct(os.stat(a.CONFIG_VIEW).st_mode & 0o777) == "0o644",
       oct(os.stat(a.CONFIG_VIEW).st_mode & 0o777))

a.save_env("gliss", dict(a.load_env("gliss", inst), **{TITLE: "Neuer Titel"}), inst)
mine = json.load(open(a.CONFIG_VIEW, encoding="utf-8"))["instances"]["gliss"]
ok("jedes Schreiben der instance.env zieht die Ansicht nach",
   mine["values"][TITLE] == "Neuer Titel", mine["values"])

ok("migrate.sh schreibt sie beim Update einmal (sonst bis zur naechsten Aenderung keine Karte)",
   "appctl.py\" config-index" in MIGRATE)
ok("und appctl kennt den Befehl dazu",
   'add_parser("config-index"' in APPCTL)

print("")
print("Das Portal liest genau diese Datei -- und nichts im Mandantenbaum")

ok("der alte Pfad ist aus dem Portal verschwunden",
   "/apps-registry/{name}/instance.env" not in SRC)

ns = {"json": json, "os": os, "secrets": __import__("secrets"), "iv": iv,
      "RESERVED_ENV": {"OAAP_APP_SECRET"}}
block = SRC[SRC.index('CONFIG_VIEW = "/apps-registry/'):
            SRC.index('@app.get("/instances")')]
exec(compile(block, "portal-config", "exec"), ns)  # noqa: S102
ns["CONFIG_VIEW"] = a.CONFIG_VIEW   # dieselbe Datei, nur ohne Container-Mount
block2 = SRC[SRC.index("def _config_changes("):SRC.index("def _config_outcome(")]
exec(compile(block2, "portal-config-changes", "exec"), ns)  # noqa: S102

rows = ns["_instance_config"]("gliss", inst)
by = {r["key"]: r for r in rows}
ok("die Karte zeigt die CORS-Adresse, wie sie gespeichert ist",
   by[ORIGINS]["value"] == "https://demo.example:50001", by[ORIGINS])
ok("und den Titel", by[TITLE]["value"] == "Neuer Titel", by[TITLE])
ok("die Liste als eine Angabe je Zeile",
   by[HOSTS]["value"] == iv.value_to_lines("a.example;b.example"), by[HOSTS])
ok("das gesetzte Geheimnis heisst 'gesetzt' -- ohne Wert",
   by[SECRET]["is_set"] and by[SECRET]["value"] == "", by[SECRET])
ok("das leere Geheimnis heisst 'nicht gesetzt'",
   not by[EMPTY_SECRET]["is_set"])
ok("der plattform-eigene Schluessel bekommt keine Zeile",
   "OAAP_APP_SECRET" not in by)

start = SRC.index('<form method="post" action="/instances/{{ i.key }}/config">')
CARD = Template(SRC[start:SRC.index("</form>", start) + len("</form>")])
html = CARD.render(i={"key": "gliss", "config": rows})
ok("im Formular steht der echte Wert im Feld",
   'value="https://demo.example:50001"' in html)

print("")
print("Speichern schreibt nur, was jemand geaendert hat")


def browser_form(rows):
    """Was ein Browser beim Absenden schickt: jedes Feld, wie angezeigt."""
    return {f"cfg-{r['key']}": r["value"] for r in rows}


values, err = ns["_config_changes"](rows, browser_form(rows))
ok("unveraendert abgeschickt: es wird NICHTS geschrieben",
   values == {} and not err, values)

form = browser_form(rows)
form[f"cfg-{ORIGINS}"] = "https://neu.example:50001"
values, err = ns["_config_changes"](rows, form)
ok("Joergs Fall: nur die CORS-Adresse geaendert -- nur sie geht an den Host",
   values == {ORIGINS: "https://neu.example:50001"} and not err, values)

form = browser_form(rows)
form[f"cfg-{HOSTS}"] = by[HOSTS]["value"].replace("\n", "\r\n")
values, err = ns["_config_changes"](rows, form)
ok("eine Liste mit Windows-Zeilenenden ist dieselbe Liste, keine Aenderung",
   values == {} and not err, values)

form = browser_form(rows)
form[f"cfg-{SECRET}"] = "neues-geheimnis"
values, err = ns["_config_changes"](rows, form)
ok("ein eingetipptes Geheimnis wird geschrieben",
   values == {SECRET: "neues-geheimnis"}, values)

print("")
print("Die alte Falle, nachgestellt: die Ansicht fehlt")

os.remove(a.CONFIG_VIEW)
ok("ohne Ansicht weiss das Portal es -- None, nicht 'leer'",
   ns["_config_view"]("gliss") is None)
blind = ns["_instance_config"]("gliss", inst)
values, err = ns["_config_changes"](blind, browser_form(blind))
ok("und selbst ein abgeschicktes leeres Formular schreibt nichts leer",
   values == {}, values)

body = SRC[SRC.index('@app.post("/instances/<name>/config")'):]
body = body[:body.index("\n@app.")]
ok("die Speicher-Route verweigert ohne Ansicht, bevor sie etwas schickt",
   "_config_view(name) is None" in body
   and body.index("_config_view(name) is None") < body.index("_queue_and_wait"),
   body[:600])
ok("die Seite bekommt gesagt, ob die Werte bekannt sind",
   '"config_known": _config_view(name) is not None' in SRC)
tpl = SRC[SRC.index("<section class=\"panel {{ 'active' if tab == 'konfiguration' }}\">"):]
tpl = tpl[:tpl.index("</section>")]
ok("und die Karte bietet dann kein Formular an",
   re.search(r"\{% if not i\.config_known %\}.*?\{% elif i\.config %\}", tpl, re.S)
   is not None, tpl[:400])

print("")
print(f"{'ALLE PRUEFUNGEN BESTANDEN' if not fails else str(fails) + ' FEHLGESCHLAGEN'}")
sys.exit(1 if fails else 0)
