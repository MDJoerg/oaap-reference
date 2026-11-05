#!/usr/bin/env python3
"""Host care (oaap.core.updates 2.6 and 2.7, oaap.fleet.status 0.6, 0.1.221).

The rules the fleet measurement of 2026-11-05 (CVE-2026-98432) produced,
executed without a node -- every command the module would run is replaced
by a recorded answer, so a test can say "oaapx02" and mean exactly the
machine that fooled the first version of the measuring tool:

- the reboot verdict is a COMPARISON of the running kernel with the newest
  installed kernel of the same flavour, never the marker file (test 9);
- automatic updates are ONE verdict from THREE parts, the missing one
  named (test 10);
- the gateway's listeners come from `ss -M` in its namespace, where every
  line IS an MPTCP listener and loopback ones are not public;
- the three readers (CLI lines, portal lines, fleet block) come from one
  set of facts and say the same thing (test 11);
- the settings file is written, idempotent, and the window needs the
  program first (tests 12, 13);

and the rules of RFC-0069 (2.7), measured here before a kernel arrives:

- a plan needs a window, keeps its announcement, lies 24 h ahead (test 14);
- a protection holds `care` and not `critical`, needs a reason and an end
  (test 15); urgency comes from origin and exposure (test 16);
- the plan closes with proof, or shows as overdue (test 17);
- the distribution's own reboot is off whatever the window (test 18).

Run: python3 test/test_host_care.py
"""
import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services"))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services", "portal"))

import host_care as hc  # noqa: E402
import fleet_view as fv  # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:500]}")


# --- the recorded machines ---------------------------------------------------

DPKG_X02 = """linux-image-6.12.111+deb13-amd64 install ok installed
linux-image-6.12.111+deb13-amd64-unsigned unknown ok not-installed
linux-image-6.12.115+deb13.1-amd64 install ok installed
linux-image-amd64 install ok installed
linux-image-generic unknown ok not-installed
"""
DPKG_PI = """linux-image-6.18.50+rpt-rpi-v8 install ok installed
linux-image-6.18.50+rpt-rpi-2712 install ok installed
linux-image-rpi-v8 install ok installed
"""
APT_ON = """APT::Periodic "";
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
Unattended-Upgrade::Origins-Pattern "";
Unattended-Upgrade::Origins-Pattern:: "origin=Debian,codename=${distro_codename},label=Debian-Security";
"""
# a 2.6.3-style file: the distribution's own reboot on (stage 1)
APT_LEGACY = APT_ON + """Unattended-Upgrade::Automatic-Reboot "true";
Unattended-Upgrade::Automatic-Reboot-Time "04:15";
"""
# the 2.7.4 form: the platform's key, the distribution's reboot off
APT_WINDOW = APT_ON + """Unattended-Upgrade::Automatic-Reboot "false";
OAAP::Care::Reboot-Window "04:15";
"""
APT_OFF = """APT::Periodic "";
Unattended-Upgrade::Origins-Pattern "";
"""
SS_MPTCP = """LISTEN 0      4096   127.0.0.11:33341 0.0.0.0:*
LISTEN 0      4096    127.0.0.1:2019  0.0.0.0:*
LISTEN 0      4096            *:80          *:*
LISTEN 0      4096            *:443         *:*
LISTEN 0      4096            *:8099        *:*
"""
# `apt-cache policy linux-image-6.12.115+deb13.1-amd64` on oaap-test, 2026-11-05
POLICY_SECURITY = """linux-image-6.12.115+deb13.1-amd64:
  Installiert:           6.12.115-2
  Installationskandidat: 6.12.115-2
  Versionstabelle:
 *** 6.12.115-2 500
        500 http://security.debian.org/debian-security trixie-security/main amd64 Packages
        100 /var/lib/dpkg/status
"""
POLICY_POINT = """linux-image-6.12.107+deb13-amd64:
  Installed: 6.12.107-1
  Candidate: 6.12.107-1
  Version table:
 *** 6.12.107-1 500
        500 http://deb.debian.org/debian trixie/main amd64 Packages
        100 /var/lib/dpkg/status
"""
POLICY_PI = """linux-image-6.18.50+rpt-rpi-v8:
  Installed: 1:6.18.50-1+rpt1
  Candidate: 1:6.18.50-1+rpt1
  Version table:
 *** 1:6.18.50-1+rpt1 500
        500 http://archive.raspberrypi.com/debian trixie/main arm64 Packages
        100 /var/lib/dpkg/status
"""
try:
    BERLIN = ZoneInfo("Europe/Berlin")
except Exception:  # noqa: BLE001 -- a Windows Python without tzdata: November is CET
    BERLIN = timezone(timedelta(hours=1), "CET")
T0 = datetime(2026, 11, 6, 10, 0, tzinfo=timezone.utc)   # Fri 11:00 CET


def machine(running, dpkg, apt, uu="install ok installed", timer="active",
            ss=SS_MPTCP, pid="2620", policy=POLICY_SECURITY):
    """A fake `sh`: answers like the machine would."""
    def run(cmd, timeout=30, env=None):
        c = " ".join(cmd)
        if cmd[0] == "uname":
            return running + "\n"
        if cmd[0] == "dpkg-query" and "linux-image-*" in c:
            return dpkg
        if cmd[0] == "dpkg-query" and hc.PACKAGE in c:
            return uu
        if cmd[0] == "apt-config":
            return apt
        if cmd[0] == "apt-cache":
            return policy
        if cmd[0] == "systemctl":
            return timer + "\n"
        if cmd[0] == "docker":
            return pid + "\n"
        if cmd[0] == "nsenter" and "ss" in cmd:
            return ss
        if cmd[0] == "nsenter":
            return "ok\n"
        return ""
    return run


print("\n-- the kernel: flavour, order, and the verdict by comparison (2.6.1, test 9)")
ok("Debian flavour", hc.kernel_flavour("6.12.115+deb13.1-amd64") == "-amd64")
ok("Pi flavour", hc.kernel_flavour("6.18.50+rpt-rpi-v8") == "-rpi-v8")
ok("a release without ABI tag keeps its flavour",
   hc.kernel_flavour("6.12.115-amd64") == "-amd64")
ok("6.12.100+deb13 < 6.12.115+deb13.1",
   hc.version_key("6.12.100+deb13-amd64") < hc.version_key("6.12.115+deb13.1-amd64"))
ok("installed kernels: meta package and unsigned twins fall out",
   hc.installed_kernels(DPKG_X02) == ["6.12.111+deb13-amd64", "6.12.115+deb13.1-amd64"])
ok("newest of the flavour", hc.newest_of_flavour(
    hc.installed_kernels(DPKG_X02), "-amd64") == "6.12.115+deb13.1-amd64")
ok("another flavour's newer kernel is not an update for this one",
   hc.newest_of_flavour(hc.installed_kernels(DPKG_PI), "-rpi-v8") == "6.18.50+rpt-rpi-v8")
ok("oaapx02: reboot required WITHOUT a marker (the first tool said no)",
   hc.reboot_required("6.12.111+deb13-amd64", "6.12.115+deb13.1-amd64"))
ok("after the reboot: not required",
   not hc.reboot_required("6.12.115+deb13.1-amd64", "6.12.115+deb13.1-amd64"))
ok("a running kernel newer than any package (rpi-update): not required",
   not hc.reboot_required("6.18.59+rpt-rpi-v8", "6.18.50+rpt-rpi-v8"))
k = hc.kernel_facts(machine("6.12.111+deb13-amd64", DPKG_X02, APT_OFF), marker_exists=False)
ok("kernel facts carry both kernels, the verdict, the origin; marker only as a hint",
   k["running"] == "6.12.111+deb13-amd64" and k["installed"] == "6.12.115+deb13.1-amd64"
   and k["reboot_required"] and k["marker"] is False and k["origin"] == "security", k)

print("\n-- the origin of the waiting kernel (2.7.3)")
ok("security archive", hc.kernel_origin(POLICY_SECURITY) == "security")
ok("main archive = a point release", hc.kernel_origin(POLICY_POINT) == "distribution")
ok("Raspberry Pi archive = vendor", hc.kernel_origin(POLICY_PI) == "vendor")
ok("no policy = unknown", hc.kernel_origin("") == "unknown"
   and hc.kernel_origin("linux-image-x:\n  Installed: (none)\n") == "unknown")
ok("in both archives: security wins (the conservative reading)",
   hc.kernel_origin(POLICY_SECURITY.replace(
       "        100 /var/lib/dpkg/status",
       "        500 http://deb.debian.org/debian trixie/main amd64 Packages\n"
       "        100 /var/lib/dpkg/status")) == "security")
ok("another version's sources are not this version's",
   hc.kernel_origin(POLICY_POINT + "     6.12.100-1 500\n"
                    "        500 http://security.debian.org/debian-security trixie-security/main amd64 Packages\n")
   == "distribution")

print("\n-- automatic updates: three parts, one verdict (2.6.1, test 10)")
ok("not installed -> error", hc.auto_updates_verdict(False, False, False)
   == ("error", "program not installed"))
ok("installed, switch missing -> warn naming the switch",
   hc.auto_updates_verdict(True, False, True) == ("warn", "installed, but periodic switch not set"))
ok("installed, switch set, timer inactive -> warn naming the timer",
   hc.auto_updates_verdict(True, True, False) == ("warn", "installed, but timer not active"))
ok("all three -> ok", hc.auto_updates_verdict(True, True, True)[0] == "ok")
a = hc.auto_updates_facts(machine("x", "", APT_OFF), stamp_mtime=0)
ok("oaapx02 after Joerg's apt-get install: the package alone is 'warn'",
   a["state"] == "warn" and "switch" in a["detail"] and a["last_run"] == "", a)
a = hc.auto_updates_facts(machine("x", "", APT_ON), stamp_mtime=1793860920)
ok("switch and timer: ok, with the last run", a["state"] == "ok"
   and a["last_run"] == "2026-11-05T06:42:00Z" and a["window"] == "off", a)
a = hc.auto_updates_facts(machine("x", "", APT_WINDOW), stamp_mtime=0)
ok("the window is read from the platform's key; the distribution's reboot is off",
   a["window"] == "04:15" and a["distribution_reboot"] is False, a)
a = hc.auto_updates_facts(machine("x", "", APT_LEGACY), stamp_mtime=0)
ok("a 2.6.3-style file still reads its window -- and says the distribution would reboot",
   a["window"] == "04:15" and a["distribution_reboot"] is True, a)
a = hc.auto_updates_facts(machine("x", "", APT_ON, uu="unknown ok not-installed"), stamp_mtime=0)
ok("not installed is 'error' even with a stale switch", a["state"] == "error")

print("\n-- the gateway's listeners")
ok("every ss -M line is an MPTCP listener; loopback ones are not public",
   hc.mptcp_listeners(SS_MPTCP) == ["*:80", "*:443", "*:8099"])
ok("no lines, namespace readable -> off",
   hc.gateway_facts(machine("x", "", "", ss=""))["mptcp"] == "off")
ok("public listeners -> on with the ports",
   hc.gateway_facts(machine("x", "", ""))["mptcp"] == "on")
ok("no gateway container -> unknown",
   hc.gateway_facts(machine("x", "", "", pid=""))["mptcp"] == "unknown")

print("\n-- one measurement, three readers (2.6.2, test 11)")
tmp = tempfile.mkdtemp(prefix="oaap-care-")
osr = os.path.join(tmp, "os-release")
open(osr, "w").write('PRETTY_NAME="Debian GNU/Linux 13 (trixie)"\nID=debian\n')
facts = hc.measure(machine("6.12.111+deb13-amd64", DPKG_X02, APT_OFF, uu="unknown ok not-installed"),
                   os_release=osr, marker_exists=False, stamp_mtime=0,
                   boot_time="2026-11-05T09:20:00Z")
ok("the measurement is dated, names the OS and the boot time",
   facts["measured"].endswith("Z") and facts["os"].startswith("Debian")
   and facts["boot_time"] == "2026-11-05T09:20:00Z", facts)
hc.write(facts, tmp)
back = hc.read(tmp)
ok("written and read back identical", back == facts)
en = hc.status_lines(back)
de = hc.status_lines(back, de=True)
ok("three lines each", len(en) == 3 and len(de) == 3, (en, de))
ok("CLI: reboot line carries both kernels and the command",
   en[0][0] == "warn" and "6.12.111" in en[0][1] and "6.12.115" in en[0][1]
   and "systemctl reboot" in en[0][1], en[0])
ok("portal: same facts in German", de[0][0] == "warn" and "6.12.115" in de[0][1]
   and "Neustart" in de[0][1], de[0])
ok("auto-updates not installed: err with the switch-on command",
   en[1][0] == "err" and "auto-updates on" in en[1][1] and de[1][0] == "err")
ok("gateway MPTCP on: warn naming the ports and oaap update",
   en[2][0] == "warn" and "*:443" in en[2][1] and "oaap update" in en[2][1])
ok("worst of the lines", hc.worst(en) == "err")
blk = hc.fleet_host(back)
ok("fleet block: the whitelist of 0.5, no plan, no protections", blk == {
    "measured": facts["measured"], "os": "Debian GNU/Linux 13 (trixie)",
    "kernel_running": "6.12.111+deb13-amd64",
    "kernel_installed": "6.12.115+deb13.1-amd64", "reboot_required": True,
    "auto_updates": "error", "reboot_window": "off", "gateway_mptcp": "on"}, blk)
att = hc.fleet_attention(back)
ok("three attention kinds", [x["kind"] for x in att]
   == ["reboot_required", "auto_updates_off", "gateway_mptcp"], att)
doc = fv.build_document(node="n", version="v", profiles=[], now_iso="t", core=[],
                        instances=[], dns_rows=None, pending_names=[],
                        extra_attention=att, host=blk)
ok("document 0.6 carries host and the kinds", doc["schema"] == "oaap.fleet.status/0.6"
   and doc["host"]["reboot_required"] is True
   and {a["kind"] for a in doc["attention"]} >= {"reboot_required", "gateway_mptcp"})
ok("no measurement -> no host block, no kinds, one honest line",
   "host" not in fv.build_document(node="n", version="v", profiles=[], now_iso="t",
                                   core=[], instances=[], dns_rows=None,
                                   pending_names=[], host=hc.fleet_host(None))
   and hc.fleet_attention(None) == []
   and hc.status_lines(None)[0][0] == "unknown")
healthy = hc.measure(machine("6.12.115+deb13.1-amd64", DPKG_X02, APT_WINDOW, ss=""),
                     os_release=osr, marker_exists=False, stamp_mtime=1793860920)
hl = hc.status_lines(healthy)
ok("a healthy node: three ok lines, window named",
   all(s == "ok" for s, _ in hl) and "04:15" in hl[1][1], hl)
ok("healthy fleet block has no attention", hc.fleet_attention(healthy) == [])
legacy = hc.measure(machine("6.12.115+deb13.1-amd64", DPKG_X02, APT_LEGACY, ss=""),
                    os_release=osr, marker_exists=False, stamp_mtime=0)
ok("a 2.6.3-style node gets a warning line: two mechanisms (test 18)",
   any(s == "warn" and "two mechanisms" in t for s, t in hc.status_lines(legacy)))
ok("a measurement older than a day gets a warning line",
   hc.status_lines(healthy, now=datetime(2027, 1, 1, tzinfo=timezone.utc))[-1][0] == "warn")
ok("stale after ten minutes, not before",
   not hc.is_stale(healthy) and hc.is_stale(healthy, now=datetime(2027, 1, 1, tzinfo=timezone.utc)))
out = io.StringIO()
with redirect_stdout(out):
    hc.print_status(back)
ok("print_status: header with the time, three indented lines",
   out.getvalue().startswith("Host care (measured ") and out.getvalue().count("\n  ") == 3, out.getvalue())

print("\n-- the settings file (2.6.3, 2.7.4, tests 12, 13 and 18)")
conf = os.path.join(tmp, "52oaap-care")
ok("absent file -> window off", hc.read_conf(conf) == "off")
ok("absent file is not migrated into being", hc.migrate_conf(conf) is False and not os.path.exists(conf))
ok("written once", hc.write_conf("off", conf) is True)
ok("written again: nothing changed", hc.write_conf("off", conf) is False)
text = open(conf).read()
ok("the switch is in the file, both lines",
   'APT::Periodic::Unattended-Upgrade "1";' in text
   and 'APT::Periodic::Update-Package-Lists "1";' in text)
ok("off: Automatic-Reboot false, no window key", 'Automatic-Reboot "false"' in text
   and "Reboot-Window" not in text)
ok("the file says who wrote it and how to change it",
   text.startswith("// Written by OAAP") and "oaap node care" in text)
ok("window validated", hc.validate_window("04:15") == "04:15" and hc.validate_window("OFF") == "off")
try:
    hc.validate_window("4:15")
    ok("a bad hour is refused", False)
except ValueError:
    ok("a bad hour is refused", True)
said = []
w = hc.reboot_window_set("04:15", run=machine("x", "", ""), path=conf, say=said.append)
text = open(conf).read()
ok("window set: the platform's key, and the distribution's reboot STILL false (test 18)",
   w == "04:15" and 'OAAP::Care::Reboot-Window "04:15"' in text
   and 'Automatic-Reboot "false"' in text and 'Automatic-Reboot "true"' not in text
   and "Automatic-Reboot-Time" not in text, text)
ok("read back from our own file", hc.read_conf(conf) == "04:15")
ok("the switch survives the window", 'Unattended-Upgrade "1"' in open(conf).read())
try:
    hc.reboot_window_set("04:15", run=machine("x", "", "", uu="unknown ok not-installed"),
                         path=conf, say=said.append)
    ok("a window without the program is refused", False)
except ValueError as e:
    ok("a window without the program is refused", "auto-updates on" in str(e))
hc.reboot_window_set("off", run=machine("x", "", ""), path=conf, say=said.append)
ok("off clears it", hc.read_conf(conf) == "off")
open(conf, "w").write(APT_LEGACY)
ok("a 2.6.3-style file reads its window", hc.read_conf(conf) == "04:15")
ok("... and is rewritten by migrate_conf with the window kept, distribution off",
   hc.migrate_conf(conf) is True and hc.read_conf(conf) == "04:15"
   and 'Automatic-Reboot "false"' in open(conf).read()
   and hc.migrate_conf(conf) is False)

print("\n-- the protections (2.7.2, test 15)")
prot = {}
rec = hc.protect(prot, "joerg-desk", "Schulung bis Mittwoch", "joerg", "t-demo",
                 today=T0.date())
ok("default end is 7 days, the record carries reason, who, tenant",
   rec["until"] == "2026-11-13" and rec["reason"] == "Schulung bis Mittwoch"
   and rec["who"] == "joerg" and rec["tenant"] == "t-demo", rec)
try:
    hc.protect(prot, "x", "", "joerg", "t", today=T0.date())
    ok("no reason: refused", False)
except ValueError as e:
    ok("no reason: refused", "reason" in str(e))
try:
    hc.protect(prot, "x", "r", "joerg", "t", until="2026-12-31", today=T0.date())
    ok("beyond 30 days: refused", False)
except ValueError as e:
    ok("beyond 30 days: refused", "30 days" in str(e))
try:
    hc.protect(prot, "x", "r", "joerg", "t", until="2026-11-01", today=T0.date())
    ok("in the past: refused", False)
except ValueError as e:
    ok("in the past: refused", "past" in str(e))
hc.protect(prot, "gone", "r", "joerg", "t", until="2026-11-20", today=T0.date())
hc.protect(prot, "old", "r", "joerg", "t", until="2026-11-06", today=T0.date())
live = hc.live_protections(prot, {"joerg-desk", "old"}, today=T0.date())
ok("live: not expired, instance known; a stale name is ignored",
   set(live) == {"joerg-desk", "old"}, live)
live2 = hc.live_protections(prot, {"joerg-desk", "old"}, today=T0.date() + timedelta(days=1))
ok("the end day is inclusive; the day after it is gone", set(live2) == {"joerg-desk"})
ok("latest end", hc.latest_end(live) == datetime(2026, 11, 13).date())
ok("release", hc.release(prot, "old") and not hc.release(prot, "old"))

print("\n-- the plan (2.7.1, 2.7.3, tests 14 and 16)")


def node(apt=APT_WINDOW, external="oaap.joomp.de", policy=POLICY_SECURITY, ss="",
         running="6.12.111+deb13-amd64", boot="2026-11-05T09:20:00Z"):
    return hc.measure(machine(running, DPKG_X02, apt, ss=ss, policy=policy),
                      os_release=osr, marker_exists=False, stamp_mtime=0,
                      external=external, boot_time=boot)


ok("security kernel + external name = critical",
   hc.urgency_of(node())[0] == "critical")
ok("security kernel + MPTCP on, no name = critical",
   hc.urgency_of(node(external="", ss=SS_MPTCP))[0] == "critical")
ok("security kernel, not exposed = care",
   hc.urgency_of(node(external=""))[0] == "care")
ok("point-release kernel on an exposed node = care",
   hc.urgency_of(node(policy=POLICY_POINT))[0] == "care")
ok("vendor kernel = care", hc.urgency_of(node(policy=POLICY_PI))[0] == "care")

plan, ev = hc.compute_plan(node(apt=APT_ON), {}, None, now=T0, tz=BERLIN)
ok("test 14: no window -> no plan, no events", plan is None and ev == [])
plan, ev = hc.compute_plan(node(), {}, None, now=T0, tz=BERLIN)
ok("window: the plan is the window hour on the first day >= 24 h after announcement",
   plan and plan["at"] == "2026-11-08T03:15:00Z" and plan["at_local"] == "2026-11-08 04:15 CET"
   and plan["announced"] == "2026-11-06T10:00:00Z" and plan["urgency"] == "critical"
   and plan["source"] == "kernel" and plan["state"] == "planned", plan)
ok("announced once, with the urgency and the reason",
   [e[0] for e in ev] == ["reboot.planned"] and "critical" in ev[0][2], ev)
later, ev2 = hc.compute_plan(node(), {}, plan, now=T0 + timedelta(hours=1), tz=BERLIN)
ok("an hour later: same announcement, same time, no event",
   later["at"] == plan["at"] and later["announced"] == plan["announced"] and ev2 == [], ev2)
ok("the lead is 24 h, not 'next night': announced at 01:00 local, the plan is the night after",
   hc.compute_plan(node(), {}, None, now=datetime(2026, 11, 6, 0, 0, tzinfo=timezone.utc),
                   tz=BERLIN)[0]["at_local"] == "2026-11-07 04:15 CET")
care, ev = hc.compute_plan(node(external=""), {}, None, now=T0, tz=BERLIN)
ok("care plan without protection: same time, urgency care",
   care["urgency"] == "care" and care["at"] == "2026-11-08T03:15:00Z", care)
live = hc.live_protections(prot, {"joerg-desk"}, today=T0.date())
held, ev = hc.compute_plan(node(external=""), live, None, now=T0, tz=BERLIN)
ok("test 15: a protection until 13.11. moves a care plan to the window on 14.11.",
   held["at_local"] == "2026-11-14 04:15 CET" and held["held_by"][0]["instance"] == "joerg-desk"
   and held["overridden"] == [] and held["deferred"] is True, held)
ok("... announced as planned, held by joerg-desk",
   ev[0][0] == "reboot.planned" and "joerg-desk" not in ev[0][2] and held["held_by"], ev)
moved, ev = hc.compute_plan(node(external=""), live, care, now=T0 + timedelta(minutes=1), tz=BERLIN)
ok("a protection arriving later MOVES the plan, said as reboot.moved naming the holder",
   moved["at_local"] == "2026-11-14 04:15 CET" and ev[0][0] == "reboot.moved"
   and "joerg-desk" in ev[0][2], ev)
crit, ev = hc.compute_plan(node(), live, None, now=T0, tz=BERLIN)
ok("the same protection on a critical plan: time stays, protection listed as overridden",
   crit["at"] == "2026-11-08T03:15:00Z" and crit["held_by"] == []
   and crit["overridden"][0]["instance"] == "joerg-desk" and crit["deferred"] is False, crit)
ok("... and the override is its own event, subject = the instance",
   [e[0] for e in ev] == ["reboot.planned", "reboot.override"] and ev[1][1] == "joerg-desk", ev)
ok("recomputed: the override is not said twice",
   [e[0] for e in hc.compute_plan(node(), live, crit, now=T0 + timedelta(minutes=1), tz=BERLIN)[1]] == [])
ok("care: may_reboot says held; critical: may reboot",
   hc.may_reboot(node(external=""), held, live) == (False, "held by joerg-desk")
   and hc.may_reboot(node(), crit, live)[0] is True)
ok("no reboot required any more: may not",
   hc.may_reboot(node(running="6.12.115+deb13.1-amd64"), crit, {})[0] is False)

print("\n-- the hand-made plan (2.7.1, D10)")
m = hc.manual_plan("2026-11-08 04:15", "glibc update", "joerg", now=T0, tz=BERLIN)
ok("a plan by hand: care, the maker, the local time, not shortened",
   m["source"] == "manual" and m["urgency"] == "care" and m["by"] == "joerg"
   and m["at"] == "2026-11-08T03:15:00Z" and m["shortened"] is False, m)
m2 = hc.manual_plan("now", "measurement", "joerg", critical=True, now=T0, tz=BERLIN)
ok("'now' is now, shortened, critical when said", m2["at"] == "2026-11-06T10:00:00Z"
   and m2["shortened"] is True and m2["urgency"] == "critical")
for bad, what in (("", "glibc"), ("2026-11-01 04:15", "r"), ("tomorrow", "r")):
    try:
        hc.manual_plan(bad, what if bad else "", "j", now=T0, tz=BERLIN)
        ok(f"refused: {bad!r}", False)
    except ValueError:
        ok(f"refused: {bad!r} / reason {what!r}", True)
mp, ev = hc.compute_plan(node(running="6.12.115+deb13.1-amd64", external=""), live, m, now=T0, tz=BERLIN)
ok("a care plan by hand is held like a computed one: moved to the day after the protection",
   mp["at_local"] == "2026-11-14 04:15 CET" and ev[0][0] == "reboot.moved", (mp, ev))
mp, ev = hc.compute_plan(node(running="6.12.115+deb13.1-amd64"), live, m2, now=T0, tz=BERLIN)
ok("a critical plan by hand overrides", mp["at"] == m2["at"] and mp["overridden"], mp)
ok("due: at T and within the grace, not before, not after",
   not hc.due(m, now=T0) and hc.due(m, now=datetime(2026, 11, 8, 3, 15, tzinfo=timezone.utc))
   and hc.due(m, now=datetime(2026, 11, 8, 3, 29, tzinfo=timezone.utc))
   and not hc.due(m, now=datetime(2026, 11, 8, 3, 31, tzinfo=timezone.utc)))
ok("a hand-made plan may reboot with no kernel waiting",
   hc.may_reboot(node(running="6.12.115+deb13.1-amd64"), m, {})[0] is True)

print("\n-- the plan closes with proof, or shows as overdue (2.7.1, test 17)")
rebooting = dict(plan, state="rebooting", rebooting_at="2026-11-08T03:15:30Z")
after, ev = hc.compute_plan(node(running="6.12.115+deb13.1-amd64", boot="2026-11-08T03:16:40Z"),
                            {}, rebooting, now=datetime(2026, 11, 8, 3, 18, tzinfo=timezone.utc), tz=BERLIN)
ok("booted after the start: reboot.done with the running kernel, plan gone",
   after is None and ev[0][0] == "reboot.done" and "6.12.115" in ev[0][2], ev)
same, ev = hc.compute_plan(node(boot="2026-11-05T09:20:00Z"), {}, rebooting,
                           now=datetime(2026, 11, 8, 3, 18, tzinfo=timezone.utc), tz=BERLIN)
ok("not booted yet: the plan stays `rebooting`, nothing said within the grace",
   same["state"] == "rebooting" and ev == [], (same, ev))
stuck, ev = hc.compute_plan(node(boot="2026-11-05T09:20:00Z"), {}, rebooting,
                            now=datetime(2026, 11, 8, 3, 40, tzinfo=timezone.utc), tz=BERLIN)
ok("started and never booted past the grace: reboot.overdue, once",
   ev[0][0] == "reboot.overdue" and stuck["overdue_said"] is True
   and hc.compute_plan(node(boot="2026-11-05T09:20:00Z"), {}, stuck,
                       now=datetime(2026, 11, 8, 3, 41, tzinfo=timezone.utc), tz=BERLIN)[1] == [])
byhand, ev = hc.compute_plan(node(running="6.12.115+deb13.1-amd64"), {}, plan, now=T0, tz=BERLIN)
ok("rebooted by hand before T: reboot.done, plan gone",
   byhand is None and ev[0][0] == "reboot.done")
nowin, ev = hc.compute_plan(node(apt=APT_ON), {}, plan, now=T0, tz=BERLIN)
ok("window removed while a kernel waits: reboot.cancelled, plan gone",
   nowin is None and ev[0][0] == "reboot.cancelled")
late, ev = hc.compute_plan(node(), {}, plan, now=datetime(2026, 11, 8, 4, 0, tzinfo=timezone.utc), tz=BERLIN)
ok("T passed without a reboot (minute job dead): state overdue, said once",
   late["state"] == "overdue" and ev[0][0] == "reboot.overdue"
   and hc.compute_plan(node(), {}, late, now=datetime(2026, 11, 8, 4, 1, tzinfo=timezone.utc),
                       tz=BERLIN)[1] == [], (late, ev))

print("\n-- the readers carry the plan and the protections (2.7.5, 0.6)")
f = dict(node(external=""), plan=held, protections=hc._held_rows(live))
en = hc.status_lines(f)
de = hc.status_lines(f, de=True)
ok("four lines: the plan line after the kernel line, warn",
   len(en) == 4 and en[1][0] == "warn" and en[1][1].startswith("Reboot planned: 2026-11-14 04:15 CET (care)")
   and "held by joerg-desk until 2026-11-13" in en[1][1], en)
ok("German: Geplanter Neustart ... Pflege ... gehalten durch",
   de[1][1].startswith("Geplanter Neustart: 2026-11-14 04:15 CET (Pflege)")
   and "gehalten durch joerg-desk bis 2026-11-13" in de[1][1], de)
ok("the kernel line points at the plan instead of systemctl reboot",
   "see the plan" in en[0][1] and "systemctl" not in en[0][1])
fc = dict(node(), plan=crit, protections=hc._held_rows(live))
ok("critical: CRITICAL / KRITISCH and the overridden protection named",
   "(CRITICAL)" in hc.status_lines(fc)[1][1] and "protection overridden: joerg-desk" in hc.status_lines(fc)[1][1]
   and "KRITISCH" in hc.status_lines(fc, de=True)[1][1])
fo = dict(node(), plan=late)
ok("overdue: err line, says so", hc.status_lines(fo)[1][0] == "err"
   and hc.status_lines(fo)[1][1].startswith("OVERDUE"))
fp = dict(node(running="6.12.115+deb13.1-amd64", external=""), protections=hc._held_rows(live))
ok("protections without a plan: an ok line naming them",
   hc.status_lines(fp)[1] == ("ok", "Reboot protection: joerg-desk until 2026-11-13 (Schulung bis Mittwoch)"),
   hc.status_lines(fp)[1])
blk = hc.fleet_host(f)
ok("fleet block 0.6: reboot_plan without the reason, protections as count and until",
   blk["reboot_plan"] == {"at": "2026-11-14T03:15:00Z", "at_local": "2026-11-14 04:15 CET",
                          "urgency": "care", "source": "kernel", "held": True,
                          "overridden": False, "state": "planned"}
   and blk["protections"] == {"count": 1, "until": "2026-11-13"}
   and "reason" not in json.dumps(blk), blk)
kinds = [x["kind"] for x in hc.fleet_attention(f)]
ok("kinds: reboot_required, reboot_planned, reboot_deferred",
   kinds == ["reboot_required", "reboot_planned", "reboot_deferred"], kinds)
ok("overdue kind", "reboot_overdue" in [x["kind"] for x in hc.fleet_attention(fo)])
ok("a healthy node with a plan by hand: planned kind only, not required",
   [x["kind"] for x in hc.fleet_attention(dict(healthy, plan=m))] == ["reboot_planned"])
ok("the plan file round-trips", hc.save_plan(held, tmp) and hc.load_plan(tmp)["at"] == held["at"]
   and hc.clear_plan(tmp) and hc.load_plan(tmp) is None)
ok("the protections file round-trips",
   hc.save_protections(prot, tmp) and hc.load_protections(tmp) == prot)

print()
print("FAILED" if fails else "ALL PASS", f"({fails} failing)")
sys.exit(1 if fails else 0)
