#!/usr/bin/env python3
"""Die wartenden Pakete des Servers (RFC-0139 Stufe 1).

Sicherheitsupdates spielt der Knoten selbst ein (unattended-upgrades). Was
sonst wartet -- auf unseren Knoten fast nur Docker --, sagte bisher niemand.
Jetzt zählt die Server-Pflege es, eine Meldung erinnert täglich, der
Wochenbericht hat eine Zeile, und `oaap node care upgrade --dry-run` zeigt
die Namen.

Braucht keinen Knoten. Aufruf: python3 test/test_host_updates.py
"""
import contextlib
import io
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
out, code = run_cli(A())
ok("ohne --dry-run: noch nicht (Stufe 2)", code != 0 and "stage 2" in out, (code, out))
host_care.apt_upgradable = real

print("")
print("OK" if not fails else f"{fails} FAIL")
sys.exit(1 if fails else 0)
