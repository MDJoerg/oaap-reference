#!/usr/bin/env python3
"""Install watches whether the instance runs (0.1.358, runtime 2.11 / 0.2.81).

Found 2026-11-23 on oaap-test (Forgejo upgrade measurement, F2): Forgejo 11
installed over a database migrated by Forgejo 15 exited at once and
restarted in a loop; the gateway answered 502 and the install said
"Installed". Now:

  * a container that restarts, exits, is dead or missing = does not run;
  * a slow app (running, not answering) is not a problem;
  * a runtime that cannot be asked says nothing;
  * the report has a WARNING with log lines and a NOTE for the portal;
  * only the command line exits with 3.

Run: python3 test/test_install_settled.py
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-install-settled-test-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, os.path.join(HERE, "..", "platform"))

import appctl as m                                            # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:700]}")


def st(state="running", restarts=0, exit_code=0, oom=False):
    return {"state": state, "started": "", "restarts": restarts,
            "exit_code": exit_code, "oom": oom, "health": ""}


print("=== die Entscheidung (rein) ===")
C = ["oaap-app-fj"]
ok("läuft, keine Neustarts -> nichts", m.install_settle_problems({"oaap-app-fj": st()}, C) == [])
p = m.install_settle_problems({"oaap-app-fj": st("restarting", 3, 1)}, C)
ok("Neustartschleife wird genannt", p == ["oaap-app-fj restarting, exit code 1, 3 restarts"], p)
p = m.install_settle_problems({"oaap-app-fj": st("running", 1)}, C)
ok("läuft gerade wieder, aber 1 Neustart -> Problem", p == ["oaap-app-fj running, 1 restart"], p)
p = m.install_settle_problems({"oaap-app-fj": st("exited", 0, 137, oom=True)}, C)
ok("ausgestiegen wegen Speicher", p == ["oaap-app-fj exited, exit code 137, out of memory"], p)
p = m.install_settle_problems({}, C)
ok("fehlender Container", p == ["oaap-app-fj does not exist"], p)
p = m.install_settle_problems({"a": st(), "b": st("dead")}, ["a", "b"])
ok("nur der kaputte von zwei Diensten", p == ["b dead"], p)

print("=== das Beobachten (Docker nachgestellt) ===")
inst = {"container": "oaap-app-fj", "image": "oaap-app/fj:1", "svc_port": 3000}
m.time.sleep = lambda s: None
seq = []


def fake_states(names):
    cur = seq.pop(0) if len(seq) > 1 else seq[0]
    return cur


m.container_states = fake_states
answers = {"v": False}
m._answers = lambda inst, seconds: answers["v"]

seq[:] = [({"oaap-app-fj": st()}, True), ({"oaap-app-fj": st("restarting", 1, 1)}, True)]
ok("Absturz nach dem ersten Blick wird gefunden",
   m.check_install_settled(inst, seconds=5) == ["oaap-app-fj restarting, exit code 1, 1 restart"])

seq[:] = [({"oaap-app-fj": st()}, True)]
answers["v"] = True
ok("antwortet und läuft -> nichts", m.check_install_settled(inst, seconds=5) == [])

answers["v"] = False
seq[:] = [({"oaap-app-fj": st()}, True)]
ok("langsam (läuft, antwortet nicht) -> nichts", m.check_install_settled(inst, seconds=0) == [])

seq[:] = [({}, False)]
ok("Laufzeit nicht fragbar -> nichts", m.check_install_settled(inst, seconds=5) == [])

seq[:] = [({"oaap-app-fj": st()}, True), ({"oaap-app-fj": st("exited", 0, 1)}, True)]
answers["v"] = True
ok("antwortet, steigt direkt danach aus -> Problem",
   m.check_install_settled(inst, seconds=5) == ["oaap-app-fj exited, exit code 1"])

ok("keine Container -> nichts", m.check_install_settled({"container": "", "image": "x"}, seconds=5) == [])

print("=== der Bericht ===")
log = "\n".join(f"line {i}" for i in range(9)) + "\nYour Forgejo database is for a newer version\n"
lines = m.install_settle_report("fjtwin", ["oaap-app-fjtwin restarting, 2 restarts"],
                                {"oaap-app-fjtwin": log})
ok("WARNING zuerst, mit Namen", lines[0].startswith("WARNING: 'fjtwin' is installed but does NOT run"))
ok("letzte fünf Logzeilen, die letzte dabei",
   sum(1 for l in lines if l.startswith("      | ")) == 5
   and any("newer version" in l for l in lines), lines)
ok("NOTE als letzte Zeile (fürs Portal)",
   lines[-1].startswith("NOTE: the instance does not run after the install"))
ok("envelope_notes_suffix trägt sie ins Portal",
   "does not run" in m.envelope_notes_suffix("\n".join(lines)))
ok("sagt, dass nichts zurückgerollt wird", any("NOT undone" in l for l in lines))

print("=== nur die Kommandozeile endet mit 3 ===")
orig = m.cmd_install


def broken(args):
    m.LAST_INSTALL_NOT_RUNNING.append("x")


m.cmd_install = broken
code = None
try:
    m.cmd_install_cli(None)
except SystemExit as e:
    code = e.code
ok("Kommandozeile: Exitcode 3", code == m.INSTALL_NOT_RUNNING_EXIT == 3, code)
m.cmd_install = lambda args: None
m.LAST_INSTALL_NOT_RUNNING.append("stale")
code = "none"
try:
    m.cmd_install_cli(None)
except SystemExit as e:
    code = e.code
ok("alter Merker wird vorher geleert -> kein Exit", code == "none", code)
m.cmd_install = orig
ok("der CLI-Eintrag zeigt auf cmd_install_cli",
   "pi.set_defaults(fn=cmd_install_cli)" in open(m.__file__, encoding="utf-8").read())

print(f"\n{'ALL PASS' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
