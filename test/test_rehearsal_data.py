#!/usr/bin/env python3
"""Die Daten der Generalprobe und ihr Ablauf (RFC-0030 D1/D4, Spec 2.15).

Das einzige wirklich neue Stueck Mechanik: den Teilbaum **einer**
Instanz aus einem Sicherungsarchiv loesen und in eine **neue, leere**
Instanz legen. Drei Fallstricke haben in diesem Projekt schon einmal
Geld gekostet, und alle drei stehen hier als Pruefung:

* Die Pfade im Archiv tragen die **alte** Kennung. Wer sie legt, wie sie
  liegen, schreibt die Kopie dorthin, wo das Original hingehoert.
* Das Archiv wird mit `--numeric-owner` geschrieben; wer beim Entpacken
  Namen aufloest, gibt dem Container einen Mount, den er nicht mehr
  beschreiben kann — und nichts sagt es.
* Die `instance.env` liegt im Teilbaum **mit**. Die Geheimnisse kommen
  also auf den Knoten, ob jemand das wollte oder nicht; sie muessen
  **danach** entfernt werden, nicht vorher gehofft.

Dazu der Ablauf (D4) und die Sperre, die nicht fehlen darf: Der Sweep
fasst **nur** Instanzen mit `rehearsal`-Block an. Eine gewoehnliche
Instanz, die zufaellig ein `expires` im Datensatz traegt, muss ihn
ueberleben — eine automatische Loeschung, die eine gewoehnliche Instanz
erreichen kann, ist eine Falle mit Zeitschaltuhr.

Docker laeuft hier nicht: `install_artifact` wird beobachtet statt
ausgefuehrt (es hat seine eigenen Pruefungen). Alles davor und alles
danach ist echt — dasselbe `tar`, dieselbe `instance.env`, dieselbe
Registry.

Aufruf: python3 test/test_rehearsal_data.py
"""
import argparse
import contextlib
import datetime
import io as _io
import os
import subprocess as real_subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-rehearsal-data-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, os.path.join(HERE, "..", "platform"))

import appctl as m                                            # noqa: E402

m.reload_gateway = lambda: None
m.remove_app_network = lambda name: None
m.refresh_generated_sites = lambda: []
os.makedirs(m.CADDY_APPS_DIR, exist_ok=True)


class _NoDocker:
    """subprocess, aber ohne Docker.

    `tar` und `du` laufen echt — sie sind das, was hier geprueft wird.
    Ein `docker`-Aufruf wird geschluckt, damit die Aufraeumwege
    (`remove_instance`) ohne Container-Laufzeit durchlaufen.
    """
    CalledProcessError = real_subprocess.CalledProcessError
    TimeoutExpired = real_subprocess.TimeoutExpired
    PIPE = real_subprocess.PIPE
    DEVNULL = real_subprocess.DEVNULL

    calls = []

    @staticmethod
    def run(cmd, *a, **kw):
        _NoDocker.calls.append(list(cmd))
        if cmd and str(cmd[0]).endswith("docker"):
            return real_subprocess.CompletedProcess(cmd, 0, "", "")
        return real_subprocess.run(cmd, *a, **kw)


m.subprocess = _NoDocker

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:500]}")


def refused(fn, *a, **kw):
    try:
        with contextlib.redirect_stdout(_io.StringIO()):
            fn(*a, **kw)
    except m.RehearsalRefused as e:
        return str(e)
    return ""


def record(**over):
    base = {
        "app_id": "crm", "app_name": "CRM", "version": "1.4.0",
        "channel": "production", "port": 8101, "container": "oaap-app-crm",
        "image": "oaap-app/crm:1.4.0", "svc_port": 8000,
        "services": [{"service": "", "container": "oaap-app-crm",
                      "image": "oaap-app/crm:1.4.0", "build": "", "port": 8000}],
        "routes": [{"path": "/", "roles": ["keyuser"]},
                   {"path": "/hook", "roles": ["public"]}],
        "storage": [{"name": "data", "mount": "/data"}],
        "config": [{"key": "SMTP_PASSWORD", "label": "Mailpasswort",
                    "secret": True, "multiline": False, "generate": "",
                    "default": ""},
                   {"key": "SMTP_HOST", "label": "Mailserver", "secret": False,
                    "multiline": False, "generate": "", "default": ""}],
        "roles": ["keyuser"], "visibility": {},
        "source": {"kind": "artifact", "version": "1.4.0",
                   "stored": "1.4.0-a.zip", "sha256": "a" * 64, "path": ""},
    }
    base.update(over)
    return base


print("")
print("Ein Knoten mit Produktion, Teststand und einem Archiv")

tid = m.ensure_default_tenant()
reg = m.load_registry()
reg["instances"]["crm"] = record(tenant=tid, id="aaaaaaaaaaaa", name="crm",
                                 address="crm.example.org", links=["lager"])
reg["instances"]["crm-test"] = record(
    tenant=tid, id="cccccccccccc", name="crm-test", channel="test",
    port=8103, version="1.5.0", container="oaap-app-crm-test",
    services=[{"service": "", "container": "oaap-app-crm-test",
               "image": "oaap-app/crm:1.5.0", "build": "", "port": 8000}],
    source={"kind": "artifact", "version": "1.5.0", "stored": "1.5.0-b.zip",
            "sha256": "b" * 64, "path": ""})
m.save_registry(reg)

# Die Produktivdaten, wie sie auf einem Knoten liegen: Nutzdaten im
# storage/, die Geheimnisse daneben in der instance.env.
prod_dir = m.instance_dir("crm", reg["instances"]["crm"])
os.makedirs(os.path.join(prod_dir, "storage", "data"), exist_ok=True)
with open(os.path.join(prod_dir, "storage", "data", "kunden.db"), "w",
          encoding="utf-8") as f:
    f.write("40000 gewachsene Zeilen\n")
m.save_env("crm", {"OAAP_APP_SECRET": "das-echte-geheimnis",
                   "SMTP_PASSWORD": "streng-geheim",
                   "SMTP_HOST": "mail.example.org"},
           reg["instances"]["crm"])

# Das zurueckgehaltene Paket des Teststands (Inhalt egal — installiert
# wird hier nicht, geprueft wird, DASS genau diese Bytes genommen werden)
pkgdir = m.artifact_dir("crm-test", reg["instances"]["crm-test"])
os.makedirs(pkgdir, exist_ok=True)
PKG = os.path.join(pkgdir, "1.5.0-b.zip")
with open(PKG, "wb") as f:
    f.write(b"PK\x03\x04 nicht wirklich ein zip")

BACKUPS = tempfile.mkdtemp(prefix="oaap-archives-")


def make_archive(name, members):
    path = os.path.join(BACKUPS, name)
    real_subprocess.run(["tar", "--numeric-owner", "-czf", path,
                         "-C", DATA, *members], check=True,
                        capture_output=True)
    return path


ARCHIVE = make_archive("oaap-backup-node-20260906-020011.tar.gz",
                       [m.archive_member(reg, "crm")])
m.backup_archive_dir = lambda: BACKUPS

ok("das Archiv traegt den Teilbaum der Produktiv-Instanz",
   m.archive_holds(ARCHIVE, m.archive_member(reg, "crm")))
ok("und der Teilbaum haengt an der Kennung, nicht am Namen",
   m.archive_member(reg, "crm") == f"tenants/{tid}/instances/aaaaaaaaaaaa",
   m.archive_member(reg, "crm"))
ok("das neueste Archiv wird gefunden", m.newest_archive() == ARCHIVE,
   m.newest_archive())

print("")
print("Was vorher laut abgelehnt wird")

reg = m.load_registry()
err = refused(m.rehearsal_review, reg, "gibt-es-nicht", "probe")
ok("eine unbekannte Instanz", "no instance named" in err, err)
err = refused(m.rehearsal_review, reg, "crm-test", "probe")
ok("eine Testinstanz als Quelle — dafuer gibt es schon eine Testinstanz",
   "not a production instance" in err, err)
err = refused(m.rehearsal_review, reg, "crm", "crm")
ok("ein Name, den es schon gibt", "already exists" in err, err)
err = refused(m.rehearsal_review, reg, "crm", "Probe!")
ok("ein unmoeglicher Name", "lowercase" in err, err)
err = refused(m.rehearsal_review, reg, "crm", "probe", code_from="lager-test")
ok("eine Codequelle, die es nicht gibt", "--code-from" in err, err)
err = refused(m.rehearsal_review, reg, "crm", "probe", code_from="crm")
ok("eine Codequelle, die keine Testinstanz ist",
   "not a test instance" in err, err)

leer = tempfile.mkdtemp(prefix="oaap-empty-archives-")
m.backup_archive_dir = lambda: leer
err = refused(m.rehearsal_review, reg, "crm", "probe")
ok("kein Archiv — ohne Sicherung keine Generalprobe",
   "no backup archive" in err and "oaap backup create" in err, err)
ok("und das ist eine brauchbare Nachricht, keine Verlegenheit",
   "RFC-0029" in err, err)
m.backup_archive_dir = lambda: BACKUPS

# Ein Archiv, das die Instanz NICHT enthaelt: der Befund von 2026-09-05,
# nur diesmal faellt er auf, bevor jemand ihn nachts braucht.
OHNE = make_archive("oaap-backup-node-20260901-020011.tar.gz", ["apps"])
# Aelter datiert, damit `newest_archive` weiter das vollstaendige nimmt:
# gewaehlt wird nach Zeit, nicht nach Namen.
os.utime(OHNE, (os.path.getmtime(ARCHIVE) - 5 * 86400,) * 2)
ok("das neueste bleibt das vollstaendige", m.newest_archive() == ARCHIVE,
   m.newest_archive())
err = refused(m.rehearsal_review, reg, "crm", "probe", archive=OHNE)
ok("ein Archiv ohne die Daten dieser Instanz",
   "holds no data" in err, err)
ok("und die Ablehnung sagt, dass das ein Befund ueber die SICHERUNG ist",
   "BACKUP" in err, err)

print("")
print("Der Plan, bevor irgendetwas geschrieben wird")

plan = m.rehearsal_review(reg, "crm", "probe")
ok("er nennt die Testinstanz als Codequelle", plan["code_key"] == "crm-test")
ok("und genau deren zurueckgehaltenes Paket", plan["package"] == PKG, plan["package"])
ok("er nennt das Archiv und sein Alter",
   plan["archive"] == ARCHIVE and plan["archive_age_days"] == 0, plan)
ok("er nennt den Platzbedarf", plan["kbytes"] >= 0 and plan["free_kbytes"] > 0)
ok("sieben Tage sind die Voreinstellung", plan["days"] == 7)
ok("und die Pruefung hat nichts geschrieben",
   "probe" not in m.load_registry()["instances"])

err = refused(m.rehearsal_review, reg, "crm", "probe", days=200)
ok("eine unsinnige Laufzeit wird abgelehnt", "1 and 90 days" in err, err)

print("")
print("Die Kopie: neue Kennung, altes Original unberuehrt")

seen = {}


def fake_install(name, zip_path, grant, channel="test", path="", origin="",
                 permit=None, ident=None, rehearsal=None):
    seen.update(name=name, zip_path=zip_path, channel=channel, permit=permit,
                ident=ident, rehearsal=rehearsal)
    r = m.load_registry()
    r["instances"][name] = record(
        tenant=ident["tenant"], id=ident["id"], name=permit["name"],
        version="1.5.0", port=8104, container=f"oaap-app-{name}",
        services=[{"service": "", "container": f"oaap-app-{name}",
                   "image": "oaap-app/crm:1.5.0", "build": "", "port": 8000}],
        rehearsal=rehearsal)
    m.save_registry(r)
    return "1.5.0", "c" * 64


real_install = m.install_artifact
m.install_artifact = fake_install

_NoDocker.calls = []
plan = m.rehearsal_review(m.load_registry(), "crm", "probe")
key, dropped = m.create_rehearsal(plan)

new_inst = m.load_registry()["instances"][key]
new_dir = m.instance_dir(key, new_inst)
ok("die Daten liegen unter der NEUEN Kennung",
   os.path.isfile(os.path.join(new_dir, "storage", "data", "kunden.db")),
   new_dir)
ok("und die neue Kennung ist eine andere als die alte",
   new_inst["id"] != "aaaaaaaaaaaa", new_inst["id"])
ok("es sind dieselben Daten",
   open(os.path.join(new_dir, "storage", "data", "kunden.db"),
        encoding="utf-8").read().strip() == "40000 gewachsene Zeilen")
ok("das Original ist unberuehrt",
   os.path.isfile(os.path.join(prod_dir, "storage", "data", "kunden.db")))
ok("und sein instance.env ebenfalls",
   m.load_env("crm", m.load_registry()["instances"]["crm"]).get("SMTP_PASSWORD")
   == "streng-geheim")

tarcall = next((c for c in _NoDocker.calls if c and c[0] == "tar"
                and "-xpzf" in c), [])
ok("entpackt wird mit --numeric-owner",
   "--numeric-owner" in tarcall,
   "sonst kann der Container seinen eigenen Mount nicht mehr beschreiben")
ok("und mit --strip-components, statt die alten Pfade zu legen",
   any(str(x).startswith("--strip-components=") for x in tarcall), tarcall)

print("")
print("Die Geheimnisse kommen mit — und werden danach entfernt")

env = m.load_env(key, new_inst)
ok("kein SMTP_PASSWORD in der Kopie", "SMTP_PASSWORD" not in env, env)
ok("kein uebernommenes OAAP_APP_SECRET", "OAAP_APP_SECRET" not in env, env)
ok("der nicht-geheime Wert bleibt", env.get("SMTP_HOST") == "mail.example.org",
   env)
ok("und der Betreiber erfaehrt, was er nachtragen muss",
   dropped == ["SMTP_PASSWORD"], dropped)

print("")
print("Was an die Installation weitergereicht wird")

ok("das Paket der Testinstanz, unveraendert", seen["zip_path"] == PKG)
ok("der Produktiv-Kanal, kein dritter Kanal",
   seen["channel"] == "production")
ok("die vorher gepraegte Kennung, damit Daten und Container dieselbe "
   "meinen", seen["ident"]["id"] == new_inst["id"])
ok("und der rehearsal-Block", bool(seen["rehearsal"]))
ok("er nennt Herkunft der Daten und des Codes",
   seen["rehearsal"]["of"] == "crm"
   and seen["rehearsal"]["code_from"] == "crm-test")
ok("das Archiv und wann es geschrieben wurde",
   seen["rehearsal"]["archive"] == ARCHIVE
   and seen["rehearsal"]["archive_created"], seen["rehearsal"])
ok("und ein Ablaufdatum", bool(seen["rehearsal"]["expires"]))

log = m.read_tenant_log(tid)
entry = next((e for e in log if e["action"] == "rehearsal.create"), None)
ok("das Anlegen steht im Mandantenprotokoll", entry is not None)
ok("mit Instanz, Quelle und Archiv",
   entry and entry["subject"] == key and "crm" in entry.get("detail", "")
   and os.path.basename(ARCHIVE) in entry.get("detail", ""), entry)

print("")
print("Zweimal dieselbe Generalprobe geht nicht")

err = refused(m.rehearsal_review, m.load_registry(), "crm", "probe")
ok("der Name ist vergeben", "already exists" in err, err)

print("")
print("Scheitert die Installation, bleibt keine Kopie liegen")


def broken_install(*a, **kw):
    raise m.ArtifactRejected("das Paket ist kaputt")


m.install_artifact = broken_install
plan2 = m.rehearsal_review(m.load_registry(), "crm", "probe2")
try:
    m.create_rehearsal(plan2)
    boom = ""
except m.ArtifactRejected as e:
    boom = str(e)
ok("der Fehler kommt durch", bool(boom), boom)
left = m.instance_dir("probe2", {"id": "", "tenant": tid})
ok("und kein halb gebautes Verzeichnis mit Produktivdaten bleibt zurueck",
   not any(os.path.isdir(os.path.join(m.tenant_dir(tid), "instances", d))
           and d not in ("aaaaaaaaaaaa", "cccccccccccc", new_inst["id"])
           for d in os.listdir(os.path.join(m.tenant_dir(tid), "instances"))),
   os.listdir(os.path.join(m.tenant_dir(tid), "instances")))
ok("und kein Registry-Eintrag",
   "probe2" not in m.load_registry()["instances"])
m.install_artifact = real_install

print("")
print("Verlaengern — jederzeit, gezaehlt, protokolliert")

before = m.load_registry()["instances"][key]["rehearsal"]["expires"]
new_date = m.rehearsal_extend(key, days=7)
after = m.load_registry()["instances"][key]["rehearsal"]
ok("das Datum wandert nach hinten", new_date > before, (before, new_date))
ok("und die Verlaengerung wird gezaehlt", after["extensions"] == 1)
m.rehearsal_extend(key, days=7)
ok("jede weitere auch",
   m.load_registry()["instances"][key]["rehearsal"]["extensions"] == 2)
log = m.read_tenant_log(tid)
ok("jede steht im Mandantenprotokoll",
   sum(1 for e in log if e["action"] == "rehearsal.extend") == 2,
   "eine sechsmal verlaengerte Generalprobe ist keine mehr — und das darf "
   "keine Gedaechtnisfrage sein")

err = refused(m.rehearsal_extend, "crm")
ok("eine gewoehnliche Instanz laesst sich nicht verlaengern",
   "not a rehearsal" in err, err)
ok("und die Ablehnung sagt, warum es kein allgemeines Ablaufdatum gibt",
   "trap with a timer" in err, err)

print("")
print("Der Sweep — und die Sperre, die nicht fehlen darf")

reg = m.load_registry()
# Eine GEWOEHNLICHE Instanz, die zufaellig ein 'expires' im Datensatz
# traegt, und noch dazu ein laengst vergangenes. Sie muss ueberleben.
past = (datetime.datetime.now(datetime.timezone.utc)
        - datetime.timedelta(days=400)).strftime("%Y-%m-%dT%H:%M:%SZ")
reg["instances"]["lager"] = record(
    tenant=tid, id="bbbbbbbbbbbb", name="lager", app_id="lager",
    app_name="Lager", port=8102, container="oaap-app-lager",
    services=[{"service": "", "container": "oaap-app-lager",
               "image": "oaap-app/lager:1.0.0", "build": "", "port": 8000}],
    expires=past)
reg["instances"][key]["rehearsal"]["expires"] = past
m.save_registry(reg)
lager_dir = m.instance_dir("lager", reg["instances"]["lager"])
os.makedirs(os.path.join(lager_dir, "storage"), exist_ok=True)
with open(os.path.join(lager_dir, "storage", "bestand.db"), "w",
          encoding="utf-8") as f:
    f.write("wichtig\n")

ok("die abgelaufene Generalprobe gilt als abgelaufen",
   m.rehearsal_expired(m.load_registry()["instances"][key]))
ok("die gewoehnliche Instanz mit 'expires' NICHT",
   not m.rehearsal_expired(m.load_registry()["instances"]["lager"]),
   "sonst waere die automatische Loeschung eine Falle mit Zeitschaltuhr")

with contextlib.redirect_stdout(_io.StringIO()):
    done = m.rehearsal_sweep()
after_reg = m.load_registry()
ok("die Generalprobe ist weg", key not in after_reg["instances"], done)
ok("und ihre Daten mit ihr", not os.path.isdir(new_dir), new_dir)
ok("die gewoehnliche Instanz steht noch", "lager" in after_reg["instances"])
ok("und ihre Daten auch",
   os.path.isfile(os.path.join(lager_dir, "storage", "bestand.db")))
log = m.read_tenant_log(tid)
ok("das Loeschen steht im Mandantenprotokoll",
   any(e["action"] == "rehearsal.expire" and e["subject"] == key for e in log))

print("")
print("Ein unlesbares Ablaufdatum loescht nichts")

reg = m.load_registry()
reg["instances"]["kaputt"] = record(
    tenant=tid, id="eeeeeeeeeeee", name="kaputt", port=8105,
    container="oaap-app-kaputt",
    services=[{"service": "", "container": "oaap-app-kaputt",
               "image": "oaap-app/crm:1.5.0", "build": "", "port": 8000}],
    rehearsal={"of": "crm", "code_from": "crm-test", "archive": ARCHIVE,
               "created": "", "expires": "irgendwann", "extensions": 0})
m.save_registry(reg)
with contextlib.redirect_stdout(_io.StringIO()):
    m.rehearsal_sweep()
ok("sie ueberlebt und will angesehen werden",
   "kaputt" in m.load_registry()["instances"],
   "ein unlesbares Feld ist ein Grund hinzusehen, nie einer zu loeschen")

print("")
print("Von Hand entfernt heisst ebenfalls: Daten weg")

# Ueberall sonst ist 'Daten behalten' die sichere Vorgabe -- im Portal
# ist das Haekchen sogar vorgabegemaess aus. Hier waere genau das die
# Falle: liegen bliebe eine Kopie echter Kundendaten, und der
# Rueckhalte-Eintrag boete sie der naechsten Instanz gleichen Namens an.
reg = m.load_registry()
reg["instances"]["handprobe"] = record(
    tenant=tid, id="ffffffffffff", name="handprobe", port=8106,
    container="oaap-app-handprobe",
    services=[{"service": "", "container": "oaap-app-handprobe",
               "image": "oaap-app/crm:1.5.0", "build": "", "port": 8000}],
    rehearsal={"of": "crm", "code_from": "crm-test", "archive": ARCHIVE,
               "archive_created": "2026-09-06T02:00:11Z",
               "created": "2026-09-08T09:00:00Z",
               "expires": "2099-01-01T00:00:00Z", "extensions": 0})
m.save_registry(reg)
hand_dir = m.instance_dir("handprobe", reg["instances"]["handprobe"])
os.makedirs(os.path.join(hand_dir, "storage"), exist_ok=True)
with open(os.path.join(hand_dir, "storage", "kopie.db"), "w",
          encoding="utf-8") as f:
    f.write("echte Kundendaten")

with contextlib.redirect_stdout(_io.StringIO()) as out:
    msg = m.remove_instance(m.load_registry(), "handprobe", purge=False)
ok("ohne --purge wird trotzdem geloescht", not os.path.isdir(hand_dir),
   hand_dir)
ok("und es wird gesagt, statt es stillschweigend zu tun",
   "RFC-0030" in out.getvalue(), out.getvalue())
ok("es bleibt auch kein Rueckhalte-Eintrag",
   not any(k.endswith("|handprobe")
           for k in (m.load_registry().get("retained") or {})),
   m.load_registry().get("retained"))

with contextlib.redirect_stdout(_io.StringIO()):
    m.remove_instance(m.load_registry(), "lager", purge=False)
ok("eine gewoehnliche Instanz behaelt ihre Daten wie bisher",
   os.path.isfile(os.path.join(lager_dir, "storage", "bestand.db")),
   "das ist ueberall sonst die sichere Vorgabe")

print("")
print("Eine Wiederherstellung startet keine Generalprobe")

reg = m.load_registry()
reg["instances"]["wieder"] = record(
    tenant=tid, id="99999999aaaa", name="wieder", port=8107,
    container="oaap-app-wieder",
    services=[{"service": "", "container": "oaap-app-wieder",
               "image": "oaap-app/crm:1.5.0", "build": "", "port": 8000}],
    rehearsal={"of": "crm", "code_from": "crm-test", "archive": ARCHIVE,
               "archive_created": "2026-09-06T02:00:11Z",
               "created": "2026-09-08T09:00:00Z",
               "expires": "2099-01-01T00:00:00Z", "extensions": 0})
m.save_registry(reg)
started = []
m._deploy_from_registry = lambda n, i: started.append(n) or True
with contextlib.redirect_stdout(_io.StringIO()) as out:
    m.cmd_restore_instances(None)
ok("die Generalprobe wird uebersprungen", "wieder" not in started, started)
ok("und die Begruendung steht dabei",
   "RFC-0030" in out.getvalue(), out.getvalue())
ok("gewoehnliche Instanzen kommen zurueck", "crm" in started, started)

print("")
print("Ein abgelehntes Paket laesst kein Verzeichnis zurueck")

# Am laufenden oaap-test gefunden (2026-09-08), waehrend die
# Generalprobe geprueft wurde: zwei abgelehnte Manifeste hatten
# `tenants/<tid>/instances/<iid>/artifacts/` liegen lassen -- leer, von
# nichts referenziert, und nicht mehr zuzuordnen, weil die Kennung
# absichtlich erst am Ende in die Registry kommt. Aelter als RFC-0030,
# aber hier entstanden.
tenant_root = os.path.join(m.tenant_dir(tid), "instances")
vorher = set(os.listdir(tenant_root))
bad = os.path.join(DATA, "kaputt.zip")
import zipfile                                                # noqa: E402
# Das Manifest muss WEIT GENUG kommen, um den Befund ueberhaupt zu
# erzeugen: bis hinter `artifact_store()`, das die Kennung praegt und
# das Verzeichnis anlegt. Ein Manifest ohne `app.version` scheitert
# vorher und laesst nichts liegen -- die erste Fassung dieser Pruefung
# bestand deshalb auch OHNE die Korrektur, und genau das ist der Fehler,
# den dieses Projekt schon einmal gemacht hat. Das hier ist der echte
# Fall von oaap-test: gueltig genug zum Ablegen, abgelehnt von
# validate_manifest, weil `health.path` fehlt.
with zipfile.ZipFile(bad, "w") as z:
    z.writestr("oaap-app.yaml", "\n".join([
        "oaap_manifest: '0.2'",
        "app: {id: neuling, name: Neuling, version: 1.0.0, type: image}",
        "services: {web: {image: nginx:alpine, port: 80}}",
        "routes: [{path: /, roles: [admin]}]",
        ""]))
try:
    with contextlib.redirect_stdout(_io.StringIO()):
        m.install_artifact("neuling", bad, None, channel="test",
                           permit={"tenant": tid, "name": "neuling"})
    boom = ""
except BaseException as e:                                    # noqa: BLE001
    boom = str(e) or e.__class__.__name__
ok("das kaputte Paket wird abgelehnt", bool(boom), boom)
ok("und hinterlaesst kein Verzeichnis",
   set(os.listdir(tenant_root)) == vorher,
   sorted(set(os.listdir(tenant_root)) - vorher))

print("")
print("Und die Faehigkeit ist auch aufrufbar")

# 0.1.78: 'backup schedule' stand im Code und fehlte in den choices des
# Parsers ("invalid choice: 'schedule'"). Der Test las den Quelltext und
# rief nie auf; aufgefallen ist es am laufenden Knoten. argparse
# beendet eine unbekannte Wahl mit Rueckgabewert 2 — genau darauf wird
# hier geprueft, und die Gegenprobe unten zeigt, dass die Pruefung
# ueberhaupt fehlschlagen KANN.
def through_parser(*argv):
    try:
        with contextlib.redirect_stdout(_io.StringIO()),                 contextlib.redirect_stderr(_io.StringIO()):
            sys.argv = ["oaap app", *argv]
            m.main()
    except SystemExit as e:
        return e.code
    except Exception:                                         # noqa: BLE001
        return "raised"
    return 0


ok("'oaap app rehearsal list' kennt der Parser",
   through_parser("rehearsal", "list") != 2)
ok("'oaap app rehearsal extend' auch",
   through_parser("rehearsal", "extend", "probe") != 2)
ok("'oaap app rehearsal sweep' auch",
   through_parser("rehearsal", "sweep") != 2)
ok("'oaap app rehearse <instanz> --name <neu>' ebenso",
   through_parser("rehearse", "crm", "--name", "probe3") != 2)
ok("die Gegenprobe: eine Wahl, die es nicht gibt, faellt durch",
   through_parser("rehearsal", "gibtsnicht") == 2,
   "sonst wuerde diese Pruefung alles bestehen lassen")
ok("und ohne --yes wird nichts gebaut",
   "probe3" not in m.load_registry()["instances"],
   "eine zweite Kopie echter Kundendaten entsteht nicht nebenbei")

print("")
print("ALLE PRUEFUNGEN BESTANDEN" if not fails else f"{fails} FEHLER")
sys.exit(1 if fails else 0)
