#!/usr/bin/env python3
"""`oaap status` zaehlt eine App in einer Neustartschleife nicht als laufend (0.1.141).

Gefunden beim Flottenlauf auf 0.1.140 (29.09.): oaapx01 meldete
"Overall: HEALTHY (4/4 core services, 20/20 apps running)", waehrend
`aipc-test` 281 Mal abgestuerzt und neu gestartet war. `bin/oaap` zaehlte
laufende Apps mit `docker ps -q` -- und `docker ps` ohne `-a` listet
nicht nur `running`, sondern auch `restarting`. Die Liste darueber zeigte
"Restarting (1) 45 seconds ago", das Urteil darunter "HEALTHY".

Die Kernzeile zaehlte schon richtig (`--filter status=running`); die
Appzeile nicht. Zwei Zaehlungen, eine traegt die Regel nicht.

Gemessen wird am Verhalten, nicht am Text: `bin/oaap status` laeuft
wirklich, gegen ein nachgebautes `docker` (als exportierte
Shell-Funktion), das sich so verhaelt wie das echte am 29.09. auf
oaapx01 -- `ps -q` ohne Statusfilter liefert auch den Container, der
gerade neu startet. Gegen 0.1.140 meldet dieser Test FAIL.
"""
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
WRAPPER = os.path.join(HERE, "..", "bin", "oaap").replace("\\", "/")

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:600]}")


# Das nachgebaute docker. FAKE_CORE: "dienst:zustand ...", FAKE_APPS:
# "name:zustand ...". Zustaende wie bei Docker: running, restarting, exited.
FAKE_DOCKER = r'''
docker() {
  local status_filter="" all="" quiet="" args=" $* "
  case "$args" in *" status=running "*) status_filter=running ;; esac
  if [ "$1" = compose ]; then
    local e s st
    case "$args" in
      *" --services "*)
        for e in $FAKE_CORE; do s="${e%%:*}"; st="${e##*:}"
          if [ -z "$status_filter" ] || [ "$st" = "$status_filter" ]; then echo "$s"; fi
        done ;;
      *) for e in $FAKE_CORE; do echo "  ${e%%:*}: ${e##*:}"; done ;;
    esac
    return 0
  fi
  if [ "$1" = ps ]; then
    case "$args" in *" -a "*|*" -aq "*) all=1 ;; esac
    case "$args" in *" -q "*|*" -aq "*) quiet=1 ;; esac
    local e n st
    for e in $FAKE_APPS; do n="${e%%:*}"; st="${e##*:}"
      if [ -n "$status_filter" ]; then [ "$st" = "$status_filter" ] || continue
      # Wie das echte docker: ohne -a erscheinen running UND restarting.
      elif [ -z "$all" ]; then [ "$st" = running ] || [ "$st" = restarting ] || continue
      fi
      if [ -n "$quiet" ]; then echo "id-$n"; else echo "  $n: $st"; fi
    done
    return 0
  fi
  echo "fake docker: unerwarteter Aufruf: $*" >&2
  return 2
}
export -f docker
'''


def status(core, apps):
    bash = shutil.which("bash")
    if not bash:
        return None
    with tempfile.TemporaryDirectory() as d:
        data = d.replace("\\", "/")
        os.makedirs(os.path.join(d, "app"))
        open(os.path.join(d, ".oaap-installed"), "w").close()
        open(os.path.join(d, "app", "VERSION"), "w").write("0.0.0-test\n")
        env = dict(os.environ, OAAP_DATA_DIR=data,
                   FAKE_CORE=" ".join(core), FAKE_APPS=" ".join(apps))
        script = FAKE_DOCKER + f'\nbash "{WRAPPER}" status\n'
        r = subprocess.run([bash, "-c", script], env=env, capture_output=True,
                           text=True, encoding="utf-8", errors="replace")
        overall = [l for l in r.stdout.splitlines() if l.startswith("Overall:")]
        return r.returncode, (overall[0] if overall else ""), r.stdout + r.stderr


CORE = ["gateway:running", "identity:running", "portal:running", "connect:running"]

res = status(CORE, ["bdt-hub:running", "aipc-test:restarting"])
ok("bash ist da (ohne bash prueft diese Datei nichts)", res is not None)
if res is None:
    sys.exit(1)

code, overall, out = res
ok("eine App in der Neustartschleife -> DEGRADED, nicht HEALTHY",
   overall.startswith("Overall: DEGRADED"), out)
ok("... und sie zaehlt nicht als laufend (1/2 apps running)",
   "1/2 apps running" in overall, overall)
ok("... und der Aufruf endet mit Fehlercode (Skripte und Ueberwachung lesen ihn)",
   code == 1, f"exit {code}")

code, overall, out = status(CORE, ["bdt-hub:running", "aipc-test:running"])
ok("alles laeuft -> HEALTHY 2/2, exit 0",
   overall.startswith("Overall: HEALTHY") and "2/2 apps running" in overall and code == 0,
   out)

code, overall, out = status(CORE, ["bdt-hub:running", "alt:exited"])
ok("eine gestoppte App -> DEGRADED wie bisher (1/2)",
   overall.startswith("Overall: DEGRADED") and "1/2 apps running" in overall, out)

code, overall, out = status(CORE[:3] + ["connect:restarting"], ["bdt-hub:running"])
ok("ein Kerndienst in der Neustartschleife -> DEGRADED (3/4 core services)",
   overall.startswith("Overall: DEGRADED") and "3/4 core services" in overall, out)

code, overall, out = status(CORE, [])
ok("keine Apps -> HEALTHY 0/0", overall.startswith("Overall: HEALTHY")
   and "0/0 apps running" in overall, out)

print()
print("OK" if not fails else f"{fails} FAIL")
sys.exit(1 if fails else 0)
