#!/usr/bin/env python3
"""`oaap user add --tenant <Kuerzel>` nimmt ein Kuerzel (2026-10-18).

Gefunden beim ersten Mandanten, der aus einem Profil aufgebaut wurde: der
Aufbau legte den Mandanten `vuebung` an, und der naechste Befehl
`oaap user add vverwalter --tenant vuebung` antwortete "this node has no
tenant 'vuebung'". `machine add`, `cohort` und `data twin` konnten ein
Kuerzel schon (resolve_tenant_arg); `user add` rief das aeltere
resolve_tenant() auf, das nur Kennungen kennt -- derselbe Fehler, den
`machine add` am 01.10. schon einmal hatte, an einer zweiten Tuer.

Der Test geht durch main(), wie an der Maschine, und faengt erst die
Anfrage an den Identity-Dienst ab: er prueft, in welchem Mandanten das
Konto angelegt WUERDE.

Aufruf: python3 test/test_user_add_tenant_label.py
"""
import contextlib
import io
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-user-add-label-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, os.path.join(HERE, "..", "platform"))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services"))

import appctl as m                                             # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:500]}")


TID = "t-verein-0001"
os.makedirs(os.path.dirname(m.TENANTS_FILE), exist_ok=True)
with open(m.TENANTS_FILE, "w", encoding="utf-8") as f:
    json.dump({"tenants": {TID: {"label": "vuebung", "name": "Verein Uebung"}}}, f)

seen = []


def fake_create(body):
    seen.append(dict(body))
    return 201, {"id": "u-1"}


m.identity_user_create = fake_create
m.os.geteuid = lambda: 0
PWFILE = os.path.join(DATA, "pw")
with open(PWFILE, "w", encoding="utf-8") as f:
    f.write("Probe-Passwort-1\n")


def cli(*argv):
    old = sys.argv
    sys.argv = ["oaap-app", "user", "add"] + list(argv)
    buf, code = io.StringIO(), 0
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            m.main()
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    finally:
        sys.argv = old
    return buf.getvalue(), code


print("Kuerzel und Kennung")
out, code = cli("vverwalter", "--tenant", "vuebung", "--password-file", PWFILE)
ok("ein Kuerzel wird angenommen", code == 0 and len(seen) == 1, (out, code))
ok("das Konto liegt im Mandanten mit diesem Kuerzel",
   bool(seen) and seen[-1]["tenant"] == TID, seen)
n = len(seen)
out, code = cli("vtrainer", "--tenant", TID, "--password-file", PWFILE)
ok("die Kennung geht weiterhin", code == 0 and len(seen) == n + 1
   and seen[-1]["tenant"] == TID, (out, code))

print("Koeder: ein unbekanntes Kuerzel")
n = len(seen)
out, code = cli("vbereich", "--tenant", "gibtsnicht", "--password-file", PWFILE)
ok("wird abgelehnt, und es wird nichts angelegt", code != 0 and len(seen) == n
   and "gibtsnicht" in out, (out, code))

print("\nALLE PRUEFUNGEN BESTANDEN" if not fails else f"\n{fails} PRUEFUNG(EN) FEHLGESCHLAGEN")
sys.exit(1 if fails else 0)
