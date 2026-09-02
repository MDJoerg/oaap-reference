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
print("Ein Umbenennen des Mandanten zieht die Kennungen mit")

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
ok("und sagt, dass die Apps neu gebaut werden und neu starten",
   "REBUILT" in out and "restart" in out, out[:800])
ok("und dass trotzdem nichts auf der Platte verschoben wird",
   "Nothing moves on disk" in out, out[:800])
ok("die alten Deploy-Adressen bekommen eine Schonfrist genannt",
   "old deploy addresses keep working" in out, out[:800])
# Seit RFC-0026 D2b folgen Kennungen dem AKTUELLEN Kuerzel: der
# eingefrorene Kurzname aus RFC-0025 ist zurueckgezogen, weil die Daten
# nicht mehr am Namen haengen. Eine Umbenennung baut Container neu -
# und verschiebt nichts.
ok("der Kurzname folgt dem neuen Kuerzel", m.tenant_slug(cls_id) == "abc")
reg_nach = m.load_registry()["instances"]
ok("die Instanz traegt jetzt den neuen Schluessel",
   "abc-viewer" in reg_nach and "cls-viewer" not in reg_nach,
   sorted(reg_nach))
ok("sie heisst aber unveraendert wie vorher",
   m.instance_name("abc-viewer", reg_nach["abc-viewer"]) == "viewer")
ok("die Adresse folgt dem neuen Kuerzel",
   m.instance_auto_hosts("abc-viewer", reg_nach["abc-viewer"],
                         ext_host=HOST)[0] == "viewer.abc." + HOST)
ok("und das alte Kuerzel antwortet die Schonfrist ueber weiter",
   "viewer.cls." + HOST
   in m.instance_auto_hosts("abc-viewer", reg_nach["abc-viewer"],
                            ext_host=HOST))
ok("die alte Deploy-Adresse gilt die Schonfrist ueber auch",
   m.resolve_deploy_target(m.load_registry(), "cls-viewer") == "abc-viewer",
   "sonst braeche jede ausgelieferte Pipeline mit der Umbenennung")

print("")
print("Ein freigewordenes Kuerzel macht keinen zweiten Anspruch")

# Ein Kuerzel, das frei geworden ist, darf ein neuer Mandant nehmen --
# und weil Kennungen seit RFC-0026 dem AKTUELLEN Kuerzel folgen, ist
# damit auch der Schluessel eindeutig. Der Praege-Mechanismus mit
# Zaehlsuffix aus RFC-0025 ist damit weg: Zwei Mandanten koennen
# dasselbe Kuerzel gar nicht gleichzeitig fuehren.
alt_id = make_tenant("tmp")
rename("tmp", "tmp-neu", grace=0)
ok("nach dem Umbenennen ohne Schonfrist ist das Kuerzel frei",
   m.label_is_free("tmp"))
ok("der alte Mandant fuehrt jetzt das neue Kuerzel",
   m.tenant_slug(alt_id) == "tmp-neu")

zwei_id = make_tenant("tmp")
ok("ein neuer Mandant darf das freie Kuerzel nehmen",
   m.tenant_label(zwei_id) == "tmp")
ok("und bekommt es auch als Kennung",
   m.tenant_slug(zwei_id) == "tmp")
ok("die beiden Schluessel bleiben trotzdem verschieden",
   m.instance_key(zwei_id, "viewer") != m.instance_key(alt_id, "viewer"),
   "weil zwei Mandanten dasselbe Kuerzel nie gleichzeitig fuehren")
ok("kein Zaehlsuffix mehr noetig",
   not hasattr(m, "mint_slug"),
   "die Praegung aus RFC-0025 ist mit dem eingefrorenen Kurznamen weg")

print("")
print("Denselben Namen zweimal im selben Mandanten gibt es nicht")

key, found = m.find_instance(m.load_registry(), cls_id, "viewer")
ok("die Suche findet die Instanz an ihrem Mandanten-Namen",
   key == "abc-viewer" and found is not None)
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
buf = _io.StringIO()
with contextlib.redirect_stdout(buf):
    m.cmd_migrate_tenants(None)
bericht = buf.getvalue()
reg = m.load_registry()
ok("eine Instanz ohne Namen bekommt ihren Schluessel als Namen",
   reg["instances"]["altbestand"]["name"] == "altbestand")
ok("die Migration sagt, was sie getan hat",
   "recorded under the name" in bericht, bericht)
ok("bestehende Schluessel bleiben unangetastet",
   "abc-viewer" in reg["instances"] and "altbestand" in reg["instances"],
   "die Umstellung benennt nichts um -- Schluessel muessen eindeutig "
   "sein, nicht einheitlich")
ok("und der Kurzname aus RFC-0025 ist aus den Datensaetzen entfernt",
   not any("slug" in t for t in m.load_tenants().values()),
   "ein gespeicherter Kurzname koennte nur noch dem Kuerzel widersprechen")
buf2 = _io.StringIO()
with contextlib.redirect_stdout(buf2):
    m.cmd_migrate_tenants(None)
ok("beim zweiten Lauf ist sie still", buf2.getvalue().strip() == "",
   buf2.getvalue())

print("")
print(f"{'FEHLER' if fails else 'Alles gruen'} - {fails} Fehlschlag(e)")
sys.exit(1 if fails else 0)
