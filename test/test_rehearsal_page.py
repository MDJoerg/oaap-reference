#!/usr/bin/env python3
"""Die Generalprobe im Portal (RFC-0030 D5, Spec 2.15.3).

Vier Regeln, und die dritte ist die, die dieses Projekt schon dreimal
teuer bezahlt hat:

1. Der Knopf steht auf der Objektseite der **Produktiv**-Instanz, weil
   dort die Daten herkommen — nicht an der Testinstanz und nicht an
   einer Generalprobe.
2. Die Seite sagt **vor** der Kopie, wie gross sie wird und wie viel
   danach frei ist — mit den Zahlen **dieses** Knotens. Ein Knoten, der
   noch nie gemessen hat, sagt das und **leiht sich keine Zahl**
   (dieselbe Bedingung, die Joerg an RFC-0029 D1 geknuepft hat).
3. Die Seite misst nichts selbst. Sie darf den **Mandantenbaum nicht
   lesen** — dort liegt jede `instance.env` jedes Kunden. Sie liest die
   Ansicht, die der Host daneben schreibt, und die eigentliche Handlung
   fragt den Host noch einmal.
4. Das Alter des Archivs steht dabei. Eine Generalprobe auf einem zwei
   Wochen alten Archiv ist eine Generalprobe auf zwei Wochen alten
   Daten.

Geprueft wird an der echten Vorlage aus `portal/app.py`, mit Jinja2
gerendert — ohne Flask, ohne Container, ohne Knoten.

Aufruf: python3 test/test_rehearsal_page.py
"""
import ast
import datetime
import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services", "portal"))
import instance_view as iv                                    # noqa: E402

try:
    from jinja2 import Environment
except ImportError:                                          # pragma: no cover
    print("jinja2 fehlt — pip install jinja2")
    sys.exit(1)

APP_PY = os.path.join(HERE, "..", "platform", "services", "portal", "app.py")

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:500]}")


def template(name):
    tree = ast.parse(io.open(APP_PY, encoding="utf-8").read())
    for node in tree.body:
        if (isinstance(node, ast.Assign)
                and any(getattr(t, "id", "") == name for t in node.targets)):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} nicht in app.py gefunden")


ENV = Environment(autoescape=True)
BODY = ENV.from_string(template("INSTANCE_EDIT_BODY"))

NOW = datetime.datetime.now(datetime.timezone.utc)


def stamp(days_ago):
    return (NOW - datetime.timedelta(days=days_ago)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


PROD = {"app_id": "crm", "app_name": "CRM", "version": "1.4.0",
        "channel": "production", "tenant": "t1", "name": "crm",
        "routes": [{"path": "/", "roles": ["keyuser"]}],
        "services": [{"service": "", "port": 8000}], "storage": [],
        "config": [], "roles": ["keyuser"], "visibility": {}}

VIEW = {
    "schema": "0.1", "written": stamp(0), "free_kbytes": 20 * 1024 * 1024,
    "default_days": 7, "step_days": 7,
    "archives": [
        {"file": "oaap-backup-node-20260908-020011.tar.gz",
         "created": stamp(0), "bytes": 900 * 1024 * 1024},
        {"file": "oaap-backup-node-20260907-020011.tar.gz",
         "created": stamp(1), "bytes": 890 * 1024 * 1024},
    ],
    "instances": {"crm": {"kbytes": 900 * 1024,
                          "code": [{"key": "crm-test", "name": "crm-test",
                                    "version": "1.5.0", "ready": True}]}},
}

print("")
print("Regel 1 — der Knopf steht an der Produktiv-Instanz")

offer = iv.rehearsal_offer("crm", PROD, VIEW, "crm")
ok("die Produktiv-Instanz bekommt das Angebot", offer is not None)
ok("die Testinstanz nicht",
   iv.rehearsal_offer("crm-test", dict(PROD, channel="test"), VIEW) is None,
   "die hat schon Testdaten — dafuer ist sie da")
ok("und eine Generalprobe erst recht nicht",
   iv.rehearsal_offer("crm-probe",
                      dict(PROD, rehearsal={"of": "crm"}), VIEW) is None,
   "eine Kopie einer Kopie beantwortet keine Frage")
ok("der Namensvorschlag haengt am Namen des Mandanten, nicht am Schluessel",
   iv.rehearsal_offer("cls-crm", PROD, VIEW, "crm")["suggestion"]
   == "crm-probe",
   "das Kuerzel setzt der Knoten selbst davor (RFC-0025 8.1)")

print("")
print("Regel 2 — der Satz mit den Zahlen dieses Knotens")

ok("die Groesse der Kopie steht da", offer["size"] == "900.0 MB", offer)
ok("und was danach frei ist",
   offer["free"] == "20480.0 MB" and offer["after"] == "19580.0 MB", offer)
ok("mit dem Datum der Messung", offer["measured"][:10] == stamp(0)[:10], offer)
ok("bei genug Platz ist es kein Alarm", not offer["tight"])

# Am laufenden Knoten aufgefallen: eine kleine Instanz stand mit
# „belegt etwa 0.0 MB" auf der Seite. Eine kleine Unwahrheit ueber
# etwas, das nicht leer ist -- und genau diese Zahl soll der Betreiber
# glauben koennen.
klein = dict(VIEW, instances={"crm": {"kbytes": 24, "code": []}})
ok("etwas Kleines heisst nicht '0.0 MB'",
   iv.rehearsal_offer("crm", PROD, klein, "crm")["size"] == "24 KB",
   iv.rehearsal_offer("crm", PROD, klein, "crm")["size"])
ok("und etwas sehr Kleines nennt Bytes", iv._mb(400) == "400 Byte", iv._mb(400))
ok("ab einem Megabyte wieder MB", iv._mb(2 * 1024 * 1024) == "2.0 MB")

eng = dict(VIEW, free_kbytes=1000 * 1024)     # 1000 MB frei, 900 MB noetig
tight = iv.rehearsal_offer("crm", PROD, eng, "crm")
ok("bei zu wenig Platz sagt die Seite es vorher", tight["tight"],
   "der Knoten lehnt bei weniger als 20 % Luft ab — beide reden ueber "
   "dieselbe Zahl")

print("")
print("Regel 2b — ein Knoten ohne Messung leiht sich keine Zahl")

leer = iv.rehearsal_offer("crm", PROD, {}, "crm")
ok("ohne Ansicht gibt es keine Groesse", leer["size"] == "", leer)
ok("und auch keine erfundene Restmenge", leer["after"] == "", leer)
ok("und kein Archiv", leer["newest"] == "" and not leer["archives"], leer)
ok("das Angebot verschwindet aber nicht", leer is not None)

print("")
print("Regel 4 — das Alter des Archivs steht dabei")

ok("ein frisches Archiv heisst 'heute'", offer["newest_age"] == "heute",
   offer)
alt = dict(VIEW, archives=[{"file": "a.tar.gz", "created": stamp(14),
                            "bytes": 1}])
ok("ein zwei Wochen altes sagt sein Alter",
   iv.rehearsal_offer("crm", PROD, alt, "crm")["newest_age"] == "14 Tage alt")
ok("ein Tag ist Einzahl", iv.age_phrase(stamp(1)) == "1 Tag alt")
ok("ein unlesbarer Stempel erfindet kein Alter",
   iv.age_phrase("irgendwann") == "",
   "lieber nichts sagen als etwas Falsches")

print("")
print("Die Karte auf der Seite")

html = BODY.render(
    i=dict(PROD, key="crm", name="crm", app_id="crm", channel_label="Produktiv",
           is_test=False, description="", address_url="", address_host="",
           auto_address="", auto_suffix="", rename_grace=30,
           visibility_label="alle", tile_visible=True, tile_mode="auto",
           tile_reason="", source_label="Paket", source_lines=[],
           route_rows=[], services=[{"service": "", "port": 8000}],
           storage=[], groups=[], config=[], token_created="",
           artifacts=[], deploy_now=None, deploy_limit=20, promote=None,
           pending=None, hook_url="", address="", aliases=[],
           has_public_route=False, links=[], link_candidates=[],
           endpoints=[], node_exposed=False, can_export=True,
           throttle=None, throttle_limit=300, throttle_window=60,
           throttle_on=True, rehearsal=None, rehearsal_note="",
           rehearse=offer),
    tabs=iv.TABS, tab="deployment", purge_wanted=False, rename_wanted="",
    msg=None, error=None)

ok("die Karte steht auf der Seite", "Generalprobe anlegen" in html)
ok("mit dem Formular an der richtigen Stelle",
   'action="/instances/crm/rehearse"' in html)
ok("das Formular traegt seinen Reiter mit",
   re.search(r'action="/instances/crm/rehearse"[^>]*>\s*<input type="hidden" '
             r'name="tab" value="deployment">', html) is not None,
   "sonst fuehrt das Speichern woanders hin (Design-Guidelines 6.2.2)")
ok("es gibt kein Feld fuer einen Pfad",
   'name="archive"' not in html and 'name="path"' not in html,
   "die Auswahl bietet nur, was da ist — getippt wird kein Pfad")
ok("die Testinstanz wird zur Auswahl angeboten", "crm-test" in html)
ok("der Platzsatz steht auf der Seite",
   "900.0 MB" in html and "19580.0 MB" in html, html[:400])
ok("das Alter des Archivs auch", "heute" in html)
flat = " ".join(html.split())
ok("und was die Generalprobe NICHT bekommt",
   "keine eigene Adresse" in flat and "keine Geheimnisse" in flat, flat[-900:])
ok("dass eine App ohne ihr Geheimnis nicht startet, ist das richtige "
   "Ergebnis", "richtige Ergebnis" in html)
ok("und dass sie mitsamt Daten geloescht wird",
   "mitsamt ihren Daten" in html)

print("")
print("Ohne Testinstanz und ohne Sicherung sagt die Karte, warum")

ohne_code = iv.rehearsal_offer(
    "crm", PROD, dict(VIEW, instances={"crm": {"kbytes": 1, "code": []}}), "crm")
html2 = BODY.render(
    i=dict(PROD, key="crm", name="crm", app_id="crm", channel_label="Produktiv",
           is_test=False, description="", address_url="", address_host="",
           auto_address="", auto_suffix="", rename_grace=30,
           visibility_label="alle", tile_visible=True, tile_mode="auto",
           tile_reason="", source_label="Paket", source_lines=[],
           route_rows=[], services=[{"service": "", "port": 8000}],
           storage=[], groups=[], config=[], token_created="",
           artifacts=[], deploy_now=None, deploy_limit=20, promote=None,
           pending=None, hook_url="", address="", aliases=[],
           has_public_route=False, links=[], link_candidates=[],
           endpoints=[], node_exposed=False, can_export=True,
           throttle=None, throttle_limit=300, throttle_window=60,
           throttle_on=True, rehearsal=None, rehearsal_note="",
           rehearse=ohne_code),
    tabs=iv.TABS, tab="deployment", purge_wanted=False, rename_wanted="",
    msg=None, error=None)
ok("ohne Testinstanz kein Formular",
   'action="/instances/crm/rehearse"' not in html2)
ok("aber die Begruendung", "keine\n     Test-Instanz" in html2
   or "keine Test-Instanz" in html2.replace("\n     ", " "), )

ohne_archiv = iv.rehearsal_offer("crm", PROD, dict(VIEW, archives=[]), "crm")
html3 = BODY.render(
    i=dict(PROD, key="crm", name="crm", app_id="crm", channel_label="Produktiv",
           is_test=False, description="", address_url="", address_host="",
           auto_address="", auto_suffix="", rename_grace=30,
           visibility_label="alle", tile_visible=True, tile_mode="auto",
           tile_reason="", source_label="Paket", source_lines=[],
           route_rows=[], services=[{"service": "", "port": 8000}],
           storage=[], groups=[], config=[], token_created="",
           artifacts=[], deploy_now=None, deploy_limit=20, promote=None,
           pending=None, hook_url="", address="", aliases=[],
           has_public_route=False, links=[], link_candidates=[],
           endpoints=[], node_exposed=False, can_export=True,
           throttle=None, throttle_limit=300, throttle_window=60,
           throttle_on=True, rehearsal=None, rehearsal_note="",
           rehearse=ohne_archiv),
    tabs=iv.TABS, tab="deployment", purge_wanted=False, rename_wanted="",
    msg=None, error=None)
ok("ohne Sicherung kein Formular",
   'action="/instances/crm/rehearse"' not in html3)
ok("und der Satz, der es erklaert",
   "keine Sicherung" in html3.replace("\n     ", " "))

print("")
print("Auf der Generalprobe selbst: Restzeit und Verlaengern")

probe = dict(PROD, rehearsal={
    "of": "crm", "code_from": "crm-test",
    "archive": "/var/backups/oaap/oaap-backup-x.tar.gz",
    "archive_created": stamp(1), "created": stamp(0),
    "expires": (NOW + datetime.timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "extensions": 2})
rv = iv.rehearsal_view(probe)
html4 = BODY.render(
    i=dict(probe, key="crm-probe", name="crm-probe", app_id="crm",
           channel_label="Produktiv", is_test=False, description="",
           address_url="", address_host="", auto_address="", auto_suffix="",
           rename_grace=30, visibility_label="alle", tile_visible=True,
           tile_mode="auto", tile_reason="", source_label="Paket",
           source_lines=[], route_rows=[],
           services=[{"service": "", "port": 8000}], storage=[], groups=[],
           config=[], token_created="", artifacts=[], deploy_now=None,
           deploy_limit=20, promote=None, pending=None, hook_url="",
           address="", aliases=[], has_public_route=False, links=[],
           link_candidates=[], endpoints=[], node_exposed=False,
           can_export=True, throttle=None, throttle_limit=300,
           throttle_window=60, throttle_on=True,
           rehearsal=rv, rehearsal_note=iv.rehearsal_note(rv),
           rehearse=None),
    tabs=iv.TABS, tab="deployment", purge_wanted=False, rename_wanted="",
    msg=None, error=None)

ok("das Abzeichen steht im Objektkopf", "Generalprobe" in html4)
ok("die Restlaufzeit daneben", "noch 6 Tage" in html4 or "noch 7 Tage" in html4,
   rv["left"])
ok("die Karte nennt Daten- und Codeherkunft",
   "crm-test" in html4 and "oaap-backup-x.tar.gz" in html4)
ok("und wann das Archiv geschrieben wurde", stamp(1)[:10] in html4)
ok("die Zahl der Verlaengerungen steht da", "2" in html4 and "Verl" in html4)
ok("es gibt einen Knopf zum Verlaengern",
   'action="/instances/crm-probe/rehearsal-extend"' in html4)
ok("und kein Angebot, eine Generalprobe der Generalprobe zu bauen",
   'action="/instances/crm-probe/rehearse"' not in html4)
ok("die Karte sagt, dass daraus nicht uebernommen wird",
   "Urteil, keine Quelle" in html4.replace("\n     ", " ")
   or "Urteil" in html4)

print("")
print("Regel 3 — die Seite liest den Mandantenbaum nicht")

src = io.open(APP_PY, encoding="utf-8").read()
ok("die Ansicht liegt neben der Registry",
   '"/apps-registry/rehearsal-options.json"' in src)
COMPOSE = os.path.join(HERE, "..", "platform", "docker-compose.yml")
compose = io.open(COMPOSE, encoding="utf-8").read()
lines, portal, mounts = compose.splitlines(), False, []
for line in lines:
    if line.startswith("  ") and line.rstrip().endswith(":") and not line.startswith("    "):
        portal = line.strip() == "portal:"
    elif portal and line.strip().startswith('- "${OAAP_DATA_DIR}'):
        mounts.append(line.strip())
ok("der Portal-Block ist ueberhaupt gefunden worden", bool(mounts))
ok("und das Portal hat ueberhaupt keinen Mount in den Mandantenbaum",
   all("/tenants" not in mnt for mnt in mounts), mounts)
ok("die Ansicht liegt in einem Mount, den es hat",
   any(mnt.startswith('- "${OAAP_DATA_DIR}/apps:') for mnt in mounts), mounts)

print("")
print("ALLE PRUEFUNGEN BESTANDEN" if not fails else f"{fails} FEHLER")
sys.exit(1 if fails else 0)
