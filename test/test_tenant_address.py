#!/usr/bin/env python3
"""Ein Namensraum, zwei Arten von Namen (RFC-0042 T1).

`<kuerzel>.<knoten>` und `<instanz>.<knoten>` haben dieselbe Form. Ein
Mandanten-Kuerzel und der Name einer Instanz des **Standard-Mandanten**
wollen also dieselbe Adresse -- und bis zur Mandantenadresse hat das
niemand gegeneinander geprueft: `label_is_free` fragte nur nach
Mandanten, das Anlegen einer Instanz nur nach der Registry.

Solange unter `<kuerzel>.<knoten>` nichts antwortete, war das folgenlos.
Ab dem Moment, in dem dort etwas antwortet, gewinnt, wer zuletzt
geschrieben wird -- lautlos. Deshalb kommt die Wache VOR der Adresse;
danach waere dieselbe Aenderung eine Migration.

Der Test prueft beide Richtungen und **zaehlt die Tueren**: es gibt
mehrere Wege, eine Instanz entstehen zu lassen, und der eine, der die
Regel nicht traegt, ist die Fehlerform, die dieses Projekt sammelt
(0.1.109, 0.1.110, 0.1.111, 0.1.114). Eine Ausnahmeliste gaebe es hier
nicht -- sie waere die naechste Stelle, die auseinanderlaeuft. Statt
dessen darf der Ablehnungssatz nur an EINER Stelle entstehen, und das
ist zaehlbar.

Braucht kein Docker und keinen Knoten.

Aufruf: python3 test/test_tenant_address.py
"""
import argparse
import contextlib
import io as _io
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-addr-test-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, os.path.join(HERE, "..", "platform"))

import appctl as m                                             # noqa: E402

m.reload_gateway = lambda: None
os.makedirs(m.CADDY_APPS_DIR, exist_ok=True)

SRC = _io.open(os.path.join(HERE, "..", "platform", "appctl.py"),
               encoding="utf-8").read()

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
            action="create", name=label, target=None, title=label.upper(),
            account="", account_name="", grace_days=30, yes=True, count=50))
    return m.tenant_by_label(label)[0]


def put_instance(tenant, local, former=None):
    """Eine Instanz in die Registry legen, ohne etwas zu starten."""
    reg = m.load_registry()
    key = m.instance_key(tenant, local)
    rec = {"tenant": tenant, "name": local, "id": m.new_instance_id()}
    if former:
        rec["former_names"] = [{"name": former, "until": m._in_days(30)}]
    reg["instances"][key] = rec
    m.save_registry(reg)
    return key


default_id = m.ensure_default_tenant()
cls_id = make_tenant("cls")

print("")
print("Richtung 1: ein Instanzname sperrt das Kuerzel")

put_instance(default_id, "studio")
ok("ein Name im Standard-Mandanten ist im Adressraum",
   "studio" in m.default_tenant_host_names())
ok("und damit ist das Kuerzel 'studio' nicht mehr frei",
   not m.label_is_free("studio"),
   "sonst wollten studio.<knoten> und die Instanz dieselbe Adresse")
ok("der Mandant kann so nicht angelegt werden",
   m.tenant_label_error("studio", "create") != "")
ok("und die Ablehnung sagt nicht, WAS den Namen haelt",
   m.tenant_label_error("studio", "create").replace("studio", "X")
   == m.tenant_label_error("cls", "create").replace("cls", "X"),
   "bis auf den Namen selbst muss es derselbe Satz sein -- sonst "
   "verriete gerade die neue Wache, dass dort eine Instanz liegt")

put_instance(default_id, "atelier", former="werkstatt")
ok("ein frueherer Instanzname sperrt ihn auch",
   not m.label_is_free("werkstatt"),
   "ein Name, der noch irgendwohin leitet, darf nicht neu vergeben "
   "werden (RFC-0026 3.3)")

put_instance(cls_id, "viewer")
ok("eine Instanz eines ANDEREN Mandanten sperrt nichts",
   m.label_is_free("viewer"),
   "sie antwortet unter viewer.cls.<knoten>, eine Ebene tiefer")

print("")
print("Richtung 2: ein Kuerzel sperrt den Instanznamen")

reg = m.load_registry()
ok("im Standard-Mandanten ist 'cls' vergeben",
   m.instance_name_taken(reg, default_id, "cls"))
ok("in einem anderen Mandanten ist derselbe Name frei",
   not m.instance_name_taken(reg, cls_id, "cls"),
   "cls.cls.<knoten> kollidiert mit nichts")

with contextlib.redirect_stdout(_io.StringIO()):
    m.cmd_tenant(argparse.Namespace(
        action="rename", name="cls", target="cls2", title="", account="",
        account_name="", grace_days=30, yes=True, count=50))
ok("ein frueheres Kuerzel sperrt ihn ebenso",
   m.instance_name_taken(m.load_registry(), default_id, "cls"),
   "die Schonfrist aus RFC-0026 gilt in beide Richtungen")

ok("und das Umbenennen einer Instanz faellt unter dieselbe Wache",
   m.rename_check(m.load_registry(), "studio", "cls2")[1] != "",
   "umbenennen ist eine Tuer in den Adressraum wie anlegen -- und die, "
   "die man am leichtesten vergisst")
ok("ein freier Name geht weiterhin durch",
   m.rename_check(m.load_registry(), "studio", "freier-name")[1] == "")

print("")
print("Die Tueren zaehlen")

ok("der Ablehnungssatz entsteht an genau einer Stelle",
   SRC.count("an instance named '") == 1,
   "eine zehnte Tuer, die ihren Satz selbst baut, faellt hier auf, "
   "bevor sie die Pruefung vergisst")
doors = len(re.findall(r"\bname_taken_msg\(", SRC)) - 1   # ohne die Definition
ok(f"und wird von mehreren Tueren geholt ({doors})", doors >= 8, doors)
asks = len(re.findall(r"\binstance_name_taken\(", SRC)) - 1
ok(f"die Wache wird an mehreren Tueren gefragt ({asks})", asks >= 6, asks)
ok("und sie fragt selbst beide Namensraeume",
   bool(re.search(r"def instance_name_taken\b[\s\S]{0,1400}?"
                  r"label_taken_by_tenant\(", SRC)),
   "sonst prueft sie nur die Registry, also die Haelfte")
ok("label_is_free fragt die andere Richtung",
   bool(re.search(r"def label_is_free\b[\s\S]{0,900}?"
                  r"default_tenant_host_names\(", SRC)))

print("")
print(f"{'FEHLER' if fails else 'Alles gruen'} - {fails} Fehlschlag(e)")
sys.exit(1 if fails else 0)
