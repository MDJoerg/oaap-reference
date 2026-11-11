#!/usr/bin/env python3
"""`oaap help` und die erzeugte CLI-Referenz (RFC-0078 Stufe 0, 0.1.248).

Die handgeschriebene CLI-Referenz in oaap-docs war gegen 0.1.42 geprueft,
die Plattform stand bei 0.1.247 -- eine zweite Beschreibung der Befehle
laeuft auseinander, sobald niemand sie zaehlt. Deshalb liest
`oaap help --markdown` den Parser, den `main()` benutzt, und die
sudo-Angabe aus demselben `is_read_only()`, das abweist.

Was argparse nicht wissen kann, steht in appctl.py als zwei Listen:
`WRAPPER_GROUPS` (Befehle, die bin/oaap unter eigenem Namen weiterreicht)
und `WRAPPER_OWN` (Befehle, die bin/oaap selbst ausfuehrt). Genau diese
Wiederholung zaehlt diese Datei gegen die `case`-Liste von bin/oaap.

Geprueft:
1. Beide Listen decken bin/oaap genau ab -- nicht mehr, nicht weniger.
2. Jeder nicht-interne Befehl hat einen Hilfetext.
3. Die sudo-Angabe stammt aus `is_read_only` (Koeder an beiden Seiten).
4. Die erzeugten Dateien: Kopf nach RFC-0078 §4, eindeutige ids,
   verwaiste erzeugte Dateien verschwinden, fremde bleiben, zweimal
   erzeugt ist zweimal dasselbe.

Braucht PyYAML (wie appctl.py), kein Docker, kein Netz, keinen Knoten.

Aufruf: python3 test/test_cli_reference.py
"""
import argparse
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PLATFORM = os.path.join(HERE, "..", "platform")
WRAPPER = os.path.join(HERE, "..", "bin", "oaap")
sys.path.insert(0, PLATFORM)
sys.path.insert(0, os.path.join(PLATFORM, "services"))

import appctl  # noqa: E402
import cli_reference  # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:400]}")


SRC = open(WRAPPER, encoding="utf-8").read()
case = dict(re.findall(r'^\s{2}([a-z][a-z-]*)\)\s+(.*?);;', SRC, re.M))
passthrough = {name for name, body in case.items()
               if re.fullmatch(r'exec python3 "\$APP_DIR/appctl\.py" '
                               + re.escape(name) + r' "\$@"\s*', body)}
own = set(case) - passthrough - {"app"}

# --- 1. die beiden Listen gegen bin/oaap ---------------------------------
ok("WRAPPER_GROUPS = was bin/oaap unter eigenem Namen weiterreicht",
   set(appctl.WRAPPER_GROUPS) == passthrough,
   f"nur in appctl: {set(appctl.WRAPPER_GROUPS) - passthrough}, "
   f"nur in bin/oaap: {passthrough - set(appctl.WRAPPER_GROUPS)}")
ok("WRAPPER_OWN = was bin/oaap selbst ausfuehrt",
   set(appctl.WRAPPER_OWN) == own,
   f"nur in appctl: {set(appctl.WRAPPER_OWN) - own}, "
   f"nur in bin/oaap: {own - set(appctl.WRAPPER_OWN)}")
ok("'oaap help' steht in bin/oaap und in seiner Hilfe-Zeile",
   "help" in passthrough and "help [--markdown DIR]" in SRC)

parser = appctl.build_parser()
cmds = cli_reference.commands(parser, appctl.is_read_only,
                              appctl.WRAPPER_GROUPS)
by = {c.name: c for c in cmds}

ok("jede weitergereichte Gruppe ist ein Befehl von appctl.py",
   passthrough <= set(by), passthrough - set(by))

# --- 2. Hilfetexte ---------------------------------------------------------
missing = sorted(c.name for c in cmds if not c.internal and not c.help.strip())
ok("jeder nicht-interne Befehl hat einen Hilfetext", not missing, missing)
ok("die internen sind als 'internal:' markiert",
   all(by[n].internal for n in ("process-deploys", "restore-instances",
                                "migrate-instance-dirs", "egress-faults")))

# --- 3. sudo aus is_read_only ----------------------------------------------
gate = {c.name: dict(c.gates) for c in cmds}
ok("tenant list ohne sudo, tenant create mit", gate["tenant"]["list"] is True
   and gate["tenant"]["create"] is False, gate["tenant"])
ok("install braucht sudo", by["install"].root is True)
ok("help braucht kein sudo", by["help"].root is False
   and appctl.is_read_only(argparse.Namespace(cmd="help", action=None)))
ok("data: status lesend, drop nicht", gate["data"]["status"] is True
   and gate["data"]["drop"] is False)
ok("gemischter Befehl heisst 'je nach Aktion'", by["tenant"].root is None)

# Koeder: ein is_read_only, das alles erlaubt, muss sich in der Ausgabe
# zeigen -- sonst liest der Generator die Regel nicht, sondern raet sie.
loose = cli_reference.commands(parser, lambda a: True, appctl.WRAPPER_GROUPS)
ok("die sudo-Angabe folgt der uebergebenen Regel",
   all(c.root is False for c in loose))

ok("Audience: nur-sudo -> server_admin, lesend -> plus support",
   by["install"].audience == ["server_admin"]
   and by["tenant"].audience == ["server_admin", "support"])
ok("Formen: 'oaap tenant' und 'oaap app install'",
   by["tenant"].form == "oaap tenant"
   and by["install"].form == "oaap app install"
   and by["install"].id == "cli/app/install")

# --- 4. die Dateien ----------------------------------------------------------
with tempfile.TemporaryDirectory() as d:
    stale = os.path.join(d, "app-alt.md")
    foreign = os.path.join(d, "notiz.md")
    with open(stale, "w", encoding="utf-8") as f:
        f.write("---\nid: cli/app/alt\n---\n" + cli_reference.MARKER + "\n")
    with open(foreign, "w", encoding="utf-8") as f:
        f.write("# von Hand\n")
    written = cli_reference.write_markdown(d, cmds, "9.9.9",
                                           appctl.WRAPPER_OWN,
                                           today="2026-11-11")
    ok("README + eine Datei je Befehl", len(written) == len(cmds) + 1,
       len(written))
    ok("verwaiste erzeugte Datei entfernt", not os.path.exists(stale))
    ok("fremde Datei bleibt", os.path.exists(foreign))

    ids, bad = [], []
    for name in written:
        text = open(os.path.join(d, name), encoding="utf-8").read()
        m = re.match(r"---\n(.*?)\n---\n", text, re.S)
        if not m:
            bad.append(name)
            continue
        fm = dict(line.split(": ", 1) for line in m.group(1).splitlines())
        ids.append(fm.get("id"))
        if (fm.get("checked_against") != '"9.9.9"'
                or fm.get("checked_on") != "2026-11-11"
                or not fm.get("audience", "").startswith("[server_admin")
                or cli_reference.MARKER not in text):
            bad.append(name)
    ok("jeder Kopf vollstaendig (id, audience, checked_against/on, Marke)",
       not bad, bad[:5])
    ok("ids eindeutig", len(ids) == len(set(ids)))

    t = open(os.path.join(d, "app-install.md"), encoding="utf-8").read()
    ok("install: Aufruf beginnt mit 'oaap app install', ohne -h",
       "```sh\noaap app install" in t and "[-h]" not in t, t[:600])
    ok("install: --bind als wiederholbar", "repeatable" in t)
    t = open(os.path.join(d, "tenant.md"), encoding="utf-8").read()
    ok("tenant: Aktionstabelle mit list=no, create=yes",
       "| `list` | no |" in t and "| `create` | yes |" in t)
    ok("keine Farbcodes in den Dateien", "\x1b[" not in t)
    r = open(os.path.join(d, "README.md"), encoding="utf-8").read()
    ok("README verlinkt jeden nicht-internen Befehl",
       all(f"]({c.filename})" in r for c in cmds))
    ok("README nennt die Befehle des Wrappers",
       all(usage in r for usage, _r, _t in appctl.WRAPPER_OWN.values()))

    before = {n: open(os.path.join(d, n), encoding="utf-8").read() for n in written}
    cli_reference.write_markdown(d, cmds, "9.9.9", appctl.WRAPPER_OWN,
                                 today="2026-11-11")
    after = {n: open(os.path.join(d, n), encoding="utf-8").read() for n in written}
    ok("zweimal erzeugt ist zweimal dasselbe", before == after)

text = cli_reference.overview_text(cmds, "9.9.9", appctl.WRAPPER_OWN)
ok("Uebersicht nennt keine internen Befehle",
   "migrate-tenants" not in text and "oaap app install" in text)
ok("Uebersicht: keine Zeile breiter als 80 Zeichen",
   max(len(line) for line in text.splitlines()) <= 80)

print("")
print(f"{'ALLE PRUEFUNGEN BESTANDEN' if not fails else str(fails) + ' FEHLGESCHLAGEN'}")
sys.exit(1 if fails else 0)
