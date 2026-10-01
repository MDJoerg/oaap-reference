#!/usr/bin/env python3
"""Einen Mandanten entfernen -- nur wenn nichts mehr darin ist (I-27).

`oaap tenant remove <kuerzel>` nimmt einen LEEREN Mandanten zurueck, so wie
`tenant create` ihn angelegt hat. Ein Mandant mit Inhalt wird nicht angefasst;
die Meldung nennt, was im Weg steht. Geprueft wird durch main(), derselbe Weg
wie an der Maschine, mit dem echten Mandantenspeicher.

Braucht kein Docker und keinen Knoten.

Aufruf: python3 test/test_tenant_remove.py
"""
import argparse
import contextlib
import io
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-tenant-remove-test-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, os.path.join(HERE, "..", "platform"))

import appctl as m                                             # noqa: E402

m.reload_gateway = lambda: None
m.zone_probe = lambda label: ""
fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:500]}")


def make(label):
    with contextlib.redirect_stdout(io.StringIO()):
        m.cmd_tenant(argparse.Namespace(
            action="create", name=label, target=None, title="Kunde",
            account="", account_name="", grace_days=30, yes=True, count=50))
    return m.tenant_by_label(label)[0]


def write_users(users):
    d = os.path.join(DATA, "data", "identity")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "users.json"), "w", encoding="utf-8") as f:
        json.dump(users, f)


def remove(label, yes=False):
    old_argv = sys.argv
    sys.argv = ["oaap-app", "tenant", "remove", label] + (["--yes"] if yes else [])
    buf, code = io.StringIO(), 0
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            m.os.geteuid = lambda: 0
            m.main()
    except SystemExit as e:
        code = e.code or 0
    finally:
        sys.argv = old_argv
    return buf.getvalue(), code


default_id = m.ensure_default_tenant()
write_users([])
empty = make("leer")
full = make("voll")
m.audit_tenant("tenant.create", empty, "leer")

print("=== ein leerer Mandant ===")
out, code = remove("leer")
ok("ohne --yes passiert nichts", code != 0 and "leer" in
   [t["label"] for t in m.load_tenants().values()], out)
ok("... und die Folgen stehen davor", "empty" in out and "--yes" in out, out)
out, code = remove("leer", yes=True)
ok("mit --yes ist er weg", code == 0 and not m.tenant_by_label("leer")[0], out)
ok("der Eintrag im Protokoll bleibt und nennt das Entfernen",
   any(e.get("action") == "tenant.remove" and e.get("subject") == "leer"
       for e in m.read_tenant_log(None, 500)))
ok("die Eintraege davor blieben", any(e.get("subject") == "leer"
   and e.get("action") == "tenant.create" for e in m.read_tenant_log(None, 500)))
out, code = remove("leer", yes=True)
ok("ein zweites Mal: es gibt ihn nicht mehr", code != 0 and "no tenant" in out, out)

print("\n=== was im Weg steht ===")
reg = m.load_registry()
reg["instances"]["voll-demo"] = {"app_id": "demo", "tenant": full,
                                 "name": "demo", "id": "voll-demo-00",
                                 "version": "1.0", "channel": "production"}
m.save_registry(reg)
out, code = remove("voll", yes=True)
ok("eine Instanz haelt ihn fest", code != 0 and "demo" in out, out)
ok("... und er ist noch da", bool(m.tenant_by_label("voll")[0]))
reg["instances"].pop("voll-demo")
m.save_registry(reg)

write_users([{"username": "anna", "tenant": full, "roles": [], "groups": []}])
out, code = remove("voll", yes=True)
ok("ein Konto haelt ihn fest", code != 0 and "1 user" in out, out)
write_users([])

os.makedirs(os.path.join(m.COHORT_DIR, full, "kurs"), exist_ok=True)
out, code = remove("voll", yes=True)
ok("eine Kohorte haelt ihn fest", code != 0 and "cohort" in out, out)
import shutil                                                   # noqa: E402
shutil.rmtree(os.path.join(m.COHORT_DIR, full))

reg = m.load_registry()
reg.setdefault("retained", {})[m.retained_key(full, "alt")] = {"id": "x"}
m.save_registry(reg)
out, code = remove("voll", yes=True)
ok("zurueckgelassene Daten halten ihn fest", code != 0 and "left behind" in out, out)
reg["retained"].clear()
m.save_registry(reg)

os.makedirs(os.path.join(m.files_tenant_dir(full), "ab"), exist_ok=True)
with open(os.path.join(m.files_tenant_dir(full), "ab", "abcdef"), "wb") as f:
    f.write(b"x")
out, code = remove("voll", yes=True)
ok("eine abgelegte Datei haelt ihn fest", code != 0 and "stored file" in out, out)

print("\n=== der Standard-Mandant ===")
out, code = remove("default", yes=True)
ok("gehoert dem Knoten selbst", code != 0 and "default tenant" in out, out)
ok("... und ist noch da", bool(m.default_tenant_id()))

print("\n=== ein nicht lesbarer Kontenspeicher zaehlt nicht als leer ===")
shutil.rmtree(m.files_tenant_dir(full))
os.makedirs(os.path.join(DATA, "data", "identity"), exist_ok=True)
with open(os.path.join(DATA, "data", "identity", "users.json"), "w") as f:
    f.write("{kaputt")
out, code = remove("voll", yes=True)
ok("Konten nicht lesbar -> nicht entfernt", code != 0 and "could not be read" in out, out)
write_users([])
out, code = remove("voll", yes=True)
ok("nach Aufraeumen geht es", code == 0 and not m.tenant_by_label("voll")[0], out)

print(f"\n{'OK' if not fails else 'FEHLER'}: {fails} Fehler")
sys.exit(1 if fails else 0)
