#!/usr/bin/env python3
"""Die wartenden Pakete des Servers (RFC-0139 Stufen 1 bis 3).

Sicherheitsupdates spielt der Knoten selbst ein (unattended-upgrades). Was
sonst wartet -- auf unseren Knoten fast nur Docker --, sagte bisher niemand.
Jetzt zählt die Server-Pflege es, eine Meldung erinnert täglich, der
Wochenbericht hat eine Zeile, und `oaap node care upgrade --dry-run` zeigt
die Namen.

Braucht keinen Knoten. Aufruf: python3 test/test_host_updates.py
"""
import contextlib
import io
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["OAAP_DATA_DIR"] = tempfile.mkdtemp(prefix="oaap-hostupd-")
sys.path.insert(0, os.path.join(HERE, "..", "platform"))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services"))

import appctl as m  # noqa: E402
import host_care  # noqa: E402
import notify  # noqa: E402
import report  # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:400]}")


# So sah es am 27.11. auf oaap-test aus (C-Locale), plus eine Sicherheitszeile
# und ein Paket aus dem normalen Archiv.
APT = """Listing...
containerd.io/trixie 2.4.2-4~debian.13~trixie amd64 [upgradable from: 2.2.6-1~debian.13~trixie]
docker-buildx-plugin/trixie 0.39.1-1~debian.13~trixie amd64 [upgradable from: 0.36.1-1~debian.13~trixie]
docker-ce-cli/trixie 5:29.10.1-1~debian.13~trixie amd64 [upgradable from: 5:29.7.1-1~debian.13~trixie]
docker-ce-rootless-extras/trixie 5:29.10.1-1~debian.13~trixie amd64 [upgradable from: 5:29.7.1-1~debian.13~trixie]
docker-ce/trixie 5:29.10.1-1~debian.13~trixie amd64 [upgradable from: 5:29.7.1-1~debian.13~trixie]
docker-compose-plugin/trixie 5.8.0-1~debian.13~trixie amd64 [upgradable from: 5.4.0-1~debian.13~trixie]
libssl3t64/trixie-security 3.5.4-1~deb13u2 amd64 [upgradable from: 3.5.4-1~deb13u1]
raspi-firmware/trixie 1:1.20251120-1 all [upgradable from: 1:1.20250915-1]
"""

print("\nDie Liste von apt")
rows = host_care.upgradable(APT)
ok("acht Pakete, der Kopf zählt nicht", len(rows) == 8, rows)
kinds = {r["package"]: r["kind"] for r in rows}
ok("Docker an den Namen erkannt (das Archiv heißt wie Debians)",
   all(kinds[p] == "docker" for p in ("containerd.io", "docker-ce", "docker-ce-cli",
                                      "docker-buildx-plugin", "docker-compose-plugin",
                                      "docker-ce-rootless-extras")), kinds)
ok("Sicherheit am Archiv -security", kinds["libssl3t64"] == "security")
ok("der Rest ist other", kinds["raspi-firmware"] == "other")
r0 = rows[0]
ok("installierte und neue Version gelesen",
   r0["installed"] == "2.2.6-1~debian.13~trixie" and r0["version"] == "2.4.2-4~debian.13~trixie", r0)

print("\nGezählt, ohne Namen")
calls = []


def fake_run(cmd, timeout=30, env=None):
    calls.append((cmd, env))
    return APT if cmd[:2] == ["apt", "list"] else ""


f = host_care.updates_facts(fake_run)
ok("8 warten, 1 Sicherheit, 6 Docker, 1 sonst",
   f == {"waiting": 8, "security": 1, "docker": 6, "other": 1}, f)
ok("apt wird in der C-Locale gefragt", calls and calls[0][1].get("LC_ALL") == "C", calls)
ok("apt antwortet nicht -> keine Zahl (None), nie 0",
   host_care.updates_facts(lambda *a, **k: "") is None)
ok("nichts wartet -> 0", host_care.updates_facts(lambda *a, **k: "Listing...\n")
   == {"waiting": 0, "security": 0, "docker": 0, "other": 0})

print("\nDie Zeile für Status und Portal")
st, t = host_care.updates_line(f, {"reboot_required": True}, de=True)
ok("mit Sicherheitsupdate: warn", st == "warn", st)
ok("nennt Zahl, Sicherheit, Docker, Neustart und den Befehl",
   "8 warten" in t and "1 Sicherheit" in t and "6 Docker" in t and "Neustart" in t
   and "upgrade --dry-run" in t, t)
st, t = host_care.updates_line({"waiting": 6, "security": 0, "docker": 6, "other": 0}, {})
ok("nur Docker: ok, keine Warnung", st == "ok" and "6 waiting" in t, (st, t))
ok("keine: ok", host_care.updates_line({"waiting": 0}, {})[0] == "ok")
ok("unmessbar: unknown", host_care.updates_line(None)[0] == "unknown")
lines = host_care.status_lines({"kernel": {}, "auto_updates": {}, "updates": f})
ok("oaap status trägt die Zeile", any("Updates: 8 waiting" in x[1] for x in lines),
   [x[1] for x in lines])

print("\nDie Meldung host.updates (D1, D2)")
c = notify.CATALOGUE.get("host.updates") or {}
ok("im Katalog: Betrieb, nur der Knoten, eine Lage",
   c.get("category") == "operations" and c.get("scope") == "node" and c.get("matter") is True, c)
ok("erinnert alle 24 Stunden", notify.remind_hours("host.updates") == 24)
cond = m.host_updates_conditions({"updates": f, "kernel": {"reboot_required": False}})
h = cond.get("host.updates") or {}
ok("offen, solange etwas wartet; kurz = Zahl", h.get("kurz") == "8", cond)
ok("der Text sagt Docker startet alle Apps neu und wie man nachsieht",
   "etwa eine Minute" in h.get("text", "") and "upgrade --dry-run" in h.get("text", ""), h)
ok("zu, wenn nichts wartet", m.host_updates_conditions({"updates": {"waiting": 0}}) == {})
ok("zu, wenn nicht messbar", m.host_updates_conditions({"updates": None}) == {})
subj = notify.expand(c["subject"], {"knoten": "oaapx01", "kurz": "6"})
ok("Betreff", subj == "[oaapx01] 6 Updates für den Server warten", subj)
names, _s = notify.route_for({"lists": {}, "routes": {}}, "host.updates", True)
ok("ohne eigene Zeile an die Pflichtliste des Knotens", names == [notify.mandatory_name(True)], names)

print("\nDer Wochenbericht")
days = ["2026-11-23", "2026-11-24"]
ledger = [{"day": d, "events": [], "mails": [],
           "care": {"kernel": "6.12", "reboot_required": d == days[-1], "advisories": 0,
                    "updates": {"waiting": 6, "security": 0, "docker": 6, "other": 0}}}
          for d in days]
md = report.build(ledger, days, "Wochenbericht")
ok("Zeile mit Zahl, Docker und Neustart",
   "Updates für den Server: 6 warten (0 Sicherheit, 6 Docker) — ein Neustart wartet" in md, md[:900])
md2 = report.build(ledger, days, "Wochenbericht", scope_is_node=False)
ok("nicht im Bericht eines Mandanten", "Updates für den Server" not in md2)

print("\noaap node care upgrade --dry-run")
real = host_care.apt_upgradable
host_care.apt_upgradable = lambda run=None: APT


class A:
    action, profile, rest = "care", "upgrade", []
    space_dry, upgrade_docker = True, False


def run_cli(a):
    buf, code = io.StringIO(), 0
    real_root = getattr(os, "geteuid", lambda: 0)
    os.geteuid = lambda: 0
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            m.cmd_node_care(a)
    except SystemExit as e:
        code = e.code or 0
    finally:
        os.geteuid = real_root
    return buf.getvalue(), code


out, code = run_cli(A())
ok("zeigt Gruppen mit Namen und Versionen",
   "Docker (6):" in out and "docker-ce" in out and "5:29.7.1-1~debian.13~trixie -> 5:29.10.1" in out, out)
ok("sagt, dass Docker nur mit --docker käme", "6 of Docker only with --docker" in out
   and "take 2 package(s)" in out, out)
A.upgrade_docker = True
out, _ = run_cli(A())
ok("mit --docker alle acht", "take 8 package(s)" in out, out)
A.space_dry = False
started = []
real_start, real_follow = m.upgrade_start, m._upgrade_follow
m.upgrade_start = lambda with_docker=False, who="root", cause="hand": (started.append(with_docker) or (True, "Update gestartet (Unit oaap-upgrade)."))
m._upgrade_follow = lambda poll=2.0: {"result": "ok"}
out, code = run_cli(A())
ok("ohne --dry-run: startet die Unit (Stufe 2), mit Docker wie angehakt",
   code == 0 and started == [True] and "gestartet" in out, (code, out, started))
A.upgrade_docker = False
host_care.apt_upgradable = lambda run=None: "\n".join(l for l in APT.splitlines()
                                                       if l.startswith(("Listing", "containerd", "docker")))
started.clear()
out, code = run_cli(A())
ok("nur Docker wartet, ohne --docker: nichts gestartet, Hinweis auf --docker",
   not started and "add --docker" in out, (out, started))
m.upgrade_start, m._upgrade_follow = real_start, real_follow
host_care.apt_upgradable = real

print("\nDer Plan und der apt-Aufruf (Stufe 2)")
rows = host_care.upgradable(APT)
p0 = host_care.upgrade_plan(rows)
ok("ohne Haken: kein Docker-Paket im Plan (D4)",
   p0 == ["libssl3t64", "raspi-firmware"], p0)
ok("mit Haken: alle acht", len(host_care.upgrade_plan(rows, True)) == 8)
argv = host_care.upgrade_argv(p0)
ok("nur Upgrades, nie Entfernen, alte Konfiguration bleibt",
   argv[:3] == ["apt-get", "install", "--only-upgrade"] and "--no-remove" in argv
   and "Dpkg::Options::=--force-confold" in argv and "remove" not in argv[1:2]
   and "autoremove" not in argv and "purge" not in argv, argv)
ok("ohne Rückfrage", host_care.upgrade_env()["DEBIAN_FRONTEND"] == "noninteractive")
ok("fehlende Container", host_care.missing_containers(["a", "b", "c"], ["a", "c", "d"]) == ["b"])

print("\nEin Lauf (Stufe 2, mit Attrappen)")


class R:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


audits, notes, apts = [], [], []
real_audit, real_note, real_fresh = m.audit_tenant, m.notify_event, m._care_fresh
real_kf = host_care.kernel_facts
m.audit_tenant = lambda *a, **k: audits.append((a, k))
m.notify_event = lambda *a, **k: notes.append((a, k)) or "x"
m._care_fresh = lambda *a, **k: {}
host_care.kernel_facts = lambda *a, **k: {"reboot_required": True}
waiting = {"text": APT}
host_care.apt_upgradable = lambda run=None: waiting["text"]


def fake_apt(argv, timeout):
    apts.append(argv)
    if argv[:2] == ["apt-get", "install"]:
        waiting["text"] = "\n".join(l for l in APT.splitlines()
                                    if l.startswith(("Listing", "containerd", "docker")))
    return R(0, "6 upgraded, 0 newly installed, 0 to remove")


res = m.upgrade_run(False, who="joerg", backup=lambda: R(1, "", "disk full"), apt=fake_apt,
                    containers=lambda: ["a"], settle=0, sleep=lambda s: None)
ok("Sicherung schlägt fehl -> nichts eingespielt (D5)",
   res["result"] == "backup-failed" and not any(x[:2] == ["apt-get", "install"] for x in apts),
   (res, apts))
ok("... und gesagt", "nichts eingespielt" in res["text"] and "disk full" in res["text"], res)
ok("... mit Audit und Meldung", audits and audits[-1][0][0] == "host.upgraded"
   and audits[-1][0][3] == "failed" and notes and notes[-1][0][0] == "host.upgraded", (audits, notes))

apts.clear(); audits.clear(); notes.clear()
waiting["text"] = APT
seq = iter([["a", "b", "c"], None, ["a"], ["a", "b", "c"]])
naps = []
res = m.upgrade_run(False, who="joerg", backup=lambda: R(0), apt=fake_apt,
                    containers=lambda: next(seq), settle=60, sleep=naps.append)
inst = [x for x in apts if x[:2] == ["apt-get", "install"]]
ok("zuerst apt-get update, dann genau ein install", apts[0][:2] == ["apt-get", "update"]
   and len(inst) == 1, apts)
ok("install ohne Docker-Pakete", not any(host_care.is_docker_package(x) for x in inst[0][9:]), inst)
ok("wartet, bis alle Container wieder laufen (Docker antwortet zwischendurch nicht)",
   res["result"] == "ok" and res["missing"] == [] and len(naps) == 2, (res, naps))
ok("Text: Zahl, alle Container, Docker wartet weiter, Neustart",
   "2 Paket(e) eingespielt" in res["text"] and "Alle 3 Container" in res["text"]
   and "nur mit dem Haken" in res["text"] and "Neustart wartet" in res["text"], res["text"])
ok("Audit ok und eine Meldung", audits[-1][0][3] == "ok" and len(notes) == 1, (audits, notes))
st = m.upgrade_state()
ok("Zustandsdatei: fertig mit Ergebnis", st.get("state") == "finished"
   and st["result"]["packages"] == 2, st)
log = open(m.UPGRADE_LOG, encoding="utf-8").read()
ok("Protokoll nennt die Pakete und das Ergebnis", "libssl3t64" in log and "eingespielt" in log, log)

apts.clear(); notes.clear()
waiting["text"] = APT
res = m.upgrade_run(True, backup=lambda: R(0), apt=fake_apt, containers=lambda: ["a", "b"],
                    settle=10, sleep=lambda s: None)
inst = [x for x in apts if x[:2] == ["apt-get", "install"]][0]
ok("mit Haken: Docker im install", "docker-ce" in inst and "containerd.io" in inst, inst)
ok("Docker gezählt", res["docker"] == 6 and res["packages"] == 8, res)

waiting["text"] = APT
seq2 = iter([["a", "b"]] + [["a"]] * 10)
res = m.upgrade_run(False, backup=lambda: R(0), apt=fake_apt, containers=lambda: next(seq2),
                    settle=10, sleep=lambda s: None)
ok("ein Container kommt nicht zurück -> genannt, Audit failed",
   res["missing"] == ["b"] and "Nicht wieder gestartet: b" in res["text"]
   and audits[-1][0][3] == "failed", (res, audits[-1]))

waiting["text"] = APT
res = m.upgrade_run(False, backup=lambda: R(0), apt=lambda a, t: (apts.append(a) or R(100, "", "E: dpkg was interrupted")),
                    containers=lambda: ["a"], settle=0, sleep=lambda s: None)
ok("apt schlägt fehl -> gesagt, Code und Zeile", res["result"] == "apt-failed"
   and "dpkg was interrupted" in res["text"], res)

waiting["text"] = "Listing...\n"
notes.clear()
res = m.upgrade_run(False, backup=lambda: (_ for _ in ()).throw(AssertionError("no backup")),
                    apt=fake_apt, containers=lambda: [], settle=0, sleep=lambda s: None)
ok("nichts wartet -> keine Sicherung, keine Meldung", res["result"] == "nothing" and not notes, res)

c2 = notify.CATALOGUE.get("host.upgraded") or {}
ok("host.upgraded im Katalog: Betrieb, Knoten, einmalig",
   c2.get("category") == "operations" and c2.get("scope") == "node" and c2.get("matter") is False, c2)

m.audit_tenant, m.notify_event, m._care_fresh = real_audit, real_note, real_fresh
host_care.kernel_facts = real_kf
host_care.apt_upgradable = real

print("\nWährend apt läuft (gemessen 27.11.: eine Messung mitten in dpkg sagte 0)")
fake_proc = tempfile.mkdtemp(prefix="oaap-proc-")
for pid, comm in (("1", "systemd"), ("77", "python3"), ("self", "x")):
    os.makedirs(os.path.join(fake_proc, pid))
    open(os.path.join(fake_proc, pid, "comm"), "w").write(comm + "\n")
os.makedirs(os.path.join(fake_proc, "696"))
open(os.path.join(fake_proc, "696", "comm"), "w").write("unattended-upgr\n")
open(os.path.join(fake_proc, "696", "cmdline"), "wb").write(
    b"/usr/bin/python3\0/usr/share/unattended-upgrades/unattended-upgrade-shutdown\0--wait-for-signal\0")
ok("kein apt -> nicht beschäftigt (der Wächter unattended-upgrade-shutdown zählt nicht)",
   host_care.apt_busy(fake_proc) is False)
os.makedirs(os.path.join(fake_proc, "697"))
open(os.path.join(fake_proc, "697", "comm"), "w").write("unattended-upgr\n")
open(os.path.join(fake_proc, "697", "cmdline"), "wb").write(b"/usr/bin/python3\0/usr/bin/unattended-upgrade\0")
ok("unattended-upgrade selbst läuft -> beschäftigt", host_care.apt_busy(fake_proc) is True)
import shutil as _sh  # noqa: E402
_sh.rmtree(os.path.join(fake_proc, "697"))
os.makedirs(os.path.join(fake_proc, "4242"))
open(os.path.join(fake_proc, "4242", "comm"), "w").write("dpkg\n")
ok("dpkg läuft -> beschäftigt", host_care.apt_busy(fake_proc) is True)
prev = {"waiting": 5, "security": 1, "docker": 4, "other": 0}
ok("beschäftigt: die vorige Zahl bleibt stehen (nicht 0, Lage bleibt offen)",
   host_care.updates_facts(lambda *a, **k: "Listing...\n", before=prev, busy=True) == prev)
ok("beschäftigt ohne vorige Zahl: keine Zahl",
   host_care.updates_facts(lambda *a, **k: "Listing...\n", before=None, busy=True) is None)
ok("nicht beschäftigt: gemessen",
   host_care.updates_facts(lambda *a, **k: "Listing...\n", before=prev, busy=False)["waiting"] == 0)

print("\nDie Unit fragen (gemessen 27.11.: die CLI hörte mitten in der Sicherung auf)")
ok("Code 0 -> läuft", m.upgrade_unit_active(lambda *a, **k: R(0)) is True)
ok("Code 3 -> beendet", m.upgrade_unit_active(lambda *a, **k: R(3)) is False)
ok("Code 4 (keine solche Unit, nach dem Lauf aufgeräumt) -> beendet",
   m.upgrade_unit_active(lambda *a, **k: R(4)) is False)
ok("anderer Code -> unklar, nicht beendet", m.upgrade_unit_active(lambda *a, **k: R(1)) is None)


def slow(*a, **k):
    raise m.subprocess.TimeoutExpired("systemctl", 20)


ok("Zeitüberschreitung -> unklar", m.upgrade_unit_active(slow) is None)
answers = iter([None, True, None, False, None, False, False])
real_active = m.upgrade_unit_active
m.upgrade_unit_active = lambda run=None: next(answers)
m._upgrade_write({"state": "running"})
open(m.UPGRADE_LOG, "w").close()
buf = io.StringIO()
real_sleep = m.time.sleep
m.time.sleep = lambda s: None
with contextlib.redirect_stdout(buf):
    r = m._upgrade_follow(poll=0)
m.time.sleep = real_sleep
m.upgrade_unit_active = real_active
ok("Mitlesen hört erst nach zweimal klar beendet auf", r == {} and "beendet" in buf.getvalue()
   and next(answers, "leer") == "leer", (r, buf.getvalue()))

print("\nDer Auftrag aus dem Portal (Stufe 3)")
starts = []
real_start = m.upgrade_start
m.upgrade_start = lambda with_docker=False, who="root", cause="hand": (starts.append((with_docker, who, cause)) or (True, "Update gestartet (Unit oaap-upgrade)."))
host_care.apt_upgradable = lambda run=None: APT
ok("nur der Server-Admin (aus dem Datensatz des Handelnden)",
   m.upgrade_job({"op": "run", "args": {"docker": True}}, "tenant_admin", "eva")[0] is False and not starts)
okj, txt = m.upgrade_job({"op": "run", "args": {"docker": True}}, "server_admin", "joerg")
ok("Jetzt einspielen mit Haken: dieselbe Unit, mit Docker, mit dem Menschen",
   okj and starts == [(True, "joerg", "portal")] and "auch dieses Portal" in txt, (okj, txt, starts))
starts.clear()
m.upgrade_job({"op": "run", "args": {"docker": "yes"}}, "server_admin", "joerg")
ok("Docker nur bei echtem true (kein Text, keine 1)", starts == [(False, "joerg", "portal")], starts)
starts.clear()
host_care.apt_upgradable = lambda run=None: "\n".join(l for l in APT.splitlines()
                                                      if l.startswith(("Listing", "containerd", "docker")))
okj, txt = m.upgrade_job({"op": "run", "args": {}}, "server_admin", "joerg")
ok("nur Docker wartet, ohne Haken: nichts gestartet, Hinweis auf den Haken",
   not okj and not starts and "Haken" in txt, txt)
ok("unbekannter Auftrag", m.upgrade_job({"op": "rm"}, "server_admin", "joerg")[0] is False)
m.upgrade_start = real_start
host_care.apt_upgradable = real
src = open(os.path.join(HERE, "..", "platform", "appctl.py"), encoding="utf-8").read()
branch = src.split('elif action == "upgrade":', 1)[1].split("elif action ==", 1)[0]
ok("der Spool-Zweig ruft upgrade_job mit Rolle und Mensch des Handelnden und schreibt das Ergebnis",
   "upgrade_job(req, act_role, actor)" in branch and "result.json" in branch
   and 'audit_tenant("host.upgrade."' in branch, branch[:400])

print("\nDie Ansicht für das Portal (apps/upgrade.json)")
m._upgrade_write({"state": "finished", "started": "2026-11-27T11:00:00+00:00",
                  "finished": "2026-11-27T11:07:00+00:00", "who": "joerg", "docker": False,
                  "result": {"result": "ok", "text": "1 Paket(e) eingespielt"}})
m.upgrade_view_write(rows=host_care.upgradable(APT), measured="2026-11-27T11:08:00Z")
view = json.load(open(m.UPGRADE_VIEW, encoding="utf-8"))
ok("Pakete mit Versionen und Art, der letzte Lauf, kein Protokoll ohne Lauf",
   len(view["packages"]) == 8 and view["packages"][0]["kind"] == "docker"
   and view["run"]["text"] == "1 Paket(e) eingespielt" and view["log"] == [], view)
ok("lesbar für das Portal (0644)", oct(os.stat(m.UPGRADE_VIEW).st_mode & 0o777) == "0o644")
m._upgrade_write({"state": "running", "started": "2026-11-27T12:00:00+00:00", "who": "joerg"})
m._upgrade_say("2026-11-27T12:00:01+00:00 Sicherung ...")
view = json.load(open(m.UPGRADE_VIEW, encoding="utf-8"))
ok("während eines Laufs: Zustand und Protokollende; die Liste bleibt",
   view["run"]["state"] == "running" and view["log"][-1].endswith("Sicherung ...")
   and len(view["packages"]) == 8, view)
ok("die Pflegedatei trägt weiterhin nur Zahlen (keine Namen)",
   "rows_out.extend(rows)" in open(os.path.join(HERE, "..", "platform", "services", "host_care.py"),
                                   encoding="utf-8").read()
   and "updates_rows=rows" in src)

print("\nDer Abschnitt „Updates“ auf der Gesundheitsseite")
try:
    sys.path.insert(0, os.path.join(HERE, "..", "platform", "services", "portal"))
    import app as portal  # noqa: E402
except ImportError as e:
    print(f"NOT RUN  die Seite: {e} (Flask steht im Portal-Abbild)")
    print(f"{fails} FAIL" if fails else "PARTIAL PASS (Seite nicht geprüft)")
    sys.exit(1 if fails else 2)
portal.UPGRADE_VIEW_FILE = m.UPGRADE_VIEW
m._upgrade_write({"state": "finished", "finished": "2026-11-27T12:07:00+00:00", "who": "joerg",
                  "result": {"result": "ok", "text": "1 Paket(e) eingespielt"}})
TMP = tempfile.mkdtemp(prefix="oaap-upg-page-")
mg = portal.management_api
mg.SPOOL_DIR = TMP
for d_ in ("queue", "jobs"):
    os.makedirs(os.path.join(TMP, d_), exist_ok=True)
QUEUED = []
mg.CTX["queue"] = lambda rid, name, payload, wait: QUEUED.append(dict(payload, _rid=rid))
WHO = {"role": "server_admin", "roles": {"server_admin"}, "tenant": "", "host": None}
portal.caller_roles = lambda: WHO["roles"]
portal.caller_scope = lambda: (WHO["role"], WHO["tenant"])
portal.host_tenant_scope = lambda host: (WHO["host"], True)
c = portal.app.test_client()
H = {"X-OAAP-User": "betreiber", "X-OAAP-Roles": "server_admin"}
body = portal.HEALTH_BODY
card = body[body.index("{% if upg %}"):body.index('<div class="card">\n  <h2>Kernservices')]
tpl = portal.app.jinja_env.from_string(card)
with portal.app.test_request_context("/health", headers=H):
    sec = portal._upgrade_section()
    html = tpl.render(upg=sec, job=None)
ok("Server-Admin am Knoten: Gruppen, Versionen, Haken für Docker, Knopf, letzter Lauf",
   sec and "Docker mit aktualisieren (6 Paket(e))" in html and "Jetzt einspielen" in html
   and "libssl3t64" in html and "5:29.10.1-1~debian.13~trixie" in html
   and "Sicherheit (1)" in html and "1 Paket(e) eingespielt" in html, html[:1500])
ok("die Rückfrage nennt Sicherung und den Neustart aller Apps", "confirm(" in html
   and "ALLE Apps" in html)
m._upgrade_write({"state": "running", "started": "2026-11-27T12:10:00+00:00", "who": "joerg",
                  "docker": True})
m._upgrade_say("2026-11-27T12:10:01+00:00 Sicherung ...")
with portal.app.test_request_context("/health", headers=H):
    html = tpl.render(upg=portal._upgrade_section(), job=None)
ok("läuft: kein Knopf, Protokoll, lädt sich neu, sagt: auch das Portal ist kurz weg",
   "Ein Update läuft" in html and "Jetzt einspielen" not in html and "Sicherung ..." in html
   and "location.reload" in html and "auch dieses Portal" in html, html[:1200])
for who, role, roles in (("Verwalter", "tenant_admin", {"tenant_admin"}), ("Mitglied", "", {"user"})):
    WHO.update(role=role, roles=roles, tenant="t-a")
    with portal.app.test_request_context("/health", headers=H):
        hidden = portal._upgrade_section() is None
    code = c.post("/health/upgrade/run", headers=H, data={"docker": "1"}).status_code
    ok(f"{who}: kein Abschnitt, 403 an der Tür, nichts im Spool", hidden and code == 403 and not QUEUED,
       (hidden, code))
WHO.update(role="server_admin", roles={"server_admin"}, tenant="", host="t-kunde")
with portal.app.test_request_context("/health", headers=H):
    hidden = portal._upgrade_section() is None
code = c.post("/health/upgrade/run", headers=H).status_code
ok("Server-Admin am Ort eines Mandanten: kein Abschnitt, 404", hidden and code == 404 and not QUEUED)
WHO.update(host=None)
r = c.post("/health/upgrade/run", headers=H, data={"docker": "1"})
ok("Jetzt einspielen mit Haken: ein Auftrag upgrade/run mit docker true, zurück mit Job",
   len(QUEUED) == 1 and QUEUED[0]["action"] == "upgrade" and QUEUED[0]["op"] == "run"
   and QUEUED[0]["args"] == {"docker": True} and "/health?job=" in r.headers["Location"], QUEUED)
QUEUED.clear()
c.post("/health/upgrade/run", headers=H)
ok("ohne Haken: docker false", QUEUED and QUEUED[0]["args"] == {"docker": False}, QUEUED)
QUEUED.clear()
c.post("/health/upgrade/measure", headers=H)
ok("Neu zählen: upgrade/measure", QUEUED and QUEUED[0]["op"] == "measure", QUEUED)
QUEUED.clear()
r = c.post("/health/upgrade/purge", headers=H)
ok("ein Auftrag, den es nicht gibt: nichts im Spool", not QUEUED and r.status_code == 302)
r = c.post("/health/upgrade/run", headers=dict(H, Origin="https://evil.example"))
ok("fremde Herkunft: 403", r.status_code == 403 and not QUEUED)

print("")
print("OK" if not fails else f"{fails} FAIL")
sys.exit(1 if fails else 0)
