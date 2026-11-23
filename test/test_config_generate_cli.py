#!/usr/bin/env python3
"""Erzeugte Werte auf der Kommandozeile (0.1.359, runtime 2.8 / 0.2.82): `oaap app config set <i> <KEY> --generate`.

Der Befund (BookStack Stufe 1, oaap-test, 23.11.): `oaap app install` auf der
Kommandozeile liess die Schluessel mit `generate: token` leer, und die
Kommandozeile hatte keinen Weg, sie zu erzeugen -- nur das Portal (Knopf)
und der Aufbau (`app.config generate: true`). Jetzt dieselbe Regel wie im
Aufbau (runtime 2.8, RFC-0079 D7), und `app install` nennt die leeren.

Die Saetze, die diese Datei verteidigt:

    --generate erzeugt die Art, die das Manifest erklaert, in einen LEEREN Schluessel.
    Ein gesetzter Schluessel wird nie ersetzt.
    Ein Schluessel ohne `generate:` wird nicht erzeugt.
    Der Wert steht weder in der Ausgabe noch im Deploy-Protokoll.
    --generate nimmt keinen Wert, kein --append, und gehoert zu `set`.
    `app install` nennt die leeren erzeugbaren Schluessel samt Befehl.

Aufruf: python3 test/test_config_generate_cli.py
"""
import contextlib
import io
import os
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["OAAP_DATA_DIR"] = tempfile.mkdtemp(prefix="oaap-config-generate-")
sys.path.insert(0, os.path.join(HERE, "..", "platform"))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services"))

import appctl as a                                            # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:400]}")


STORE = {"APP_SECRET": "", "DB_PW": "schon-da", "PUSH_KEY": "", "SUPPLIER_KEY": ""}
GEN = {"APP_SECRET": "token", "DB_PW": "token", "PUSH_KEY": "vapid"}
APPLIED, AUDITED = [], []
a.load_registry = lambda: {"instances": {"v-wiki": {"version": "0.2.0"}}}
a.config_entries = lambda key, inst: [
    {"key": k, "value": v, "secret": True, "label": k, "default": "",
     "generate": GEN.get(k, "")} for k, v in STORE.items()]


def _apply(key, inst, values):
    APPLIED.append((key, dict(values)))
    STORE.update(values)
    return ",".join(values)


a.apply_config = _apply
a.audit_deploy = lambda entry: AUDITED.append(entry)


def cli(*argv, value=None, append=False, generate=True, action="set"):
    args = types.SimpleNamespace(action=action, name="v-wiki", key=argv[0] if argv else None,
                                 value=value, append=append, generate=generate)
    out = io.StringIO()
    code = 0
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        try:
            a.cmd_config(args)
        except SystemExit as e:
            code = e.code or 0
    return code, out.getvalue()


print("--generate in einen leeren Schluessel")
code, out = cli("APP_SECRET")
val = STORE["APP_SECRET"]
ok("erzeugt und setzt", code == 0 and APPLIED and APPLIED[-1][0] == "v-wiki" and len(val) >= 40, (code, out, val))
ok("Alphabet url-sicher (runtime 2.8)", val and all(c.isalnum() or c in "-_" for c in val), val)
ok("Wert nicht in der Ausgabe", val not in out, out)
ok("Wert nicht im Deploy-Protokoll, Art schon", AUDITED and val not in str(AUDITED[-1])
   and "generated: token" in AUDITED[-1]["message"] and AUDITED[-1]["via"] == "cli", AUDITED)
code, out = cli("PUSH_KEY")
ok("vapid erzeugt die Art des Manifests", code == 0 and STORE["PUSH_KEY"] and
   "generated: vapid" in AUDITED[-1]["message"], (out, AUDITED[-1]))

print("")
print("Nie ersetzen, nie raten")
n = len(APPLIED)
code, out = cli("DB_PW")
ok("gesetzter Schluessel bleibt", code == 0 and STORE["DB_PW"] == "schon-da" and len(APPLIED) == n
   and "never replaced" in out, out)
first = STORE["APP_SECRET"]
code, out = cli("APP_SECRET")
ok("ein zweites --generate ersetzt den erzeugten nicht", STORE["APP_SECRET"] == first and len(APPLIED) == n, out)
code, out = cli("SUPPLIER_KEY")
ok("Schluessel ohne generate: abgelehnt", code != 0 and STORE["SUPPLIER_KEY"] == "" and "generate" in out, out)
code, out = cli("GIBTSNICHT")
ok("unbekannter Schluessel abgelehnt", code != 0 and len(APPLIED) == n, out)
code, out = cli("SUPPLIER_KEY", value="x")
ok("--generate mit Wert abgelehnt", code != 0 and STORE["SUPPLIER_KEY"] == "", out)
code, out = cli("SUPPLIER_KEY", append=True)
ok("--generate mit --append abgelehnt", code != 0, out)
code, out = cli("APP_SECRET", action="unset")
ok("--generate bei unset abgelehnt", code != 0 and STORE["APP_SECRET"] == first, out)

print("")
print("app install nennt die leeren")
STORE.update({"APP_SECRET": "", "PUSH_KEY": ""})
keys = a.empty_generatable("v-wiki", {})
ok("leere erzeugbare Schluessel in Reihenfolge, gesetzte und fremde nicht",
   keys == [("APP_SECRET", "token"), ("PUSH_KEY", "vapid")], keys)
lines = a.generate_hint_lines("v-wiki", keys)
ok("Hinweis mit je einem Befehl", any("APP_SECRET (token)" in ln for ln in lines)
   and "sudo oaap app config set v-wiki PUSH_KEY --generate" in "\n".join(lines), lines)
ok("nichts leer -> kein Hinweis", a.generate_hint_lines("v-wiki", []) == [])

print("")
print("FAILED" if fails else "OK", f"({fails} failed)")
sys.exit(1 if fails else 0)
