#!/usr/bin/env python3
"""Die Daten eines Mandanten liegen in einem Baum (RFC-0026).

Bis 0.1.59 lag jede Instanz flach in `apps/<schluessel>/` -- zwischen
den JSON-Dateien der Plattform, und nichts an dem Verzeichnis sagte,
wem es gehoert. Deshalb musste 0.1.56 eine Markierungsdatei
hineinschreiben: eine Kruecke fuer eine Ablage, die nicht sagt, was sie
enthaelt.

Jetzt haengt der Pfad an IDENTITAETEN:

    tenants/<mandant-id>/instances/<instanz-id>/

Drei Dinge fallen dabei ab, und alle drei werden hier geprueft: alle
Daten eines Mandanten sind ein Pfad; eine Umbenennung verschiebt nichts;
und Daten koennen die Mandantengrenze gar nicht mehr versehentlich
ueberqueren.

Dazu die Zusage, die dabei fast verloren gegangen waere: eine
Neuinstallation unter demselben Namen findet ihre Daten wieder. Das
steht so im Entfernen-Dialog, und mit id-basierten Pfaden gilt es nur
noch, weil das Entfernen sich die Identitaet merkt.

Braucht kein Docker und keinen Knoten.

Aufruf: python3 test/test_tenant_tree.py
"""
import argparse
import contextlib
import io as _io
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-tree-test-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, os.path.join(HERE, "..", "platform"))

import appctl as m                                             # noqa: E402

m.reload_gateway = lambda: None
os.makedirs(m.CADDY_APPS_DIR, exist_ok=True)

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:400]}")


def make_tenant(label):
    with contextlib.redirect_stdout(_io.StringIO()):
        m.cmd_tenant(argparse.Namespace(
            action="create", name=label, target=None, title="Kunde",
            account="", account_name="", grace_days=30, yes=True, count=50))
    return m.tenant_by_label(label)[0]


def put_instance(key, tid, name, iid=None):
    reg = m.load_registry()
    inst = {"app_id": "demo", "app_name": "Demo", "tenant": tid, "name": name,
            "version": "1.0", "channel": "test", "port": 8600,
            "container": f"oaap-app-{key}", "svc_port": 8000,
            "image": "demo:1", "services": [
                {"service": "web", "container": f"oaap-app-{key}",
                 "image": "demo:1", "port": 8000}],
            "routes": [{"path": "/", "roles": ["user"]}]}
    if iid:
        inst["id"] = iid
    reg["instances"][key] = inst
    m.save_registry(reg)
    return inst


print("")
print("Der Pfad haengt an Identitaeten, nicht an Namen")

default_id = m.ensure_default_tenant()
cls_id = make_tenant("cls")

inst = put_instance("cls-viewer", cls_id, "viewer", iid="a1b2c3d4e5f6")
d = m.instance_dir("cls-viewer", inst)
ok("er liegt unter tenants/<mandant>/instances/<instanz>",
   d == os.path.join(m.TENANTS_DIR, cls_id, "instances", "a1b2c3d4e5f6"), d)
ok("und nicht mehr flach unter apps/", not d.startswith(m.APPS_DIR + os.sep), d)
ok("alles eines Mandanten liegt unter einem Pfad",
   d.startswith(m.tenant_dir(cls_id) + os.sep), d)
ok("Umgebung und Pakete liegen mit darin",
   m.env_path("cls-viewer", inst).startswith(d)
   and m.artifact_dir("cls-viewer", inst).startswith(d))

ok("ein Datensatz von vor der Umstellung behaelt seinen flachen Pfad",
   m.instance_dir("alt", {"tenant": default_id}) == os.path.join(m.APPS_DIR, "alt"),
   "sonst waeren seine Daten weg, bevor die Migration sie bewegt hat")

print("")
print("Eine Umbenennung wuerde nichts verschieben")

vorher = m.instance_dir("cls-viewer", inst)
umbenannt = dict(inst, name="modelle")
ok("ein anderer Name, derselbe Pfad",
   m.instance_dir("cls-modelle", umbenannt) == vorher)
tenants = m.load_tenants()
tenants[cls_id]["label"] = "abc"
m.save_tenants(tenants)
ok("ein anderes Mandanten-Kuerzel, derselbe Pfad",
   m.instance_dir("cls-viewer", inst) == vorher,
   "der Pfad kennt die UUID, nicht das Kuerzel")
tenants[cls_id]["label"] = "cls"
m.save_tenants(tenants)

print("")
print("Lesbare Pfade neben den Identitaeten")

links = m.name_links()
by_label = os.path.join(m.TENANTS_DIR, "by-label", "cls")
by_name = os.path.join(m.tenant_dir(cls_id), "by-name", "viewer")
ok("das Kuerzel zeigt auf das Mandantenverzeichnis",
   links.get(by_label) == m.tenant_dir(cls_id), links)
ok("der Instanzname auf das Instanzverzeichnis",
   links.get(by_name) == vorher, links)
ok("eine Instanz ohne Identitaet bekommt keinen lesbaren Pfad",
   not any("alt" in k for k in links))

print("")
print("Die Grenze ist jetzt die Form des Baums")

meier_id = make_tenant("meier")
inst_m = put_instance("meier-viewer", meier_id, "viewer", iid="ffffffffffff")
ok("zwei Mandanten mit derselben App liegen in getrennten Baeumen",
   not m.instance_dir("meier-viewer", inst_m).startswith(m.tenant_dir(cls_id)),
   "keine Markierungsdatei noetig -- der Pfad sagt es")

print("")
print("Entfernen merkt sich, was es liegen laesst")

reg = m.load_registry()
os.makedirs(os.path.join(vorher, "storage", "data"), exist_ok=True)
m.save_env("cls-viewer", {"API_KEY": "geheim"}, inst)
with contextlib.redirect_stdout(_io.StringIO()):
    meldung = m.remove_instance(reg, "cls-viewer", purge=False)
ok("die Meldung nennt den Namen des Kunden, nicht den Schluessel",
   "viewer" in meldung and "cls-viewer" not in meldung, meldung)
kept = m.load_registry().get("retained") or {}
ok("und es steht ein Merkposten im Register", len(kept) == 1, kept)
ok("der die Identitaet festhaelt",
   list(kept.values())[0]["id"] == "a1b2c3d4e5f6", kept)
ok("die Daten liegen noch da", os.path.isdir(vorher))

print("")
print("Eine Neuinstallation gleichen Namens findet ihre Daten wieder")

reg = m.load_registry()
ident = m.instance_identity(reg, "cls-viewer", cls_id, "viewer")
ok("sie bekommt dieselbe Identitaet zurueck", ident["id"] == "a1b2c3d4e5f6",
   "sonst waere die Zusage aus dem Entfernen-Dialog gebrochen")
ok("und damit dasselbe Verzeichnis",
   m.instance_dir("cls-viewer", ident) == vorher)
ok("das Geheimnis von vorher ist wieder da",
   m.load_env("cls-viewer", ident).get("API_KEY") == "geheim")
ok("ein anderer Name im selben Mandanten bekommt eine neue Identitaet",
   m.instance_identity(reg, "cls-anders", cls_id, "anders")["id"]
   != "a1b2c3d4e5f6")
ok("und derselbe Name in einem ANDEREN Mandanten ebenfalls",
   m.instance_identity(reg, "meier-viewer2", meier_id, "viewer")["id"]
   != "a1b2c3d4e5f6",
   "hier verlief frueher genau das Leck aus 0.1.56")

print("")
print("Aufraeumen loescht Daten und Merkposten")

buf = _io.StringIO()
with contextlib.redirect_stdout(buf):
    m.cmd_purge(argparse.Namespace(name="viewer", yes=False))
ok("die Vorschau nennt den echten Pfad und den Mandanten",
   vorher in buf.getvalue() and "cls" in buf.getvalue(), buf.getvalue())
ok("und loescht noch nichts", os.path.isdir(vorher))
with contextlib.redirect_stdout(_io.StringIO()):
    m.cmd_purge(argparse.Namespace(name="viewer", yes=True))
ok("mit --yes sind die Daten weg", not os.path.isdir(vorher))
ok("und der Merkposten auch", not (m.load_registry().get("retained") or {}))
ok("danach bekommt derselbe Name eine frische Identitaet",
   m.instance_identity(m.load_registry(), "cls-viewer", cls_id, "viewer")["id"]
   != "a1b2c3d4e5f6")

print("")
print("Die Umstellung bewegt vorhandene Daten, ohne etwas zu loeschen")

alt_dir = os.path.join(m.APPS_DIR, "altbestand")
os.makedirs(os.path.join(alt_dir, "storage", "data"), exist_ok=True)
with open(os.path.join(alt_dir, "storage", "data", "kunde.txt"), "w") as f:
    f.write("wichtig")
with open(os.path.join(alt_dir, m.TENANT_MARKER), "w") as f:
    f.write(meier_id)
put_instance("altbestand", meier_id, "altbestand")

buf = _io.StringIO()
with contextlib.redirect_stdout(buf):
    m.cmd_migrate_instance_dirs(None)
bericht = buf.getvalue()
neu = m.load_registry()["instances"]["altbestand"]
ok("die Instanz hat jetzt eine Identitaet", bool(neu.get("id")), neu)
ziel = m.instance_dir("altbestand", neu)
ok("das alte Verzeichnis ist leer geraeumt", not os.path.isdir(alt_dir))
ok("die Kundendatei liegt unversehrt im neuen Baum",
   open(os.path.join(ziel, "storage", "data", "kunde.txt")).read() == "wichtig")
ok("die Markierungsdatei ist weg -- der Pfad sagt es jetzt",
   not os.path.exists(os.path.join(ziel, m.TENANT_MARKER)))
ok("die Umstellung sagt, was sie getan hat",
   "moved" in bericht and "identity" in bericht, bericht)

buf2 = _io.StringIO()
with contextlib.redirect_stdout(buf2):
    m.cmd_migrate_instance_dirs(None)
ok("und ist beim zweiten Lauf still", buf2.getvalue().strip() == "",
   buf2.getvalue())

print("")
print(f"{'FEHLER' if fails else 'Alles gruen'} - {fails} Fehlschlag(e)")
sys.exit(1 if fails else 0)
