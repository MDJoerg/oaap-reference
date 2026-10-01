#!/usr/bin/env python3
"""Eine Instanz, vier Schreibweisen -- und kein stilles Doppelpraefix (I-12).

Das Portal zeigt NAMEN, die Registry kennt SCHLUESSEL (`<kuerzel>-<name>`).
Wer den Namen aus dem Portal in die CLI trug, bekam "no instance named";
wer bei `promote` --to als Schluessel schrieb, bekam `sgl-sgl-hvp` ohne ein
Wort. Geprueft wird:

    key, kuerzel/name, --tenant + name und der Portalname (eindeutig)
    loesen DIESELBE Instanz auf -- und der alte Aufruf bleibt gueltig.
    Ein Name, den zwei Mandanten tragen, wird nie geraten.
    Der Weg durch main() (Argumente -> Aufloesung -> Verb) ist derselbe
    wie der durch die Funktion: ein Koeder fuer "zwei Wege, eine Regel".
    `promote --to sgl-hvp` wird abgefangen, `--keep-name` erlaubt es.

Aufruf: python3 test/test_instance_ref.py
"""
import argparse
import contextlib
import io as _io
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-instance-ref-test-")
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
        print(f"      {str(detail)[:400]}")


def make_tenant(label):
    with contextlib.redirect_stdout(_io.StringIO()):
        m.cmd_tenant(argparse.Namespace(
            action="create", name=label, target=None, title="Kunde",
            account="", account_name="", grace_days=30, yes=True, count=50))
    return m.tenant_by_label(label)[0]


def put(key, tid, name, channel="production", app="demo"):
    reg = m.load_registry()
    reg["instances"][key] = {"app_id": app, "tenant": tid, "name": name,
                             "id": key[:12].ljust(12, "0"), "version": "1.0",
                             "channel": channel}
    m.save_registry(reg)


default_id = m.ensure_default_tenant()
sgl = make_tenant("sgl")
cls = make_tenant("cls")
put("sgl-hvp", sgl, "hvp")
put("sgl-hvp-test", sgl, "hvp-test", "test")
put("cls-hvp", cls, "hvp")            # derselbe Name in einem zweiten Mandanten
put("cls-viewer", cls, "viewer")
put("wegweiser", default_id, "wegweiser")
reg = m.load_registry()


def res(ref, tenant=""):
    return m.resolve_instance_ref(reg, ref, tenant)


print("=== vier Schreibweisen, eine Instanz ===")
ok("der Schluessel (wie bisher)", res("cls-viewer") == ("cls-viewer", ""))
ok("kuerzel/name", res("cls/viewer") == ("cls-viewer", ""), res("cls/viewer"))
ok("--tenant + Name", res("viewer", "cls") == ("cls-viewer", ""))
ok("der Portalname, wenn er nur einmal vorkommt",
   res("viewer") == ("cls-viewer", ""))
ok("ein Schluessel mit --tenant, der zu diesem Mandanten gehoert",
   res("cls-viewer", "cls") == ("cls-viewer", ""))
ok("die Instanz des Standard-Mandanten bleibt unter dem blanken Namen",
   res("wegweiser") == ("wegweiser", "") and res("default/wegweiser")[0] == "wegweiser")

print("\n=== nichts wird geraten ===")
key, err = res("hvp")
ok("ein Name, den zwei Mandanten tragen, wird abgelehnt",
   not key and "more than one" in err, err)
ok("... und beide Schluessel stehen in der Meldung",
   "sgl-hvp" in err and "cls-hvp" in err, err)
ok("kuerzel/name loest ihn trotzdem auf",
   res("sgl/hvp")[0] == "sgl-hvp" and res("hvp", "cls")[0] == "cls-hvp")
key, err = res("gibtsnicht", "cls")
ok("ein falscher Name im Mandanten nennt, was der Mandant hat",
   not key and "viewer" in err and "hvp" in err, err)
key, err = res("viewer", "gibtsnicht")
ok("ein falscher Mandant wird benannt", "no tenant" in err, err)
ok("ein unbekannter blanker Name bleibt die bekannte Meldung",
   res("gibtsnicht") == ("", "no instance named 'gibtsnicht'"))
ok("a/b/c ist kein Name", bool(res("a/b/c")[1]))

print("\n=== der Weg durch die Argumente (derselbe wie die Funktion) ===")
seen = []
real = {v: getattr(m, "cmd_" + v, None) for v in ("restart",)}


def spy(args):
    seen.append(args.name)


def argv_run(argv):
    """main() mit gefangenem Verb: was kaeme beim Verb an?"""
    old_argv, old_fn = sys.argv, m.cmd_restart
    m.cmd_restart = spy
    sys.argv = ["oaap-app"] + argv
    buf = _io.StringIO()
    code = 0
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            m.os.geteuid = lambda: 0
            m.main()
    except SystemExit as e:
        code = e.code or 0
    finally:
        sys.argv, m.cmd_restart = old_argv, old_fn
    return buf.getvalue(), code


for argv in (["restart", "cls-viewer"], ["restart", "cls/viewer"],
             ["restart", "viewer", "--tenant", "cls"], ["restart", "viewer"]):
    seen.clear()
    out, code = argv_run(argv)
    ok(f"oaap app {' '.join(argv)} -> cls-viewer", seen == ["cls-viewer"],
       (seen, out))
seen.clear()
out, code = argv_run(["restart", "hvp"])
ok("der doppelte Name stoppt vor dem Verb", not seen and code != 0
   and "more than one" in out, (seen, out))
seen.clear()
out, code = argv_run(["restart", "gibtsnicht"])
ok("ein unbekannter Name erreicht das Verb unveraendert (es sagt es selbst)",
   seen == ["gibtsnicht"], (seen, out))

print("\n=== promote: --to ist ein NAME, das erste Argument ein SCHLUESSEL ===")
m.promotion_review = lambda reg, src, tgt: (_ for _ in ()).throw(
    m.PromotionRefused("REACHED"))
reg = m.load_registry()


def promote(**kw):
    a = argparse.Namespace(name="sgl-hvp-test", to="", confirm=False,
                           keep_name=False)
    for k, v in kw.items():
        setattr(a, k, v)
    buf = _io.StringIO()
    code = 0
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            m.cmd_promote(a)
    except SystemExit as e:
        code = e.code or 0
    return buf.getvalue(), code


out, code = promote(to="sgl-hvp")
ok("--to mit dem Mandantenpraefix wird nicht still ausgefuehrt",
   code != 0 and "REACHED" not in out, out)
ok("die Meldung nennt den Schluessel, der entstuende",
   "sgl-sgl-hvp" in out, out)
ok("... und die richtige Schreibweise", "--to hvp" in out, out)
ok("... und den Ausweg", "--keep-name" in out, out)
out, code = promote(to="sgl-hvp", keep_name=True)
ok("--keep-name laesst einen solchen Namen zu", "REACHED" in out, out)
out, code = promote(to="hvp")
ok("der ordentliche Name laeuft durch", "REACHED" in out, out)
out, code = promote()
ok("ohne --to bleibt es beim Abschneiden von -test", "REACHED" in out, out)
out, code = promote(to="sglx-viewer")
ok("ein Name, der nur AEHNLICH beginnt, ist kein Fall",
   "REACHED" in out, out)

print(f"\n{'OK' if not fails else 'FEHLER'}: {fails} Fehler")
sys.exit(1 if fails else 0)
