#!/usr/bin/env python3
"""Ein Instanzname gilt je Mandant (RFC-0025).

Zwei Kunden duerfen beide eine `viewer` haben. Damit sich ihre
Container, Netzwerke, Verzeichnisse und Deploy-Adressen nicht ins
Gehege kommen, benutzt alles Knotenweite einen **Schluessel**:

    <kurzname>-<name>      in einem Mandanten
    <name>                 im Standard-Mandanten

Der **Kurzname** wird beim Anlegen des Mandanten einmal vergeben und nie
wieder geaendert -- auch nicht von `tenant rename`. Genau das haelt ein
Umbenennen bei einer Umbenennung, statt es zu einer Migration mit
Ausfallzeit zu machen (RFC-0025 8.1).

Die drei Saetze, an denen hier alles haengt:

    Die Adresse traegt den Namen, den der Kunde gewaehlt hat.
    Der Schluessel traegt den Kurznamen, damit nichts kollidiert.
    Die Migration benennt nichts um.

Braucht kein Docker und keinen Knoten.

Aufruf: python3 test/test_instance_namespace.py
"""
import argparse
import contextlib
import io as _io
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-ns-test-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services", "portal"))
sys.path.insert(0, os.path.join(HERE, "..", "platform"))

import appctl as m                                             # noqa: E402
import instance_view as iv                                     # noqa: E402

m.reload_gateway = lambda: None
os.makedirs(m.CADDY_APPS_DIR, exist_ok=True)

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:400]}")


def make_tenant(label, title="Kunde"):
    with contextlib.redirect_stdout(_io.StringIO()):
        m.cmd_tenant(argparse.Namespace(
            action="create", name=label, target=None, title=title,
            account="", account_name="", grace_days=30, yes=True, count=50))
    return m.tenant_by_label(label)[0]


def rename(old, new, grace=30):
    buf = _io.StringIO()
    with contextlib.redirect_stdout(buf):
        m.cmd_tenant(argparse.Namespace(
            action="rename", name=old, target=new, title="", account="",
            account_name="", grace_days=grace, yes=True, count=50))
    return buf.getvalue()


print("")
print("Der Kurzname wird einmal vergeben")

default_id = m.ensure_default_tenant()
ok("der Standard-Mandant hat gar keinen Kurznamen",
   m.tenant_slug(default_id) == "",
   "sein Kuerzel ist die Abwesenheit eines Kuerzels -- sonst zoege die "
   "Umstellung jede bestehende Adresse mit")

cls_id = make_tenant("cls")
meier_id = make_tenant("meier")
ok("ein Kundenmandant bekommt seinen", m.tenant_slug(cls_id) == "cls")
ok("und jeder einen eigenen", m.tenant_slug(meier_id) == "meier")

print("")
print("Der Schluessel haelt zwei Kunden auseinander")

ok("im Standard-Mandanten bleibt der Name der Schluessel",
   m.instance_key(default_id, "viewer") == "viewer",
   "alles Bestehende behaelt damit seine Kennung")
ok("in einem Mandanten kommt der Kurzname davor",
   m.instance_key(cls_id, "viewer") == "cls-viewer")
ok("zwei Kunden koennen dieselbe App gleich nennen",
   m.instance_key(cls_id, "viewer") != m.instance_key(meier_id, "viewer"))

print("")
print("Die Adresse traegt den Namen, nicht den Schluessel")

inst_cls = {"tenant": cls_id, "name": "viewer"}
HOST = "oaap.joomp.de"
ok("appctl: viewer.cls.<knoten>, nicht cls-viewer.cls.<knoten>",
   m.instance_auto_hosts("cls-viewer", inst_cls, ext_host=HOST)
   == ["viewer.cls." + HOST])
ok("das Portal sagt dasselbe",
   iv.auto_host("cls-viewer", inst_cls, m.load_tenants(), HOST)
   == "viewer.cls." + HOST)
ok("im Standard-Mandanten aendert sich nichts",
   m.instance_auto_hosts("bdt-hub", {"tenant": default_id, "name": "bdt-hub"},
                         ext_host=HOST) == ["bdt-hub." + HOST])

print("")
print("Ein Umbenennen des Mandanten fasst nichts Internes an")

reg = m.load_registry()
reg["instances"]["cls-viewer"] = {
    "app_id": "viewer", "app_name": "Viewer", "tenant": cls_id,
    "name": "viewer", "version": "1.0", "channel": "test", "port": 8500,
    "container": "oaap-app-cls-viewer", "svc_port": 8000,
    "routes": [{"path": "/", "roles": ["user"]}]}
m.save_registry(reg)

out = rename("cls", "abc")
ok("die Vorschau nennt die Instanz beim Namen des Kunden",
   "viewer.cls." in out and "viewer.abc." in out
   and "cls-viewer.cls." not in out, out[:400])
ok("und sagt ausdruecklich, dass nichts umgebaut wird",
   "Not touched" in out and "cls" in out, out[:600])
ok("der Kurzname bleibt nach dem Umbenennen stehen",
   m.tenant_slug(cls_id) == "cls",
   "sonst muesste jedes Umbenennen Container und Verzeichnisse mitnehmen")
ok("der Schluessel der Instanz bleibt damit auch stehen",
   "cls-viewer" in m.load_registry()["instances"])
ok("die Adresse folgt aber dem neuen Kuerzel",
   m.instance_auto_hosts("cls-viewer", m.load_registry()["instances"]["cls-viewer"],
                         ext_host=HOST)[0] == "viewer.abc." + HOST)
ok("und das alte Kuerzel antwortet die Schonfrist ueber weiter",
   "viewer.cls." + HOST
   in m.instance_auto_hosts("cls-viewer",
                            m.load_registry()["instances"]["cls-viewer"],
                            ext_host=HOST))

print("")
print("Ein freigewordenes Kuerzel macht keinen zweiten Anspruch")

# Ein Mandant wird ohne Schonfrist umbenannt -- sein altes Kuerzel ist
# danach sofort frei, sein KURZNAME aber bleibt bei ihm.
alt_id = make_tenant("tmp")
rename("tmp", "tmp-neu", grace=0)
ok("nach dem Umbenennen ohne Schonfrist ist das Kuerzel frei",
   m.label_is_free("tmp"))
ok("der Kurzname gehoert aber weiter dem alten Mandanten",
   m.tenant_slug(alt_id) == "tmp")

zwei_id = make_tenant("tmp")
ok("ein neuer Mandant darf das freie Kuerzel nehmen",
   m.tenant_label(zwei_id) == "tmp")
ok("bekommt aber einen anderen Kurznamen",
   m.tenant_slug(zwei_id) == "tmp-2",
   "sonst schrieben beide in dieselben Verzeichnisse")
ok("und damit einen anderen Schluessel",
   m.instance_key(zwei_id, "viewer") == "tmp-2-viewer")
ok("waehrend der alte seinen behaelt",
   m.instance_key(alt_id, "viewer") == "tmp-viewer")

print("")
print("Denselben Namen zweimal im selben Mandanten gibt es nicht")

key, found = m.find_instance(m.load_registry(), cls_id, "viewer")
ok("die Suche findet die Instanz an ihrem Mandanten-Namen",
   key == "cls-viewer" and found is not None)
ok("und im falschen Mandanten nicht",
   m.find_instance(m.load_registry(), meier_id, "viewer") == ("", None))
ok("eine Instanz von vor der Umstellung ist ihr eigener Name",
   m.instance_name("bdt-hub", {}) == "bdt-hub")

print("")
print("Die Migration benennt nichts um")

reg = m.load_registry()
reg["instances"]["altbestand"] = {
    "app_id": "alt", "app_name": "Alt", "tenant": meier_id,
    "version": "1", "channel": "test", "port": 8501}
m.save_registry(reg)
tenants = m.load_tenants()
for t in tenants.values():
    t.pop("slug", None)
m.save_tenants(tenants)

buf = _io.StringIO()
with contextlib.redirect_stdout(buf):
    m.cmd_migrate_tenants(None)
bericht = buf.getvalue()
ok("sie vergibt fehlende Kurznamen", "short name" in bericht, bericht)
ok("und zwar das Kuerzel selbst, nicht eine Variante davon",
   m.tenant_slug(meier_id) == "meier"
   and not any(m.tenant_slug(t).endswith("-2") for t in m.load_tenants()),
   "am echten Knoten kam hier zuerst 'cls-2' heraus: die Praegung sah "
   "den Mandanten, fuer den sie praegte, als Kollision mit sich selbst")
vorher = m.tenant_slug(meier_id)
with contextlib.redirect_stdout(_io.StringIO()):
    m.cmd_migrate_tenants(None)
ok("ein einmal gesetzter Kurzname wird von der Migration nie ueberschrieben",
   m.tenant_slug(meier_id) == vorher,
   "er ist eingefroren -- auch gegen die eigene Migration")
ok("und sagt ausdruecklich, dass nichts umbenannt wurde",
   "nothing renamed" in bericht, bericht)
reg = m.load_registry()
ok("bestehende Schluessel bleiben unangetastet",
   "cls-viewer" in reg["instances"] and "altbestand" in reg["instances"])
ok("eine Instanz ohne Namen bekommt ihren Schluessel als Namen",
   reg["instances"]["altbestand"]["name"] == "altbestand")
ok("und behaelt ihn danach",
   m.instance_auto_hosts("altbestand", reg["instances"]["altbestand"],
                         ext_host=HOST)[0] == "altbestand.meier." + HOST)
buf2 = _io.StringIO()
with contextlib.redirect_stdout(buf2):
    m.cmd_migrate_tenants(None)
ok("beim zweiten Lauf ist sie still", buf2.getvalue().strip() == "",
   buf2.getvalue())

print("")
print(f"{'FEHLER' if fails else 'Alles gruen'} - {fails} Fehlschlag(e)")
sys.exit(1 if fails else 0)
