#!/usr/bin/env python3
"""Was ein gescheitertes Backup hinterlässt (0.1.249).

Befund 11.11. auf oaap-test: Das nächtliche Backup scheiterte am 07., 08.,
09. und 11.11. Portal und Journal sagten jedes Mal nur
„tar: …/1.14-main.sock: Socket ignoriert“ -- die LETZTE Zeile von stderr,
ohne Exit-Code. Diese Zeile ist harmlos (tar überspringt Sockets mit
Exit 0, nachgemessen mit GNU tar 1.35); die eigentliche Ursache stand
weiter oben und erreichte niemanden.

Geprüft:
1. Exit-Code steht immer in der Meldung.
2. Zeilen über übersprungene Sockets (deutsch und englisch) verdrängen
   keine echte Fehlerzeile; die ersten echten Zeilen stehen vorn.
3. Nur Socket-Zeilen: das wird ausgesprochen, statt als Ursache zu gelten.
4. Beide Archiv-Wege rufen tar mit --warning=no-file-ignored auf und
   benutzen diese eine Funktion für ihre Meldung.

Aufruf: python3 test/test_backup_failure_reason.py
"""
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "platform"))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services"))
import appctl  # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {detail}")


def cpe(code, stderr):
    return subprocess.CalledProcessError(code, ["tar", "-cpf", "x"], stderr=stderr)


r = appctl.backup_failure_reason(cpe(1, "tar: a/x.sock: Socket ignoriert\n"
                                        "tar: b/db: file changed as we read it\n"
                                        "tar: c/y.sock: socket ignored\n"))
ok("Exit-Code und echte Zeile, Sockets nicht", r == "tar exit 1: tar: b/db: file changed as we read it", r)

r = appctl.backup_failure_reason(cpe(2, "".join(f"tar: f{i}: Cannot open\n" for i in range(5))))
ok("höchstens drei Zeilen, Rest gezählt", r.endswith("(+2 more)") and r.count("Cannot open") == 3, r)

r = appctl.backup_failure_reason(cpe(1, "tar: a.sock: Socket ignoriert\n"))
ok("nur Sockets: ausgesprochen, nicht als Ursache", r == "tar exit 1 -- stderr held only skipped sockets", r)

r = appctl.backup_failure_reason(cpe(2, ""))
ok("ohne stderr: der Exit-Code", r == "tar exit 2", r)

r = appctl.backup_failure_reason(OSError("No space left on device"))
ok("andere Fehler: ihr Text", "No space left" in r, r)

src = open(os.path.join(HERE, "..", "platform", "appctl.py"), encoding="utf-8").read()
creates = re.findall(r'\["tar", "--numeric-owner"[^\]]*?"-cpf"', src, re.S)
ok("beide Archiv-Wege: tar mit --warning=no-file-ignored",
   len(creates) == 2 and all("--warning=no-file-ignored" in c for c in creates), creates)
ok("beide Meldungen über backup_failure_reason",
   'die(f"tenant backup failed: {backup_failure_reason(e)}")' in src
   and "reason = backup_failure_reason(e)" in src)

print("")
print(f"{'ALLE PRUEFUNGEN BESTANDEN' if not fails else str(fails) + ' FEHLGESCHLAGEN'}")
sys.exit(1 if fails else 0)
