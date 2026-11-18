#!/usr/bin/env python3
"""Hat jeder Knoten seinen Zusicherungsschluessel? (0.1.302, L18)

Fund vom 2026-11-18 auf oaapx01, Mandant bec: Eine angemeldete Person
schreibt im Chat, und jede Runde endet mit "Keine Zusicherung der
Plattform fuer diese Anfrage". identity unterschreibt die Zusicherung
(oaap.core.identity 2.11) mit dem Schluessel des Knotens -- und das
Verzeichnis war leer. Der Schluessel entstand bis dahin nur bei zwei
Anlaessen: einer Destination mit Principal Propagation und einer App,
die Rechte weitergeben darf. Ein Chat oder ein MCP-Gateway braucht ihn
genauso, loeste ihn aber nicht aus. Auf oaap-test lag er zufaellig.

Eine Liste "wer braucht ihn" war der Fehler. Verteidigt wird darum:

    Jeder Knoten hat den Schluessel -- nach der Installation, nach
    jedem Update (migrate.sh), nach jeder App-Installation.
    Ein vorhandener Schluessel wird nie ersetzt.
    `oaap status` sagt, wenn er fehlt, und gilt dann nicht als gesund.

Braucht kein Docker und keinen Knoten.

Run: python3 test/test_assertion_key.py
"""
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
DATA = tempfile.mkdtemp(prefix="oaap-assertion-key-test-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, os.path.join(ROOT, "platform"))

import appctl as m  # noqa: E402

ok_n = fail_n = 0


def ok(label, cond, detail=""):
    global ok_n, fail_n
    if cond:
        ok_n += 1
        print(f"  ok    {label}")
    else:
        fail_n += 1
        print(f"  FAIL  {label}  {detail}")


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def run_cli(*args):
    env = dict(os.environ, OAAP_DATA_DIR=DATA)
    return subprocess.run([sys.executable, os.path.join(ROOT, "platform", "appctl.py"),
                           *args], capture_output=True, text=True, env=env)


print("A  der Schluessel entsteht einmal und bleibt")
ok("der Pfad liegt unter dem Datenverzeichnis",
   m.ASSERTION_KEY_FILE == os.path.join(DATA, "data", "assertion", "key"))
ok("frischer Knoten: kein Schluessel", m.assertion_key_present() is False)
r = run_cli("assertion-key-state")
ok("Zustand ohne Schluessel heisst MISSING", r.stdout.strip() == "MISSING", r.stdout + r.stderr)

r = run_cli("migrate-assertion-key")
ok("migrate-assertion-key laeuft durch", r.returncode == 0, r.stderr)
ok("... und sagt, dass es ihn gemacht hat", "assertion key" in r.stdout, r.stdout)
ok("der Schluessel ist da", m.assertion_key_present() is True)
with open(m.ASSERTION_KEY_FILE, encoding="ascii") as f:
    first = f.read().strip()
ok("64 Hexzeichen (32 Byte)", re.fullmatch(r"[0-9a-f]{64}", first) is not None)
if os.name == "posix":
    ok("Datei 0600", (os.stat(m.ASSERTION_KEY_FILE).st_mode & 0o777) == 0o600)
    ok("Verzeichnis 0700", (os.stat(m.ASSERTION_DIR).st_mode & 0o777) == 0o700)

r = run_cli("migrate-assertion-key")
ok("zweiter Lauf: still", r.returncode == 0 and r.stdout.strip() == "", r.stdout)
with open(m.ASSERTION_KEY_FILE, encoding="ascii") as f:
    ok("zweiter Lauf: derselbe Schluessel", f.read().strip() == first)
ok("Zustand heisst present", run_cli("assertion-key-state").stdout.strip() == "present")

# Ein zu kurzer Rest (abgebrochenes Schreiben) zaehlt als fehlend und wird ersetzt.
with open(m.ASSERTION_KEY_FILE, "w", encoding="ascii") as f:
    f.write("abc\n")
ok("ein zu kurzer Schluessel zaehlt als fehlend", m.assertion_key_present() is False)
ok("... und wird neu gemacht", m.ensure_assertion_key() is True
   and m.assertion_key_present() is True)

print("B  jede Tuer zum Knoten macht ihn")
src = read("platform/appctl.py")
body = src[src.index("def _install_from_dir("):]
body = body[:body.index("\ndef ", 1)]
ok("App-Installation ruft ensure_assertion_key()", "ensure_assertion_key()" in body)
i = body.index("ensure_assertion_key()")
before = body[max(0, i - 400):i]
ok("... ohne Bedingung davor (keine Liste 'wer braucht ihn')",
   "may_grant" not in before and not re.search(r"\n\s+if [^\n]*\n\s+(and|or)?[^\n]*$", before.rstrip(" ")),
   before[-200:])
mig = read("platform/migrate.sh")
ok("migrate.sh ruft migrate-assertion-key (jedes Update)", "appctl.py\" migrate-assertion-key" in mig)
ok("install.sh ruft migrate-assertion-key (neuer Knoten)",
   "appctl.py\" migrate-assertion-key" in read("install.sh"))

print("C  oaap status sagt es")
st = read("bin/oaap")
ok("status fragt den Zustand ab", "assertion-key-state" in st)
ok("status nennt MISSING mit dem Ausweg", "Assertion key: MISSING" in st and "oaap update" in st)
ok("ein fehlender Schluessel macht den Knoten DEGRADED", '[ "$akey" != "MISSING" ]' in st)

print(f"\n{ok_n} ok, {fail_n} failed")
sys.exit(1 if fail_n else 0)
