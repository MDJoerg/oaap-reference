#!/usr/bin/env python3
"""Advisories (oaap.core.updates 2.8, RFC-0070 stage 1, 0.1.226).

The rules the measurement of 2026-11-05 produced, executed without a
network -- the list, dpkg and apt are recorded answers:

1. the list is parsed with a fixed grammar; a line it does not know is
   counted, never read as "no advisory"; the headers of 24 years parse;
2. the signed kernel is `linux` in the list (6.12.115+2 -> 6.12.115-2);
3. only an installed version OLDER than the fix is open; the newest DSA
   per package counts; for the kernel the newest installed image counts;
4. a package from another archive is `unknown`, never clean;
5. a read that fails keeps the old list and becomes stale after two
   days -- and without any list there is no count, never 0;
6. the readers: CLI/portal line, fleet block (no names), attention;
7. the "last run" is the timer's stamp, and without it the line says
   "no run yet".

Run: python3 test/test_advisories.py   (with dpkg: also the real comparator)
"""
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services"))

import advisories as adv  # noqa: E402
import host_care as hc  # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f"  -- {detail}"))


def vkey(v):
    """A comparator for the fixtures only (plain numbers); the module
    uses dpkg's own, test 3b runs it where dpkg exists."""
    import re
    return tuple(int(n) for n in re.findall(r"\d+", v))


def older(a, b):
    return vkey(a) < vkey(b)


LIST = """[04 Nov 2026] DSA-6607-1 apt - security update
\t{CVE-2026-109518}
\t[trixie] - apt 3.0.4
[02 Nov 2026] DSA-6602-1 linux - security update
\t{CVE-2026-1 CVE-2026-2}
\t[trixie] - linux 6.12.115-2
\t[bookworm] - linux 6.1.150-1
[30 Sep 2026] DSA-6500-1 openssl - security update
\t{CVE-2026-3}
\t[trixie] - openssl 3.5.4-1~deb13u1
\tNOTE: fixed in testing at time of DSA
[14 Aug 2026] DSA-6400-1 util-linux - security update
\t[trixie] - util-linux 2.41.5-0+deb13u1
[01 Jan 2026] DSA-6000-1 openssl - security update
\t[trixie] - openssl 3.5.1-1~deb13u1
[10 Jan 2026] DSA-6001-1 containerd - security update
\t[trixie] - containerd 1.7.24~ds1-6+deb13u1
[17 Aug 2010] DSA-2092-1  lxr-cvs - cross-site scripting
[13 Feb 2009] DSA-1724-1 fai-kernels linux-2.6 user-mode-linux - several vulnerabilities
\t[woody] - mantis <not-affected> (Vulnerable code not present)
[01 Jan 2004] DSA-123 linux-kernel-2.4.18 - several vulnerabilities
\t[woody] - gps  0.9.4-1woody1
"""

# 1. the grammar
entries, skipped = adv.parse(LIST)
ok("1 every header of 24 years parses (DSA-123, double space, several packages)",
   len(entries) == 9, [e["id"] for e in entries])
ok("1 the one malformed release line is counted, not dropped silently", skipped == 1, skipped)
ok("1 <not-affected> is no fix", "woody" not in entries[7]["fixed"], entries[7])
ok("1 the date is ISO", entries[0]["date"] == "2026-11-04", entries[0]["date"])
ok("1 garbage before any header is counted", adv.parse("hello\n" + LIST)[1] == 2)

# 2. the kernel's name
ok("2 linux-signed-amd64 6.12.115+2 is linux 6.12.115-2",
   adv.source_name("linux-signed-amd64", "6.12.115+2") == ("linux", "6.12.115-2"))
ok("2 linux-signed-arm64 too", adv.source_name("linux-signed-arm64", "6.12.100+1")[0] == "linux")
ok("2 any other name stays", adv.source_name("openssl", "3.5.4") == ("openssl", "3.5.4"))

DPKG = "\n".join([
    "apt\t3.0.3\tapt\tinstall ok installed",
    "apt\t3.0.3\tlibapt-pkg7.0\tinstall ok installed",
    "openssl\t3.5.1-1~deb13u1\tlibssl3t64\tinstall ok installed",
    "util-linux\t2.41.5-0+deb13u1\tmount\tinstall ok installed",
    "linux-signed-amd64\t6.12.100+1\tlinux-image-6.12.100+deb13-amd64\tinstall ok installed",
    "linux-signed-amd64\t6.12.115+2\tlinux-image-6.12.115+deb13.1-amd64\tinstall ok installed",
    "containerd\t1.7.0\tcontainerd.io\tinstall ok installed",
    "gone\t1.0\tgone\tdeinstall ok config-files",
    "oldlib\t0.9\toldlib1\tinstall ok installed",
])
inst = adv.installed(DPKG)
ok("2 removed packages are not installed", "gone" not in inst)
ok("2 two kernel images, one source", inst["linux"]["versions"] == {"6.12.100-1", "6.12.115-2"},
   inst.get("linux"))

# 4. origins
GLOBAL = """Package files:
 100 /var/lib/dpkg/status
     release a=now
 500 http://deb.debian.org/debian-security trixie-security/main amd64 Packages
     release v=13,o=Debian,a=stable-security,n=trixie-security,l=Debian-Security,c=main,b=amd64
     origin deb.debian.org
 500 http://deb.debian.org/debian trixie/main amd64 Packages
     release v=13.2,o=Debian,a=stable,n=trixie,l=Debian,c=main,b=amd64
     origin deb.debian.org
 500 https://download.docker.com/linux/debian trixie/stable amd64 Packages
     release o=Docker,a=trixie,l=Docker CE,c=stable,b=amd64
     origin download.docker.com
"""
rel = adv.release_origins(GLOBAL)
ok("4 the archive line maps to its release origin",
   rel.get("http://deb.debian.org/debian-security trixie-security/main") == "Debian"
   and rel.get("https://download.docker.com/linux/debian trixie/stable") == "Docker", rel)
POL = """apt:
  Installed: 3.0.3
  Candidate: 3.0.4
  Version table:
     3.0.4 500
        500 http://deb.debian.org/debian-security trixie-security/main amd64 Packages
 *** 3.0.3 100
        100 /var/lib/dpkg/status
containerd.io:
  Installed: 1.7.0
  Candidate: 1.7.0
  Version table:
 *** 1.7.0 500
        500 https://download.docker.com/linux/debian trixie/stable amd64 Packages
        100 /var/lib/dpkg/status
oldlib1:
  Installed: 0.9
  Candidate: 0.9
  Version table:
 *** 0.9 100
        100 /var/lib/dpkg/status
libssl3t64:
  Installed: 3.5.1-1~deb13u1
  Candidate: 3.5.4-1~deb13u1
  Version table:
     3.5.4-1~deb13u1 500
        500 http://deb.debian.org/debian-security trixie-security/main amd64 Packages
 *** 3.5.1-1~deb13u1 100
        100 /var/lib/dpkg/status
linux-image-6.12.115+deb13.1-amd64:
  Installed: 6.12.115-2
  Candidate: 6.12.115-2
  Version table:
 *** 6.12.115-2 500
        500 http://deb.debian.org/debian-security trixie-security/main amd64 Packages
        100 /var/lib/dpkg/status
mount:
  Installed: 2.41.5-0+deb13u1
  Candidate: 2.41.5-0+deb13u1
  Version table:
 *** 2.41.5-0+deb13u1 500
        500 http://deb.debian.org/debian trixie/main amd64 Packages
        100 /var/lib/dpkg/status
"""
po = adv.package_origins(POL, rel)
ok("4 apt: the candidate's archive counts (installed only in dpkg status)", po["apt"] == {"Debian"}, po)
kinds = adv.classify(inst, po)
ok("4 openssl and the kernel are Debian's (security archive)",
   kinds["openssl"] == "debian" and kinds["linux"] == "debian", kinds)
ok("4 Docker CE is vendor", kinds["containerd"] == "vendor", kinds)
ok("4 a package no archive carries is local", kinds["oldlib"] == "local", kinds)

# 3. the comparison
items, covered = adv.compare(entries, inst, "trixie", kinds, older)
names = [i["package"] for i in items]
ok("3 open: apt and openssl, oldest first", names == ["openssl", "apt"], items)
ok("3 the NEWEST DSA per package counts (openssl 3.5.4, not 3.5.1)",
   items[0]["fixed"] == "3.5.4-1~deb13u1" and items[0]["dsa"] == "DSA-6500-1", items[0])
ok("3 util-linux installed == fixed is not open", "util-linux" not in names)
ok("3 the kernel: the newest installed image counts (115-2 == fix)", "linux" not in names)
ok("3 a vendor package with a Debian DSA is not compared (containerd)", "containerd" not in names)
ok("3 covered = installed packages with an advisory", covered == 4, covered)
inst_old = adv.installed(DPKG.replace("6.12.115+2", "6.12.111+1"))
it2, _ = adv.compare(entries, inst_old, "trixie", kinds, older)
ok("3 an older newest kernel is open", "linux" in [i["package"] for i in it2], it2)
ok("3 another suite has no advisories here", adv.compare(entries, inst, "forky", kinds, older)[0] == [])
if shutil.which("dpkg"):
    ok("3b dpkg: tilde sorts before (3.5.4-1~deb13u1 < 3.5.4-1)",
       adv.dpkg_older("3.5.4-1~deb13u1", "3.5.4-1"))
    ok("3b dpkg: the epoch wins (1:1.0 > 2.0)", adv.dpkg_older("2.0", "1:1.0"))
    ok("3b dpkg: a string comparison would be wrong (3.10 > 3.9)", adv.dpkg_older("3.9", "3.10"))
else:
    print("SKIP 3b no dpkg here")

ok("security candidates from `apt list --upgradable`",
   adv.security_candidates("Listing...\napt/stable-security 3.0.4 amd64 [upgradable from: 3.0.3]\n"
                           "bsdutils/stable,stable-security 1:2.41.5 amd64 [upgradable from: 1:2.41]\n"
                           "foo/stable 1.1 amd64 [upgradable from: 1.0]\n") == 2)

# 5. the cache
d = tempfile.mkdtemp()
T0 = datetime(2026, 11, 6, 6, 0, tzinfo=timezone.utc)


def run_fixture(cmd, timeout=60, input=None):
    if cmd[0] == "dpkg-query":
        return DPKG + "\n"
    if cmd[:2] == ["apt-cache", "policy"]:
        return GLOBAL if len(cmd) == 2 else POL
    if cmd[0] == "apt":
        return "Listing...\n"
    return ""


def fail_fetch():
    raise OSError("no route")


osr = os.path.join(d, "os-release")
open(osr, "w").write("VERSION_CODENAME=trixie\n")
st = adv.refresh(d, now=T0, run=run_fixture, fetch=fail_fetch, older=older,
                 dpkg_status=osr, upgrade_stamp="/nonexistent", os_release=osr)
blk = adv.care_block(st, T0)
ok("5 never a list: no count at all, never 0", blk and blk["open"] is None and blk["stale"], blk)
ok("5 ... and the line says the list could not be read",
   hc.advisory_line(blk)[0] == "warn" and "could not be read" in hc.advisory_line(blk)[1])
st = adv.refresh(d, now=T0 + timedelta(minutes=10), run=run_fixture, fetch=lambda: LIST,
                 older=older, dpkg_status=osr, upgrade_stamp="/nonexistent", os_release=osr)
ok("5 a failed read is retried hourly, not every ten minutes", st.get("read_at") is None, st)
st = adv.refresh(d, now=T0 + timedelta(hours=1, minutes=1), run=run_fixture, fetch=lambda: LIST,
                 older=older, dpkg_status=osr, upgrade_stamp="/nonexistent", os_release=osr)
blk = adv.care_block(st, T0 + timedelta(hours=1, minutes=1))
ok("5 after the hour: read, 2 open, oldest 2026-09-30 (openssl)",
   blk["open"] == 2 and blk["oldest"] == "2026-09-30" and blk["oldest_package"] == "openssl", blk)
ok("5 the list lies beside the registry, world-readable",
   oct(os.stat(os.path.join(d, adv.LIST_FILE)).st_mode)[-3:] == "644")
st = adv.refresh(d, now=T0 + timedelta(days=3), run=run_fixture, fetch=fail_fetch, older=older,
                 dpkg_status=osr, upgrade_stamp="/nonexistent", os_release=osr)
blk = adv.care_block(st, T0 + timedelta(days=3))
ok("5 three days of failed reads: the old count stays, marked stale",
   blk["open"] == 2 and blk["stale"] is True, blk)
ok("5 ... and the line says STALE, not 0", "STALE" in hc.advisory_line(blk)[1], hc.advisory_line(blk))
st = adv.refresh(d, now=T0 + timedelta(days=3, hours=2), run=run_fixture,
                 fetch=lambda: "<html>login</html>", older=older,
                 dpkg_status=osr, upgrade_stamp="/nonexistent", os_release=osr)
ok("5 a page that is not the list is refused (the cache stays)",
   "first line" in st.get("last_error", "") and adv.care_block(st, T0)["open"] == 2, st.get("last_error"))
ok("5 need_read: an automatic-update run after the read triggers a read",
   adv.need_read({"read_at": "2026-11-06T06:00:00Z"}, T0 + timedelta(hours=1),
                 upgrade_stamp=(T0 + timedelta(minutes=30)).timestamp()))
ok("5 need_read: fresh and no run since -> no read",
   not adv.need_read({"read_at": "2026-11-06T06:00:00Z"}, T0 + timedelta(hours=1),
                     upgrade_stamp=(T0 - timedelta(minutes=30)).timestamp()))
shutil.rmtree(d)

# 6. the readers
blk = {"source": "debian-dsa", "read_at": "2026-11-06T06:00:00Z", "stale": False, "open": 13,
       "oldest": "2026-08-14", "oldest_package": "util-linux", "items": [{"package": "openssl"}],
       "unknown": ["docker-ce"], "local": [], "security_candidates": 13}
facts = {"measured": "2026-11-06T06:01:00Z", "kernel": {"running": "a", "installed": "a"},
         "auto_updates": {"state": "ok", "last_run": ""}, "gateway": {"mptcp": "off"},
         "advisories": blk}
lines = dict((t.split(":")[0], (s, t)) for s, t in hc.status_lines(facts))
ok("6 the line: 13 open, the oldest with its package",
   lines["Advisories"][0] == "warn" and "13 open, the oldest since 2026-08-14 (util-linux)"
   in lines["Advisories"][1], lines.get("Advisories"))
ok("6 German too", any("13 offen" in t for _, t in hc.status_lines(facts, de=True)))
ok("6 unknown packages are named as not covered", "1 package(s) from other archives" in lines["Advisories"][1])
fb = hc.fleet_host(facts)
ok("6 fleet: count, oldest, stale -- no package name",
   fb["advisories"] == {"open": 13, "oldest": "2026-08-14", "stale": False,
                        "read_at": "2026-11-06T06:00:00Z", "unknown": 1}
   and "openssl" not in str(fb) and "util-linux" not in str(fb), fb)
kinds_ = [a["kind"] for a in hc.fleet_attention(facts)]
ok("6 attention: advisories_open", "advisories_open" in kinds_, kinds_)
ok("6 attention carries no package name", "util-linux" not in str(hc.fleet_attention(facts)))
ok("6 the door an instance reads carries nothing of it", "advis" not in str(hc.door_facts(facts)))
facts["advisories"] = None
ok("6 no reading: 'not read yet', unknown -- not 0",
   any(s == "unknown" and "not read yet" in t for s, t in hc.status_lines(facts)))
ok("6 no reading: no fleet block", "advisories" not in hc.fleet_host(facts))

# 7. the stamp
ok("7 the timer's stamp, not the program's", hc.STAMP.endswith("/upgrade-stamp")
   and "unattended" not in hc.STAMP, hc.STAMP)
ok("7 no stamp: 'no run yet'", any("no run yet" in t for _, t in hc.status_lines(facts)))
facts["kernel"]["marker"] = True
ok("7b the marker without a kernel: a hint, ok, no plan",
   any(s == "ok" and "library" in t for s, t in hc.status_lines(facts)))

print()
print("FAILED" if fails else "ALL PASS", f"({fails} failing)")
sys.exit(1 if fails else 0)
