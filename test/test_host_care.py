#!/usr/bin/env python3
"""Host care (oaap.core.updates 2.6, oaap.fleet.status 0.5, 0.1.220).

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
  program first (tests 12, 13); with a window set, the marker is written
  when the comparison says reboot (2.6.3).

Run: python3 test/test_host_care.py
"""
import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout
from datetime import datetime, timezone

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
APT_WINDOW = APT_ON + """Unattended-Upgrade::Automatic-Reboot "true";
Unattended-Upgrade::Automatic-Reboot-Time "04:15";
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


def machine(running, dpkg, apt, uu="install ok installed", timer="active",
            ss=SS_MPTCP, pid="2620"):
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
ok("kernel facts carry both kernels and the verdict, marker only as a hint",
   k["running"] == "6.12.111+deb13-amd64" and k["installed"] == "6.12.115+deb13.1-amd64"
   and k["reboot_required"] and k["marker"] is False, k)

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
ok("the window is read from the dump", a["window"] == "04:15")
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
                   os_release=osr, marker_exists=False, stamp_mtime=0)
ok("the measurement is dated and names the OS",
   facts["measured"].endswith("Z") and facts["os"].startswith("Debian"), facts)
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
ok("fleet block: the whitelist of 0.5", blk == {
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
ok("document 0.5 carries host and the kinds", doc["schema"] == "oaap.fleet.status/0.5"
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
ok("a measurement older than a day gets a fourth, warning line",
   hc.status_lines(healthy, now=datetime(2027, 1, 1, tzinfo=timezone.utc))[-1][0] == "warn")
ok("stale after ten minutes, not before",
   not hc.is_stale(healthy) and hc.is_stale(healthy, now=datetime(2027, 1, 1, tzinfo=timezone.utc)))
out = io.StringIO()
with redirect_stdout(out):
    hc.print_status(back)
ok("print_status: header with the time, three indented lines",
   out.getvalue().startswith("Host care (measured ") and out.getvalue().count("\n  ") == 3, out.getvalue())

print("\n-- the settings file (2.6.3, tests 12 and 13)")
conf = os.path.join(tmp, "52oaap-care")
ok("absent file -> window off", hc.read_conf(conf) == "off")
ok("written once", hc.write_conf("off", conf) is True)
ok("written again: nothing changed", hc.write_conf("off", conf) is False)
text = open(conf).read()
ok("the switch is in the file, both lines",
   'APT::Periodic::Unattended-Upgrade "1";' in text
   and 'APT::Periodic::Update-Package-Lists "1";' in text)
ok("off means Automatic-Reboot false, no time", 'Automatic-Reboot "false"' in text
   and "Automatic-Reboot-Time" not in text)
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
ok("window set: file holds true + time + WithUsers", w == "04:15"
   and 'Automatic-Reboot "true"' in open(conf).read()
   and 'Automatic-Reboot-Time "04:15"' in open(conf).read()
   and 'Automatic-Reboot-WithUsers "true"' in open(conf).read(), open(conf).read())
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

print("\n-- the marker the window needs (2.6.3, oaapx02)")
marker = os.path.join(tmp, "reboot-required")
ok("no window: never written", not hc.ensure_marker(dict(facts, reboot_window="off"), marker)
   and not os.path.exists(marker))
ok("window, no reboot needed: not written",
   not hc.ensure_marker(dict(healthy, reboot_window="04:15"), marker))
ok("window and reboot needed: written once",
   hc.ensure_marker(dict(facts, reboot_window="04:15"), marker) and os.path.exists(marker)
   and not hc.ensure_marker(dict(facts, reboot_window="04:15"), marker))

print()
print("FAILED" if fails else "ALL PASS", f"({fails} failing)")
sys.exit(1 if fails else 0)
