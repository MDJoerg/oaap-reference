#!/usr/bin/env python3
"""Die Rahmenregel auf jedem Weg (I-43, 0.1.250).

Der Befund (09.11., Keycloak 0.1.7 auf oaap-test): Eine Installation aus
einem VERZEICHNIS oder einer Git-Adresse verglich das Paket nie mit dem,
was die Instanz schon darf. Eine neue oeffentliche Route ging auch in
Produktion ohne ein Wort durch -- der ZIP-Weg, die Uebernahme, das
Sideloading und das Deploy-Token fragten laengst. Dazu: Bei einer
Test-Instanz druckte der Knoten die Erweiterung als NOTE, und der Worker
des Portals warf genau diese Zeilen weg ("deployed").

Die Saetze, die diese Datei verteidigt:

    Verzeichnis/Git nach Produktion: eine Erweiterung braucht --confirm.
    Verzeichnis/Git auf Test: sie wird angesagt, nicht verweigert.
    Der Portal-Text traegt die Erweiterung, und eine Ablehnung nennt
    einen Weg, den man im Browser auch gehen kann.

Aufruf: python3 test/test_envelope_paths.py
"""
import argparse
import contextlib
import io
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-envelope-paths-")
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


class GotPast(Exception):
    """Die Rahmenregel hat durchgelassen; dahinter beginnt Docker."""


def _stop(_inst):
    raise GotPast()


m.is_rehearsal = _stop            # der naechste Griff nach der Regel

default_id = m.ensure_default_tenant()


def put_instance(channel, version="0.1.0"):
    reg = m.load_registry()
    reg["instances"]["demo"] = {
        "app_id": "demo", "app_name": "Demo", "version": version,
        "channel": channel, "port": 8991, "svc_port": 80,
        "container": "oaap-app-demo", "tenant": default_id,
        "name": "demo", "id": "i-demo",
        "routes": [{"path": "/", "roles": ["user"]}]}
    m.save_registry(reg)


def package(version, public=False):
    lines = ['oaap_manifest: "0.1"', "app:", "  id: demo", "  name: Demo",
             f"  version: {version}", "  type: native", "services:", "  web:",
             "    build: .", "    port: 80", "routes:", "  - path: /",
             "    roles: [user]"]
    if public:
        lines += ["  - path: /fan", "    roles: [public]"]
    lines += ["health:", "  path: /healthz", ""]
    d = tempfile.mkdtemp(prefix="oaap-envelope-pkg-")
    with open(os.path.join(d, "oaap-app.yaml"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return d


def install(pkg, confirm=False, **extra):
    args = argparse.Namespace(package=pkg, path="", ref="", name="demo",
                              channel=None, store_source="", tenant="",
                              key="", ident=None, rehearsal=None, bind=[],
                              confirm=confirm, **extra)
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            m._install_from_dir(pkg, args, {"kind": "local", "url": pkg, "path": ""})
        return "through", buf.getvalue()
    except GotPast:
        return "past", buf.getvalue()
    except SystemExit:
        return "refused", buf.getvalue()


print("Verzeichnis nach PRODUKTION")
put_instance("production")
out, said = install(package("0.2.0", public=True))
ok("eine neue oeffentliche Route ohne --confirm: abgelehnt",
   out == "refused" and m.ENVELOPE_REFUSAL in said, (out, said))
ok("und die Erweiterung steht als NOTE da",
   "NOTE: routes become reachable without login: /fan" in said, said)
out, said = install(package("0.2.0", public=True), confirm=True)
ok("mit --confirm: durch", out == "past", (out, said))
out, said = install(package("0.2.0"))
ok("ohne Erweiterung: durch, ohne Bestaetigung", out == "past", (out, said))
ok("und ohne NOTE", "NOTE" not in said, said)

print("")
print("Verzeichnis auf TEST")
put_instance("test")
out, said = install(package("0.1.0", public=True))
ok("die Erweiterung wird angesagt, nicht verweigert",
   out == "past" and "/fan" in said, (out, said))
ok("dieselbe Version auf Test erzeugt keinen Versions-Satz",
   "already installed" not in said, said)

print("")
print("Was das Portal davon sieht")
sfx = m.envelope_notes_suffix("Fetching\nNOTE: routes become reachable without login: /fan\nok\n")
ok("die NOTE-Zeilen werden zum Zusatz der Meldung",
   sfx == " (routes become reachable without login: /fan)", sfx)
ok("ohne NOTE kein Zusatz", m.envelope_notes_suffix("ok\n") == "")
src = open(os.path.join(HERE, "..", "platform", "appctl.py"), encoding="utf-8").read()
ok("der Worker haengt den Zusatz an 'deployed'",
   '"deployed" + envelope_notes_suffix(buf.getvalue())' in src)
ok("eine Ablehnung im Portal nennt den Weg ueber Test und Uebernahme",
   "update a test instance and promote it" in src
   and "confirm it with the tick on the store page" in src)

print("")
print("I-43 Haken im Portal: der Fingerabdruck genau dieser Erweiterung")
put_instance("production")
out, said = install(package("0.2.0", public=True))
fpl = [ln for ln in said.splitlines() if ln.startswith(m.ENVELOPE_FP_LINE)]
fp = fpl[0][len(m.ENVELOPE_FP_LINE):] if fpl else ""
ok("die Ablehnung traegt den Fingerabdruck", out == "refused" and len(fp) == 16, said)
ok("er ist der Fingerabdruck der NOTE-Zeilen",
   fp == m.envelope_fingerprint(["routes become reachable without login: /fan"]), fp)
out, said = install(package("0.2.0", public=True), confirm=fp)
ok("mit dem passenden Fingerabdruck: durch", out == "past", (out, said))
out, said = install(package("0.2.0", public=True), confirm="0" * 16)
ok("mit einem anderen Fingerabdruck: abgelehnt (der Haken galt etwas anderem)",
   out == "refused", (out, said))
out, said = install(package("0.2.0", public=True), confirm="")
ok("leerer Haken: abgelehnt", out == "refused", (out, said))
ok("die Reihenfolge der Zeilen aendert den Fingerabdruck nicht",
   m.envelope_fingerprint(["a", "b"]) == m.envelope_fingerprint(["b", "a"]))
ok("der Worker reicht den Haken nur als 16 Hex-Zeichen weiter",
   're.fullmatch(r"[0-9a-f]{16}", want)' in src)
ok("der Worker meldet needs=confirm_envelope mit Fingerabdruck und Zeilen",
   'res_body["needs"] = "confirm_envelope"' in src and 'res_body["notes"]' in src)
psrc = open(os.path.join(HERE, "..", "platform", "services", "portal", "app.py"),
            encoding="utf-8").read()
ok("das Portal fragt auf der Store-Seite, nur bei einem Update",
   'res.get("needs") == "confirm_envelope" and update' in psrc
   and 'name="confirm_envelope"' in psrc and "{% for n in envelope.notes %}" in psrc)

print("")
print("Ein Weg, der schon geprueft hat, prueft nicht noch einmal (gemessen 11.11.)")
put_instance("production")
out, said = install(package("0.2.0", public=True), envelope_checked=True)
ok("install_artifact hat geprueft und bestaetigt: der Verzeichnisweg laesst durch",
   out == "past" and "NOTE" not in said, (out, said))
ok("install_artifact sagt es dem Verzeichnisweg (Uebernahme, Sideload, ZIP, Token)",
   "envelope_checked=True)" in src
   and 'if inst and not getattr(args, "envelope_checked", False):' in src)

print("")
print("Der ZIP-Weg fragt dieselbe Funktion")
ok("envelope_gate steht im ZIP-Zweig von cmd_install",
   src.count("envelope_gate(") >= 3, src.count("envelope_gate("))

print("")
print("ALLE PRUEFUNGEN BESTANDEN" if not fails else f"{fails} FEHLGESCHLAGEN")
sys.exit(1 if fails else 0)
