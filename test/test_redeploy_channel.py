#!/usr/bin/env python3
"""Ein erneutes Ausrollen behaelt den Kanal der Instanz (0.1.110).

Der Befund (Eintrag 140, mir selbst passiert): `--channel` hatte die
Vorgabe `production`, und die Vorgabe galt auch fuer ein REDEPLOY. Ein
`oaap app install ./paket.zip --name studio` gegen eine TEST-Instanz
machte sie damit still produktiv -- mitsamt "Artifact grants dropped"
und dem Deploy-Token. Zurueck ging es nicht: dieselbe Version auf den
Test-Kanal scheiterte an der Versionsregel, und ein `demote` gibt es
nicht.

Die Gestalt des Fehlers ist die von 0.1.109: der Portal-Weg und der
Rollback-Weg erbten den Kanal laengst richtig, die CLI stand allein
dagegen. Deshalb fragen jetzt BEIDE CLI-Pfade dieselbe Funktion, statt
`args.channel` selbst zu lesen.

Aufruf: python3 test/test_redeploy_channel.py
"""
import argparse
import contextlib
import io
import os
import re
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-channel-test-")
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
        print(f"      {str(detail)[:500]}")


def resolved(inst, requested):
    """Der Kanal UND was dabei gesagt wurde -- beides gehoert zur Regel.
    Ein lautloser Wechsel waere genau der Fehler noch einmal."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ch = m.resolve_channel(inst, requested, "studio")
    return ch, buf.getvalue()


TEST = {"channel": "test"}
PROD = {"channel": "production"}

print("Eine NEUE Instanz nimmt die Vorgabe")
ch, said = resolved(None, None)
ok("ohne Angabe: production, wie dokumentiert", ch == "production", ch)
ok("und ohne Wortmeldung, denn nichts wird bewegt", said == "", said)
ok("mit --channel test: test", resolved(None, "test")[0] == "test")

print("")
print("Ein REDEPLOY behaelt, was die Instanz ist")
ch, said = resolved(TEST, None)
ok("eine Test-Instanz bleibt Test-Instanz", ch == "test", ch)
ok("und zwar still -- es aendert sich nichts", said == "", said)
ok("eine Produktiv-Instanz bleibt produktiv",
   resolved(PROD, None)[0] == "production")
ok("den eigenen Kanal ausdruecklich zu nennen ist kein Wechsel",
   resolved(TEST, "test") == ("test", ""), resolved(TEST, "test"))

print("")
print("Ein Wechsel bleibt moeglich -- und sagt, was er kostet")
ch, said = resolved(TEST, "production")
ok("--channel production hebt eine Test-Instanz", ch == "production", ch)
ok("und nennt Deploy-Token und Artifact-Grants beim Namen",
   "deploy token" in said and "grant" in said, said)
ch, said = resolved(PROD, "test")
ok("--channel test fuehrt zurueck", ch == "test", ch)
ok("und sagt, dass die Instanz damit wieder in sich selbst ausrollbar ist",
   "same version" in said and "redeployable" in said, said)

print("")
print("Die Vorgabe des Schalters selbst")

# Der Kern des Fixes, und er laesst sich nur am Text pruefen: `main()`
# baut den Parser und verlangt im selben Atemzug root, laesst sich hier
# also nicht aufrufen. Geprueft wird deshalb die Zeile -- denn mit
# default="production" ist "nicht angegeben" von "ausdruecklich
# production" nicht mehr zu unterscheiden, und dann kann die Aufloesung
# oben den Unterschied gar nicht mehr sehen.
src = open(os.path.join(HERE, "..", "platform", "appctl.py"),
           encoding="utf-8").read()
flag = re.search(r'add_argument\("--channel".*?\)', src, re.S)
ok("'--channel' hat ueberhaupt eine Vorgabe-Angabe", bool(flag))
ok("und sie ist None, nicht 'production'",
   bool(flag) and "default=None" in flag.group(0),
   flag.group(0) if flag else "")

print("")
print("Der Weg zurueck ist wieder offen (die Versionsregel)")

# Die Versionsregel schuetzt PRODUKTION davor, dieselbe Version mit
# anderen Bytes ueberschrieben zu bekommen. Sie fragte nach dem Kanal,
# den die Instanz HAT -- und sperrte damit genau den Befehl, der sie da
# wieder herausholt. Jetzt fragt sie nach dem Kanal, auf dem die
# Instanz LANDET.
default_id = m.ensure_default_tenant()
reg = m.load_registry()
reg["instances"]["demo"] = {
    "app_id": "demo", "app_name": "Demo", "version": "0.1.0",
    "channel": "production", "port": 8991, "svc_port": 80,
    "container": "oaap-app-demo", "tenant": default_id,
    "name": "demo", "id": m.new_instance_id(),
    "routes": [{"path": "/", "roles": ["user"]}]}
m.save_registry(reg)

MANIFEST = "\n".join([
    'oaap_manifest: "0.1"',
    "app:",
    "  id: demo",
    "  name: Demo",
    "  version: 0.1.0",
    "  type: native",
    "services:",
    "  web:",
    "    build: .",
    "    port: 80",
    "routes:",
    "  - path: /",
    "    roles: [user]",
    "health:",
    "  path: /healthz",
    "",
])
pkg = tempfile.mkdtemp(prefix="oaap-channel-pkg-")
with open(os.path.join(pkg, "oaap-app.yaml"), "w", encoding="utf-8") as f:
    f.write(MANIFEST)


class GotPast(Exception):
    """Die Versionsregel hat durchgelassen. Weiter interessiert hier
    nichts -- dahinter beginnt Docker, und geprueft wird die Regel."""


def _stop(_inst):
    raise GotPast()


m.is_rehearsal = _stop            # der naechste Griff nach der Regel


def install(channel):
    args = argparse.Namespace(package=pkg, path="", ref="", name="demo",
                              channel=channel, store_source="", tenant="",
                              key="", ident=None, rehearsal=None, bind=[])
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            m._install_from_dir(pkg, args, {"kind": "local", "url": pkg,
                                            "path": ""})
        return "through", buf.getvalue()
    except GotPast:
        return "past-the-rule", buf.getvalue()
    except SystemExit:
        return "refused", buf.getvalue()


outcome, said = install(None)
ok("dieselbe Version noch einmal nach produktiv bleibt abgelehnt",
   outcome == "refused" and "bump the version" in said, (outcome, said))

outcome, said = install("test")
ok("dieselbe Version auf den Test-Kanal kommt durch",
   outcome == "past-the-rule", (outcome, said))
ok("und der Wechsel wird angesagt, nicht stillschweigend vollzogen",
   "NOTE" in said and "test channel" in said, said)

print("")
print("Der zweite CLI-Pfad (hochgeladenes Paket) fragt dieselbe Stelle")

# `oaap app install ./paket.zip` kehrt frueher zurueck und rief
# install_artifact bis 0.1.109 mit `args.channel` auf. Genau dieser
# Aufruf war der, der mir das Studio verschoben hat.
reg = m.load_registry()
reg["instances"]["demo"]["channel"] = "test"
m.save_registry(reg)

zpath = os.path.join(pkg, "demo.zip")
with zipfile.ZipFile(zpath, "w") as z:
    z.writestr("oaap-app.yaml", MANIFEST)

seen = {}


def _capture(name, zip_path, grant, channel="test", **kw):
    seen.update(name=name, channel=channel)
    return "0.1.0", "0" * 64


m.install_artifact = _capture


def zip_install(channel):
    seen.clear()
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            m.cmd_install(argparse.Namespace(
                package=zpath, path="", ref="", name="demo",
                channel=channel, store_source="", tenant="", key="",
                bind=[]))
    except SystemExit:
        pass
    return dict(seen), buf.getvalue()

got, said = zip_install(None)
ok("eine Test-Instanz bleibt auch auf diesem Weg eine Test-Instanz",
   got.get("channel") == "test", (got, said))

got, said = zip_install("production")
ok("und wer hier ausdruecklich hebt, hebt sie -- laut",
   got.get("channel") == "production" and "NOTE" in said, (got, said))

print("")
print(f"{'FEHLER' if fails else 'Alles gruen'} - {fails} Fehlschlag(e)")
sys.exit(1 if fails else 0)
