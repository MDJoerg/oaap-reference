#!/usr/bin/env python3
"""Daten eines entfernten Mandanten wandern nicht zum naechsten weiter.

Der Befund (2026-09-02, aus Joergs Frage nach der Eindeutigkeit der
Deploy-URL): Instanznamen sind **knotenweit** eindeutig, nicht
mandantenweit (spec 2.4). `oaap app remove` behaelt die Daten aber
absichtlich, wenn niemand `--purge` sagt -- im Portal ist das Haekchen
sogar vorgabegemaess aus. Damit entsteht diese Kette:

    Mandant A hat `viewer` -> entfernt ihn ohne --purge -> der Name ist
    frei -> Mandant B legt `viewer` an -> die Installation macht
    `makedirs(..., exist_ok=True)` auf dasselbe Verzeichnis, `load_env`
    liest die alte `instance.env` und `setdefault` laesst die alten
    Werte gewinnen, und die Storage-Mounts zeigen wieder dorthin.

Bei EINEM Mandanten ist genau das ein Merkmal ("neu installieren
behaelt meine Daten"). Ueber die Grenze ist es ein Datenabfluss.

Geprueft werden die Regeln, nicht Docker: Wer darf uebernehmen, wer
nicht, was sagt die Ablehnung -- und vor allem, was sie NICHT sagt.

Aufruf: python3 test/test_retained_data.py
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-retained-test-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, os.path.join(HERE, "..", "platform"))

import appctl as m                                            # noqa: E402

m.reload_gateway = lambda: None

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:400]}")


def leave_data(name, secret="s3cr3t"):
    """Was `oaap app remove` ohne --purge zuruecklaesst."""
    d = m.instance_dir(name)
    os.makedirs(os.path.join(d, "storage", "data"), exist_ok=True)
    m.save_env(name, {"OAAP_APP_SECRET": secret, "API_KEY": "geheim"})


def forget_marker(name):
    p = os.path.join(m.instance_dir(name), m.TENANT_MARKER)
    if os.path.exists(p):
        os.remove(p)


print("")
print("Ein Knoten mit einem Mandanten aendert sich nicht")

default_id = m.ensure_default_tenant()
leave_data("crm")
forget_marker("crm")
ok("Reste ohne Kennzeichen duerfen uebernommen werden",
   m.retained_data_refusal("crm", default_id) == "",
   "das ist der Normalfall 'neu installieren behaelt meine Daten'")
ok("und ein Name ohne jede Hinterlassenschaft sowieso",
   m.retained_data_refusal("gibt-es-nicht", default_id) == "")
ok("ohne Verzeichnis meldet die Auskunft None",
   m.retained_data_tenant("gibt-es-nicht") is None)
ok("mit Verzeichnis, aber ohne Kennzeichen, den leeren String",
   m.retained_data_tenant("crm") == "")

print("")
print("Sobald es zwei Mandanten gibt, haelt die Grenze")

import argparse                                               # noqa: E402
import contextlib                                             # noqa: E402
import io as _io                                              # noqa: E402

with contextlib.redirect_stdout(_io.StringIO()):
    m.cmd_tenant(argparse.Namespace(
        action="create", name="cls", target=None, title="Kunde",
        account="", account_name="", grace_days=30, yes=True, count=50))
cls_id, _t = m.tenant_by_label("cls")

m.stamp_data_tenant("crm", default_id)
ok("der eigene Mandant darf seine Daten weiter uebernehmen",
   m.retained_data_refusal("crm", default_id) == "")

refusal = m.retained_data_refusal("crm", cls_id)
ok("ein anderer Mandant nicht", bool(refusal), refusal)
ok("die Ablehnung nennt den Namen und einen Weg heraus",
   "crm" in refusal and "oaap app purge crm --yes" in refusal, refusal)
ok("sie nennt den anderen Mandanten NICHT",
   "default" not in refusal and cls_id not in refusal and default_id not in refusal,
   refusal)

leave_data("wiki")
forget_marker("wiki")
ok("nicht zuordenbare Reste werden auf einem Knoten mit zwei Mandanten "
   "abgelehnt",
   bool(m.retained_data_refusal("wiki", cls_id)),
   "hier ist nicht mehr entscheidbar, wem sie gehoeren")

print("")
print("Das Kennzeichen liegt neben den Daten, nicht darin")

marker = os.path.join(m.instance_dir("crm"), m.TENANT_MARKER)
ok("es liegt im Instanzverzeichnis", os.path.isfile(marker))
ok("und NICHT unter storage/, das in den Container gemountet wird",
   not os.path.exists(os.path.join(m.instance_dir("crm"), "storage",
                                   m.TENANT_MARKER)),
   "sonst koennte die App es lesen oder faelschen")
ok("es enthaelt die UUID, nicht das Kuerzel",
   open(marker, encoding="utf-8").read().strip() == default_id)

print("")
print("Die Migration macht vorhandene Instanzen zuordenbar")

reg = m.load_registry()
reg["instances"]["altbestand"] = {"app_id": "alt", "app_name": "Alt",
                                  "tenant": cls_id, "version": "1",
                                  "channel": "test", "port": 8999}
m.save_registry(reg)
leave_data("altbestand")
forget_marker("altbestand")
with contextlib.redirect_stdout(_io.StringIO()) as out:
    m.cmd_migrate_tenants(None)
ok("eine laufende Instanz bekommt ihr Kennzeichen nachgereicht",
   m.retained_data_tenant("altbestand") == cls_id)
ok("die Migration sagt, was sie getan hat",
   "marked with their tenant" in out.getvalue(), out.getvalue())
with contextlib.redirect_stdout(_io.StringIO()) as out2:
    m.cmd_migrate_tenants(None)
ok("und beim zweiten Lauf ist sie still", out2.getvalue().strip() == "",
   out2.getvalue())

print("")
print("Aufraeumen ist moeglich, angesagt und protokolliert")

survived, out3 = True, ""
buf = _io.StringIO()
try:
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        m.cmd_purge(argparse.Namespace(name="crm", yes=False))
except SystemExit:
    survived = False
out3 = buf.getvalue()
ok("ohne --yes wird nichts geloescht, sondern gesagt was verschwaende",
   survived and os.path.isdir(m.instance_dir("crm"))
   and "--yes" in out3 and "no undo" in out3, out3)

buf = _io.StringIO()
try:
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        m.cmd_purge(argparse.Namespace(name="altbestand", yes=True))
    refused = False
except SystemExit:
    refused = True
ok("eine INSTALLIERTE Instanz wird nicht heimlich entkernt",
   refused and os.path.isdir(m.instance_dir("altbestand")), buf.getvalue())

buf = _io.StringIO()
with contextlib.redirect_stdout(buf):
    m.cmd_purge(argparse.Namespace(name="crm", yes=True))
ok("mit --yes sind die Reste weg", not os.path.isdir(m.instance_dir("crm")),
   buf.getvalue())
log = m.read_tenant_log(default_id, limit=50)
ok("und das Loeschen steht im Log DES MANDANTEN, dem die Daten gehoerten",
   any(e.get("action") == "instance.purge" and e.get("subject") == "crm"
       for e in log),
   log)
ok("danach darf der Name neu vergeben werden",
   m.retained_data_refusal("crm", cls_id) == "")

print("")
print(f"{'FEHLER' if fails else 'Alles gruen'} - {fails} Fehlschlag(e)")
sys.exit(1 if fails else 0)
