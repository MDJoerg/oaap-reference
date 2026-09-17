#!/usr/bin/env python3
"""Instanz-Diagnose: Zustand, Fenster, Gateway-Sicht, Neustart (RFC-0038).

Der Anlass, 2026-09-15: Joerg suchte einen CORS-Fehler und war „etwas
mittellos". Das Portal wusste, ob eine App laeuft -- und sonst nichts.
Warum sie abstuerzt, stand in `docker logs` auf der Maschine; ob die
Anfrage des Browsers ueberhaupt ankam, stand nirgends.

Was diese Datei festhaelt, sind die fuenf Entscheidungen und **die
Grenzen zwischen ihnen** -- denn die Reihenfolge nach Empfindlichkeit
ist die ganze Gestaltung:

  D1  Zustand IMMER sichtbar. Tatsachen ueber einen Container, nie
      Inhalte der App. Und: eine fehlende Ansicht heisst „unbekannt",
      niemals „laeuft nicht".
  D2  Das Log nur bei ausdruecklich geoeffnetem Fenster, 15/30/60
      Minuten, im Mandantenprotokoll. Kein Verlaengern.
  D3  Die Gateway-Sicht NUR solange das Fenster offen ist -- und nie
      Query-Parameter, nie der Wert von Authorization, nie Cookies, nie
      die Identitaets-Kopfzeilen.
  D4  Neustart = neu ERZEUGEN, derselbe Weg wie das Speichern der
      Konfiguration. Abgelehnt, solange ein Deployment laeuft.
  D5  Container-Logs sind begrenzt -- ueberall, wo ein Container
      entsteht.

Die empfindlichste Regel ist D3, und sie wird hier an dem geprueft, was
das Gateway TATSAECHLICH bekommt: am erzeugten Caddy-Text.

Geprueft ueber die echte Grenze: appctl schreibt in ein
Wegwerf-Datenverzeichnis, der echte Portal-Code liest dieselben
Dateien. Braucht PyYAML (appctl) und jinja2, kein Docker, keinen Knoten.

Aufruf: python3 test/test_diagnose.py
"""
import ast
import importlib
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
PLATFORM = os.path.join(HERE, "..", "platform")
PORTAL = os.path.join(PLATFORM, "services", "portal")
sys.path.insert(0, PLATFORM)

try:
    from jinja2 import Environment
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


DATA = tempfile.mkdtemp(prefix="oaap-diagnose-test-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.modules.pop("appctl", None)
a = importlib.import_module("appctl")
sys.path.insert(0, PORTAL)
sys.modules.pop("diagnose_view", None)
dv = importlib.import_module("diagnose_view")

APPCTL = open(os.path.join(PLATFORM, "appctl.py"), encoding="utf-8").read()
SRC = open(os.path.join(PORTAL, "app.py"), encoding="utf-8").read()
COMPOSE = open(os.path.join(PLATFORM, "docker-compose.yml"),
               encoding="utf-8").read()
MIGRATE = open(os.path.join(PLATFORM, "migrate.sh"), encoding="utf-8").read()
INSTALL = open(os.path.join(PLATFORM, "..", "install.sh"),
               encoding="utf-8").read()
OAAP_BIN = open(os.path.join(PLATFORM, "..", "bin", "oaap"),
                encoding="utf-8").read()

KEY = "cls-gliss-viewer-test"
tid = a.ensure_default_tenant()
reg = a.load_registry()
reg["instances"][KEY] = {
    "app_id": "gliss-viewer", "app_name": "GLISS Viewer", "channel": "test",
    "tenant": tid, "id": "bbbbbbbbbbbb", "version": "0.1.47",
    "name": "gliss-viewer-test", "port": 8123, "svc_port": 8000,
    "container": "oaap-app-cls-gliss-viewer-test",
    "image": "oaap-app/gliss-viewer:0.1.47",
    "routes": [{"path": "/admin", "roles": ["admin"]},
               {"path": "/", "roles": ["admin", "user"]}],
    "storage": [{"name": "data", "mount": "/data"}],
}
a.save_registry(reg)
INST = reg["instances"][KEY]


# ---------------------------------------------------------------- D5
print("")
print("D5 — ein Container-Log darf nicht unbegrenzt wachsen")

ok("die Grenze steht an der EINEN Stelle, an der Container entstehen",
   "*CONTAINER_LOG_OPTS," in APPCTL
   and APPCTL.count("CONTAINER_LOG_OPTS = ") == 1,
   "sonst waere sie eine Absicht und keine Eigenschaft")
ok("sie ist eine Groesse und eine Anzahl",
   "max-size=10m" in APPCTL and "max-file=3" in APPCTL)
ok("die Kerndienste bekommen dieselbe Grenze",
   COMPOSE.count('max-size: "10m"') == COMPOSE.count("restart: unless-stopped")
   and COMPOSE.count('max-size: "10m"') >= 3,
   "Compose kennt keine Vorgabe -- jeder Dienst muss es selbst sagen")
ok("NICHT global in einer daemon.json",
   "daemon.json" not in INSTALL and "daemon.json" not in MIGRATE,
   "ein Knoten darf Container tragen, die nicht OAAP gehoeren -- fuer "
   "die entscheidet ihr Besitzer")


# ---------------------------------------------------------------- D1
print("")
print("D1 — Zustand: Tatsachen ueber einen Container, immer sichtbar")

now = datetime(2026, 9, 17, 12, 0, 0, tzinfo=timezone.utc)


def view(**over):
    base = {"state": "running", "started": "2026-09-17T11:58:00Z",
            "restarts": 0, "exit_code": 0, "oom": False, "health": ""}
    base.update(over)
    return {"services": [dict(base, container=INST["container"])]}


rows = dv.state_rows(INST, view(), now)
ok("laufend heisst laufend, mit Alter",
   rows[0]["label"] == "läuft" and rows[0]["since"] == "2 Minuten", rows[0])
ok("und gilt als in Ordnung", rows[0]["level"] == "ok")

rows = dv.state_rows(INST, None, now)
ok("OHNE Ansicht heisst es unbekannt, nicht 'laeuft nicht'",
   rows[0]["label"] == "unbekannt" and rows[0]["level"] == "unknown"
   and not rows[0]["known"],
   "eine Seite darf keine Stoerung behaupten, nur weil sie nichts las")
ok("und die Karte sagt genau das",
   "sagt <em>nichts</em> darüber" in SRC)

rows = dv.state_rows(INST, view(state="exited", exit_code=137), now)
ok("ein beendeter Container nennt seinen Exit-Code",
   rows[0]["level"] == "err" and rows[0]["exit_code"] == 137)

loop = dv.state_rows(INST, view(restarts=3), now)
ok("Zaehler ueber null UND gerade gestartet = Neustartschleife",
   loop[0]["looping"],
   "aus EINER Momentaufnahme ableitbar -- eine zweite gibt es nicht")
ok("der Befund steht als Satz da, mit Verweis auf das Fenster",
   "startet vermutlich immer wieder neu" in dv.state_warning(loop)
   and "Diagnose-Fenster" in dv.state_warning(loop),
   dv.state_warning(loop))
old = dv.state_rows(INST, view(restarts=3, started="2026-09-17T09:00:00Z"),
                    now)
ok("drei Neustarts von heute morgen sind KEIN Befund",
   not old[0]["looping"] and "immer wieder" not in dv.state_warning(old),
   "sonst warnt die Seite ewig wegen eines Deployments von gestern")
ok("Speichermangel wird benannt",
   "OOM" in dv.state_warning(dv.state_rows(INST, view(oom=True), now)))
ok("ein gesunder Zustand erzeugt keinen Satz",
   dv.state_warning(dv.state_rows(INST, view(), now)) == "")

ok("die Warnung steht UEBER den Reitern, nicht in einem",
   SRC.index("{% if i.diag and i.diag.warning %}")
   < SRC.index('<nav class="tabs">'),
   "ein Container in der Schleife darf nicht in einem Reiter verborgen "
   "sein, den niemand oeffnet")
ok("der Zustand steht im Objektkopf", '<span class="k">Zustand</span>' in SRC)

a.state_view_write()
with open(a.STATE_VIEW, encoding="utf-8") as f:
    written = json.load(f)
ok("der Host schreibt die Ansicht, wo das Portal liest",
   KEY in (written.get("instances") or {}), written)
ok("das Portal liest genau diese Datei",
   'STATE_VIEW = "/apps-registry/instance-state.json"' in SRC)
ok("und sie sagt, ob die Laufzeit ueberhaupt antwortete",
   "runtime_ok" in written,
   "'kein Container' und 'kein Docker' sind zwei verschiedene Antworten")

reg2 = a.load_registry()
reg2["instances"]["halbfertig"] = {"app_id": "x", "channel": "test"}
a.save_registry(reg2)
a.state_view_write()
with open(a.STATE_VIEW, encoding="utf-8") as f:
    written2 = json.load(f)
ok("ein schraeger Datensatz kostet nicht die Ansicht aller anderen",
   KEY in (written2.get("instances") or {}),
   "dieselbe Regel wie bei config_view_write")
del reg2["instances"]["halbfertig"]
a.save_registry(reg2)
a.state_view_write()

ok("die Ansicht haengt an einem Timer, nicht am Speichern der Registry",
   "state-index" in MIGRATE and "state-index" in INSTALL
   and "state_view_write(reg)" not in APPCTL.split("def save_registry")[1][:400],
   "Container-Zustand aendert sich von selbst -- eine Ansicht, die nur "
   "beim Speichern frisch wird, ist frisch, wenn nichts passiert ist")
ok("und der Timer wird beim Entfernen der Plattform wieder abgebaut",
   "oaap-instance-watch" in OAAP_BIN,
   "ein zurueckgelassener Minuten-Timer ruft ein geloeschtes appctl.py")


# ---------------------------------------------------------------- D2
print("")
print("D2 — das Fenster: ausdruecklich, befristet, im Protokoll")

ok("drei Dauern, Vorgabe 30",
   a.DIAGNOSE_MINUTES == (15, 30, 60) and a.DIAGNOSE_DEFAULT_MINUTES == 30
   and dv.DURATIONS == a.DIAGNOSE_MINUTES,
   "Host und Portal duerfen hier nicht auseinanderlaufen")

refused = ""
try:
    a.diagnose_open(KEY, minutes=600)
except a.DiagnoseRefused as e:
    refused = str(e)
ok("eine Dauer, die niemand angeboten hat, wird am HOST abgelehnt",
   "15, 30, 60" in refused, refused or "sie wurde angenommen")
FLAT = " ".join(SRC.split())
ok("das Portal prueft sie auch, aber der Host ist die Grenze",
   "minutes not in dv.DURATIONS" in SRC
   and "Spool ist Daten, kein Vertrauen" in FLAT)

ok("ohne Fenster gibt es keine Aufnahme des App-Logs",
   not os.path.exists(a.diagnose_snapshot_file(KEY)))
snap_refused = ""
try:
    a.diagnose_logs_write(KEY)
except a.DiagnoseRefused as e:
    snap_refused = str(e)
ok("und der Versuch wird abgelehnt, nicht ignoriert",
   "no diagnosis window" in snap_refused, snap_refused)

# Das Fenster von Hand in die Registry, ohne Docker und ohne Gateway --
# geprueft wird die REGEL, nicht Caddy.
reg3 = a.load_registry()
open_until = (datetime.now(timezone.utc) + timedelta(minutes=30))
reg3["instances"][KEY]["diagnose"] = {
    "opened": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    "until": open_until.isoformat(timespec="seconds"),
    "minutes": 30, "by": "joerg"}
a.save_registry(reg3)
INST_OPEN = a.load_registry()["instances"][KEY]

ok("ein offenes Fenster ist offen", bool(a.diagnose_window(INST_OPEN)))
ok("und das Portal liest dieselbe Tatsache",
   bool(dv.window(INST_OPEN)) and dv.window(INST_OPEN)["by"] == "joerg")
ok("die Restzeit wird benannt, nicht die Endzeit allein",
   "Minuten" in dv.window(INST_OPEN)["left"])
ok("Verlaengern gibt es nicht", "Verlängern gibt es nicht" in SRC)

expired = dict(INST_OPEN)
expired["diagnose"] = dict(INST_OPEN["diagnose"], until=(
    datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat())
ok("ein abgelaufenes Fenster gilt SOFORT als zu, auch vor dem Sweep",
   a.diagnose_window(expired) is None and dv.window(expired) is None,
   "sonst waere die Zeitgrenze eine Anzeige und keine Zusage")
unreadable = dict(INST_OPEN)
unreadable["diagnose"] = {"until": "irgendwann"}
ok("ein unlesbares Datum gilt als abgelaufen",
   a.diagnose_window(unreadable) is None,
   "ein Fenster, das niemand datieren kann, darf nicht ewig offen sein")

ok("der Warnsatz ueber den Logs steht woertlich in D2",
   "Logs können vertrauliche Daten enthalten" in dv.LOG_WARNING
   and dv.LOG_WARNING in SRC.replace("{{ i.diag.log_warning }}",
                                     dv.LOG_WARNING))
ok("und der Satz, der die haeufigste Enttaeuschung verhindert",
   "ab jetzt" in dv.COLLECT_NOTE)
ok("Oeffnen, Schliessen und Ablauf stehen im Mandantenprotokoll",
   all(x in APPCTL for x in ('"diagnose.opened"', '"diagnose.closed"',
                             '"diagnose.expired"')))
ok("die INHALTE nie", "detail=f\"{int(minutes)} minutes\"" in APPCTL,
   "im Protokoll steht die Dauer, nicht das Gelesene")
ok("ein Sweep schliesst abgelaufene Fenster",
   "def diagnose_sweep" in APPCTL and "diagnose sweep" in MIGRATE
   and "diagnose sweep" in INSTALL)


# ---------------------------------------------------------------- D3
print("")
print("D3 — die Gateway-Sicht: nur bei offenem Fenster, und gefiltert")

site = "\n".join(a.site_body(INST["routes"], INST["container"],
                             INST["svc_port"], scope=KEY, tenant=tid))
ok("bei offenem Fenster protokolliert die Site diese Instanz",
   f"output file /logs/diagnose-{KEY}.log" in site)
ok("die Aufzeichnung ist begrenzt", "roll_size 2mib" in site
   and "roll_keep 1" in site,
   "eine Stunde Dauerfeuer darf kein Plattenproblem werden")

print("      -- und jetzt die vier MUSS-NICHT-Regeln, am erzeugten Text:")
ok("der Query-Teil wird weggeschnitten",
   'request>uri regexp "\\?.*$" ""' in site,
   "im Zugriffsprotokoll des Gateways stehen ganze URIs, und Token sind "
   "schon in Query-Strings gelandet")
ok("der WERT von Authorization wird ersetzt, nicht geloescht",
   "request>headers>Authorization replace REDACTED" in site,
   "D3 will wissen, OB ein Nachweis dabei war -- Loeschen wuerde genau "
   "die Tatsache wegnehmen, die 'kein Schluessel' von 'falscher "
   "Schluessel' unterscheidet")
ok("Cookies ebenso", "request>headers>Cookie replace REDACTED" in site)
ok("die Identitaets-Kopfzeilen werden geloescht",
   "request>headers>X-Oaap-User delete" in site
   and "request>headers>X-Oaap-Roles delete" in site)
ok("und ein Set-Cookie der App wird nicht aufgeschrieben",
   "resp_headers>Set-Cookie delete" in site)
ok("auch ein Location verliert seinen Query-Teil",
   'resp_headers>Location regexp "\\?.*$" ""' in site,
   "eine App darf auf eine URL mit Token umleiten -- diese Datei darf "
   "nicht die Stelle sein, die ihn aufschreibt")

reg4 = a.load_registry()
reg4["instances"][KEY].pop("diagnose")
a.save_registry(reg4)
closed_site = "\n".join(a.site_body(INST["routes"], INST["container"],
                                    INST["svc_port"], scope=KEY, tenant=tid))
ok("ohne Fenster protokolliert die Site NICHTS fuer diese Instanz",
   "diagnose-" not in closed_site,
   "dieses RFC schaltet kein Dauerprotokoll je Instanz ein")
_body = APPCTL.split("def site_body")[1].split("\ndef ")[0]
ok("die Regel haengt an EINER Stelle, die jede Site teilt",
   APPCTL.count("list(diagnose_site_lines(scope))") == 1
   and "list(diagnose_site_lines(scope))" in _body,
   "es gibt neun Stellen, an denen eine Site geschrieben wird -- ein "
   "Parameter durch neun Aufrufer scheitert still an dem einen, den "
   "niemand angepasst hat")

print("      -- und der Leser im Portal ist eine ERLAUBNISLISTE:")
line = json.dumps({
    "ts": 1789600000.5, "status": 401, "duration": 0.012,
    "resp_headers": {"Access-Control-Allow-Origin": ["http://localhost:8080"],
                     "Set-Cookie": ["sollte-nicht-hier-sein"],
                     "X-Ueberraschung": ["ein neues Feld von Caddy"]},
    "request": {"method": "GET", "uri": "/api/models?token=geheim",
                "host": "gliss-viewer-test.cls.oaap.joomp.de",
                "headers": {"Origin": ["http://localhost:8080"],
                            "Authorization": ["REDACTED"],
                            "X-Api-Key": ["nicht-auf-die-seite"]}}})
grows = dv.gateway_rows(line + "\nkaputte zeile\n")
ok("eine kaputte Zeile wird uebersprungen, nie fatal", len(grows) == 1)
r = grows[0]
ok("der Pfad wird auch beim LESEN vom Query befreit",
   r["path"] == "/api/models" and "geheim" not in json.dumps(r),
   "eine Datei aus einer Fassung vor dem Filter darf keinen Token auf "
   "die Seite bringen")
ok("ein Nachweis wird als ja/nein gemeldet, nie als Wert",
   r["credentials"] is True and "REDACTED" not in json.dumps(r["req_headers"]))
ok("nur die vereinbarten Kopfzeilen erscheinen",
   [n for n, _ in r["resp_headers"]] == ["Access-Control-Allow-Origin"],
   r["resp_headers"])
ok("ein Feld, das niemand vorgesehen hat, erreicht die Seite nicht",
   "Ueberraschung" not in json.dumps(r) and "X-Api-Key" not in json.dumps(r),
   "zwei Schichten mit umgekehrter Logik: das Gateway loescht, der "
   "Leser laesst nur zu")
ok("und die Seite sagt, wer geantwortet hat",
   r["verdict"] == "abgelehnt: kein oder kein gültiger Schlüssel", r["verdict"])

print("      -- die Deutung, genau ein Satz und nur wo eindeutig:")


def note(**over):
    e = {"ts": 1789600000.0, "status": 200, "duration": 0.01,
         "resp_headers": {}, "request": {"method": "GET", "uri": "/x",
                                         "headers": {"Origin": ["http://a"]}}}
    e["status"] = over.pop("status", e["status"])
    e["request"]["method"] = over.pop("method", e["request"]["method"])
    e["resp_headers"].update(over.pop("resp", {}))
    e["request"]["headers"].update(over.pop("req", {}))
    return dv.cors_note(dv.gateway_rows(json.dumps(e)))


ok("Vorab-Anfrage zur Anmeldung umgeleitet -> der Browser bricht ab",
   "bricht hier ab" in note(method="OPTIONS", status=303,
                            resp={"Location": ["/auth/login"]}))
# Gemessen auf oaap-test, 17.09.: Die Vorab-Anfrage ging am Gateway
# vorbei zur App, und die antwortete 501 -- sie kennt OPTIONS nicht. Im
# Browser heisst das „CORS-Fehler".
ok("Vorab-Anfrage von der App abgelehnt -> auch dann bricht er ab",
   "bricht hier ab" in note(method="OPTIONS", status=501)
   and "200 oder 204" in note(method="OPTIONS", status=501),
   note(method="OPTIONS", status=501))
ok("und die Zeile sagt es, statt 'von der App beantwortet'",
   dv.answered_by(501, "OPTIONS", "").startswith("Vorab-Anfrage abgelehnt"),
   "wahr und nutzlos ist die schlechtere Antwort")
ok("eigentliche Anfrage ohne Nachweis abgelehnt -> es fehlt ein Schluessel",
   "API-Schlüssel" in note(status=401), note(status=401))
ok("durchgelassen, aber ohne CORS-Kopfzeile -> das ist die App",
   "keine CORS-Kopfzeile" in note(status=200))
ok("Stern plus Anmeldedaten -> diese Kombination verbietet der Browser",
   "verbietet der Browser" in note(
       status=200, resp={"Access-Control-Allow-Origin": ["*"]},
       req={"Cookie": ["REDACTED"]}))
ok("gleiche Herkunft -> keine Deutung",
   dv.cors_note(dv.gateway_rows(json.dumps({
       "ts": 1789600000.0, "status": 200, "request": {
           "method": "GET", "uri": "/x", "headers": {}}}))) == "",
   "eine Seite, die zu raten anfaengt, schickt jemanden in die naechste "
   "falsche Richtung -- und genau davon handelt dieses RFC")


# ---------------------------------------------------------------- D4
print("")
print("D4 — Neustart heisst neu ERZEUGEN")

ok("es ist derselbe Aufruf wie beim Speichern der Konfiguration",
   "recreate_instance_containers(name, instance_services(inst),"
   in APPCTL.split("def restart_instance")[1][:1400]
   and "recreate_instance_containers(name, instance_services(inst),"
   in APPCTL.split("def apply_config")[1][:1400],
   "ein zweiter, leichterer Weg waere der, der auseinanderlaeuft")

os.makedirs(os.path.join(a.SPOOL_DIR, "queue"), exist_ok=True)
pend = os.path.join(a.SPOOL_DIR, "queue", "f" * 32 + ".json")
with open(pend, "w", encoding="utf-8") as f:
    json.dump({"id": "f" * 32, "instance": KEY, "action": "artifact"}, f)
ok("waehrend eines Deployments wird er abgelehnt",
   a.deployment_in_flight(KEY))
denied = ""
try:
    a.restart_instance(KEY, INST)
except a.DiagnoseRefused as e:
    denied = str(e)
ok("und zwar mit dem Satz, der sagt warum",
   "deployment of this instance" in denied, denied or "er lief einfach")
ok("die Seite sagt es auch, statt den Knopf still zu verschlucken",
   "läuft gerade ein Deployment" in SRC)
with open(pend, "w", encoding="utf-8") as f:
    json.dump({"id": "f" * 32, "instance": KEY, "action": "visibility"}, f)
ok("eine Sichtbarkeitsaenderung ist kein Deployment und blockiert nicht",
   not a.deployment_in_flight(KEY),
   "sonst waere jeder Spool-Eintrag ein Grund, nichts zu tun")
os.remove(pend)
ok("ein Neustart findet sich selbst nicht",
   not a.deployment_in_flight(KEY, own_rid="f" * 32))
ok("er steht im Mandantenprotokoll",
   '"instance.restarted"' in APPCTL)
ok("der Knopf steht NICHT im letzten Reiter",
   SRC.index('action="/instances/{{ i.key }}/restart"')
   < SRC.index("""<section class="panel {{ 'active' if tab == 'verwaltung' }}">"""),
   "dort steht ausschliesslich Unwiderrufliches, jedes mit dem heutigen "
   "Namen zu bestaetigen (Design-Guidelines 6.2.2) -- ein Neustart "
   "laesst Daten, Adresse, Version und Konfiguration unberuehrt")
ok("und die Folge steht am Knopf",
   "einige Sekunden nicht erreichbar" in SRC
   and "Daten, Adresse, Version und Konfiguration bleiben" in SRC)


# ------------------------------------------------------- Seite & CLI
print("")
print("Die Seite und die Kommandozeile")

tree = ast.parse(SRC)
BODY = next(ast.literal_eval(n.value) for n in tree.body
            if isinstance(n, ast.Assign)
            and any(getattr(t, "id", "") == "INSTANCE_EDIT_BODY"
                    for t in n.targets))
ENV = Environment(autoescape=True)
sys.modules.pop("instance_view", None)
iv = importlib.import_module("instance_view")
ok("es gibt einen eigenen Reiter Diagnose",
   "diagnose" in iv.TAB_KEYS
   and iv.TAB_KEYS.index("diagnose") == len(iv.TAB_KEYS) - 2,
   "vor der Verwaltung, weil dort das Unwiderrufliche liegt")


def render(diag, tab="diagnose"):
    return ENV.from_string(BODY).render(
        i={"key": KEY, "name": "gliss-viewer-test", "is_test": True,
           "config": [], "endpoints": [], "links": [], "link_candidates": [],
           "groups": [], "roles": [], "storage": [], "services": [],
           "route_rows": [], "source_lines": [], "aliases": [],
           "artifacts": [], "diag": diag},
        tabs=iv.TABS, tab=tab, msg=None, error=None)


base = {"state": dv.state_rows(INST, view(), now), "state_known": True,
        "state_written": "2026-09-17 12:00", "warning": "", "window": None,
        "durations": dv.DURATIONS, "default_minutes": dv.DEFAULT_MINUTES,
        "log_warning": dv.LOG_WARNING, "collect_note": dv.COLLECT_NOTE,
        "log": None, "gateway": [], "cors_note": "", "deploy_running": False}

zu = render(base)
ok("ohne Fenster erklaert der Reiter, was es zeigen WUERDE",
   "Diagnose-Fenster öffnen" in zu and "Sicht des Gateways" in zu,
   "ein leerer Abschnitt sagt, warum er leer ist")
ok("und der Zustand steht trotzdem da", "läuft" in zu)
ok("die Dauer ist eine Auswahl aus dreien", zu.count("<option") == 3)

offen = render(dict(base, window=dv.window(INST_OPEN),
                    gateway=grows, cors_note="Ein Satz.",
                    log={"written": "2026-09-17T12:01:00", "tail": 200,
                         "services": [{"service": "", "container": "c",
                                       "lines": ["zeile eins"]}]}))
ok("mit Fenster stehen Restzeit, Gateway-Sicht und Log da",
   "noch <strong>" in offen and "/api/models" in offen
   and "zeile eins" in offen)
ok("der Warnsatz steht ueber beidem",
   offen.index("vertrauliche Daten") < offen.index("/api/models"))
flat = " ".join(offen.split())
ok("die Seite sagt, was NIE aufgezeichnet wird",
   "Pfad <strong>ohne Query</strong>" in flat and "Cookies" in flat
   and "nur, <em>ob</em> ein Nachweis dabei war" in flat)
ok("und dass Schliessen loescht", "gelöscht" in offen)
stumm = dv.gateway_rows(json.dumps({
    "ts": 1789600000.0, "status": 200, "duration": 0.01, "resp_headers": {},
    "request": {"method": "GET", "uri": "/api/x",
                "headers": {"Origin": ["http://localhost:8080"]}}}))
ok("ein Aufruf ohne CORS-Kopfzeile wird in der Zeile markiert",
   "keine Access-Control-Allow-Origin in der Antwort" in " ".join(
       render(dict(base, window=dv.window(INST_OPEN),
                   gateway=stumm)).split()),
   "genau der Fall, den ein Browser als CORS-Fehler meldet")

ok("die Maschine kennt beide Befehle",
   "oaap app logs" in APPCTL.replace('"logs"', "oaap app logs")
   or 'sub.add_parser("logs"' in APPCTL)
ok("logs, restart und diagnose sind Unterbefehle",
   all(f'sub.add_parser("{c}"' in APPCTL
       for c in ("logs", "restart", "diagnose", "state-index")))
ok("und stehen in der Hilfe des Wrappers",
   "logs|restart|diagnose" in OAAP_BIN)
ok("an der Maschine braucht es kein Fenster",
   "whoever is at the machine" in APPCTL,
   "wer dort steht, hat Docker -- das Fenster ist fuer das PORTAL")

print("")
print(f"{'FEHLER' if fails else 'ALLE PRUEFUNGEN BESTANDEN'} — {fails} Fehlschlag(e)")
sys.exit(1 if fails else 0)
