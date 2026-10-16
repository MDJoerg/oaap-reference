"""`oaap app user delete`: was die Kommandozeile sagt, wenn Identity ablehnt.

    python3 test/test_user_delete_cli.py

Gemessen auf oaap-test (16.10.2026): Identity lehnte ab ("Es gibt noch
gueltige API-Schluessel ... zuerst entziehen"), der Befehl endete mit 1 --
aber auf stdout stand nur "This DELETES the user ..." und "The record is
gone for good", und ueber SSH kam diese gepufferte Vorwarnung NACH dem
Fehler an. Es las sich wie: abgelehnt, und dann doch geloescht. Der
Benutzer stand weiter in der Liste.

Gehalten wird hier:
1. Bei einer Ablehnung endet der Befehl ungleich 0 und sagt ausdruecklich,
   dass nichts geloescht wurde, mit dem Grund von Identity.
2. Kein Satz auf stdout behauptet vor dem Erfolg, der Datensatz SEI weg.
3. Die Vorwarnung ist geschrieben (flush), bevor Identity gefragt wird --
   sonst stellt eine Pipe sie hinter den Fehler.
4. Bei Erfolg: "User '<name>' deleted."
"""
import argparse
import io
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["OAAP_DATA_DIR"] = tempfile.mkdtemp(prefix="oaap-userdel-")
sys.path.insert(0, os.path.join(HERE, "..", "platform"))

import appctl as m                                            # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:500]}")


USER = {"username": "rc-probe", "id": "395ca1ba-8b35-49a2-99fd-644ed118a504",
        "tenant": "", "roles": ["user"]}
EVENTS = []


class Tee(io.StringIO):
    """stdout, das mitschreibt, WANN geflusht wurde -- relativ zum Aufruf
    an Identity."""
    def flush(self):
        EVENTS.append(("flush", self.getvalue()))
        super().flush()


def fake_exec(script, env=None):
    return json.dumps(USER) + "\n"


def run(answer):
    del EVENTS[:]

    def fake_delete(name):
        EVENTS.append(("identity", name))
        return answer
    m._identity_exec = fake_exec
    m.identity_user_delete = fake_delete
    m.tenant_label = lambda t: "default"
    out, err = Tee(), io.StringIO()
    old = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    code = 0
    try:
        m.cmd_user(argparse.Namespace(action="delete", username="rc-probe", yes=True))
    except SystemExit as e:
        code = e.code
    finally:
        sys.stdout, sys.stderr = old
    return code, out.getvalue(), err.getvalue()


print("")
print("1. Identity lehnt ab")
code, out, err = run((409, {"error": "Es gibt noch gültige API-Schlüssel dieses "
                                      "Benutzers (ad36b4c5) — zuerst entziehen."}))
ok("Rueckgabewert ungleich 0", code not in (0, None), code)
ok("der Fehler sagt, dass NICHTS geloescht wurde", "nothing was deleted" in err, err)
ok("… und nennt den Grund von Identity", "zuerst entziehen" in err, err)
ok("kein Satz auf stdout behauptet, der Datensatz SEI weg",
   "is gone for good" not in out and "deleted." not in out, out)
ok("die Vorwarnung nennt, wen es treffen WIRD", "rc-probe" in out and "395ca1ba" in out, out)
seen = [e[0] for e in EVENTS]
ok("die Vorwarnung ist geschrieben, bevor Identity gefragt wird",
   "flush" in seen and "identity" in seen and seen.index("flush") < seen.index("identity")
   and "rc-probe" in next(v for k, v in EVENTS if k == "flush"), EVENTS)

print("")
print("2. Identity loescht")
code, out, err = run((200, {"ok": True}))
ok("Rueckgabewert 0", code in (0, None), (code, err))
ok("Erfolg steht da, erst nach dem Aufruf", "User 'rc-probe' deleted." in out, out)
ok("kein Fehler", err == "", err)

print("")
print("ALLE PASS" if not fails else f"{fails} FAIL")
sys.exit(1 if fails else 0)
