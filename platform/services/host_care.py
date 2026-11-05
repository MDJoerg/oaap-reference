"""Host care (oaap.core.updates 2.6): the operating system under the
platform, measured by the node and read in three places.

Why this exists -- 2026-11-05, CVE-2026-98432: the kernel fix had been
INSTALLED on the internet node for two days and was not running (no
reboot in ten weeks); four nodes had no automatic updates; on one node
the auto-update package was installed by hand and did nothing, because
the periodic switch it is supposed to write was missing. All of it was
a fact on the machine. Nothing on the platform said so.

The rules the measurements produced live in functions, not in branches,
so a test can execute them without a node (the disks.py precedent):

- a reboot is required when the newest installed kernel OF THE SAME
  FLAVOUR is newer than the running one -- by comparison, never by the
  distribution's marker file (`/var/run/reboot-required` is written by
  a hook that ships with unattended-upgrades; on a node without the
  package the marker never appears and "no marker" read as "fine" while
  a newer kernel waited on disk -- oaapx02);
- automatic updates are ONE verdict from THREE parts (program, periodic
  switch, timer), each named when it is the one missing -- installing
  the package alone does not set the switch;
- the gateway's listeners are read from the gateway's own network
  namespace: `ss -M` lists the MPTCP listeners, and a running listener
  keeps what it was created with (a sysctl flipped at runtime changes
  nothing -- measured on oaap-test);
- every measurement carries its time, because right after a reboot a
  stored value describes the old kernel.

Everything that touches the host goes through `sh()`, so the tests can
replace it. Two languages on purpose: `oaap status` speaks English
like the rest of the CLI, the portal speaks German like the rest of the
portal -- and both read the SAME file, so they cannot disagree.
"""
import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone

SCHEMA = "oaap.host-care/0.1"
FILE_NAME = "host-care.json"
# The platform's own file beside the distribution's defaults; 50 is the
# package's, 52 sorts after it and therefore wins where both speak.
APT_CONF = "/etc/apt/apt.conf.d/52oaap-care"
REBOOT_MARKER = "/var/run/reboot-required"
STAMP = "/var/lib/apt/periodic/unattended-upgrades-stamp"
GATEWAY_CONTAINER = "oaap-gateway-1"
STALE_AFTER = 600           # the minute job re-measures past this
OLD_AFTER = 86400           # a reader says so past this
PACKAGE = "unattended-upgrades"
TIMER = "apt-daily-upgrade.timer"


def sh(cmd, timeout=30, env=None):
    """stdout of a command, '' when it fails or is missing."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, env=env)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return r.stdout if r.returncode == 0 else ""


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- the kernel ------------------------------------------------------------

def kernel_flavour(release):
    """`6.12.115+deb13.1-amd64` -> `-amd64`; `6.18.50+rpt-rpi-v8` -> `-rpi-v8`.

    The version and the distribution's ABI tag go, the flavour stays. A
    machine may hold several flavours, and a newer kernel of ANOTHER
    flavour is not an update for the running one.
    """
    return re.sub(r"^\d+(\.\d+)*(\+[^-]*)?", "", release or "")


def version_key(release):
    """Numbers in order of appearance -- enough to order Debian kernels
    of one flavour (`6.12.100+deb13` < `6.12.115+deb13.1`)."""
    return tuple(int(n) for n in re.findall(r"\d+", release or ""))


def installed_kernels(dpkg_lines):
    """Releases of installed kernel images from `dpkg-query -W` lines
    `<package> <status>` -- meta packages (linux-image-amd64) and the
    unsigned twins (`not-installed`) fall out."""
    out = []
    for line in dpkg_lines.splitlines():
        parts = line.split()
        if len(parts) < 2 or parts[-1] != "installed":
            continue
        name = parts[0]
        if not name.startswith("linux-image-"):
            continue
        rel = name[len("linux-image-"):]
        if rel and rel[0].isdigit():
            out.append(rel)
    return out


def newest_of_flavour(releases, flavour):
    same = [r for r in releases if kernel_flavour(r) == flavour]
    return max(same, key=version_key) if same else ""


def reboot_required(running, newest):
    """The verdict of 2.6.1: by comparison, never by the marker."""
    return bool(running and newest and version_key(newest) > version_key(running))


def kernel_facts(run=sh, marker_exists=None):
    running = run(["uname", "-r"]).strip()
    flavour = kernel_flavour(running)
    newest = newest_of_flavour(
        installed_kernels(run(["dpkg-query", "-W",
                               "-f=${Package} ${Status}\n", "linux-image-*"])),
        flavour)
    if marker_exists is None:
        marker_exists = os.path.exists(REBOOT_MARKER)
    return {"running": running, "installed": newest or running,
            "flavour": flavour,
            "reboot_required": reboot_required(running, newest),
            "marker": bool(marker_exists)}


# --- automatic updates -----------------------------------------------------

def apt_values(dump):
    """`apt-config dump` -> {key: value} (last wins, list items ignored)."""
    out = {}
    for line in dump.splitlines():
        m = re.match(r'^([A-Za-z0-9:_.-]+) "(.*)";\s*$', line)
        if m and not m.group(1).endswith("::"):
            out[m.group(1)] = m.group(2)
    return out


def auto_updates_verdict(installed, switch, timer):
    """ONE verdict from THREE parts, the missing one named (2.6.1)."""
    if not installed:
        return "error", "program not installed"
    missing = []
    if not switch:
        missing.append("periodic switch not set")
    if not timer:
        missing.append("timer not active")
    if missing:
        return "warn", "installed, but " + " and ".join(missing)
    return "ok", "program, switch and timer in place"


def reboot_window(values):
    """`off`, or the HH:MM the distribution will reboot at."""
    on = values.get("Unattended-Upgrade::Automatic-Reboot", "false").lower()
    at = values.get("Unattended-Upgrade::Automatic-Reboot-Time", "")
    if on in ("true", "1", "yes") and re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", at):
        return at
    return "off"


def auto_updates_facts(run=sh, stamp_mtime=None):
    status = run(["dpkg-query", "-W", "-f=${Status}", PACKAGE]).strip()
    installed = status.endswith("installed") and "not-installed" not in status
    values = apt_values(run(["apt-config", "dump"]))
    switch = (values.get("APT::Periodic::Unattended-Upgrade", "0") not in ("", "0")
              and values.get("APT::Periodic::Update-Package-Lists", "0") not in ("", "0"))
    timer = run(["systemctl", "is-active", TIMER]).strip() == "active"
    state, detail = auto_updates_verdict(installed, switch, timer)
    if stamp_mtime is None:
        try:
            stamp_mtime = os.path.getmtime(STAMP)
        except OSError:
            stamp_mtime = 0
    last = (datetime.fromtimestamp(stamp_mtime, timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%SZ") if stamp_mtime else "")
    return {"state": state, "detail": detail, "installed": installed,
            "switch": switch, "timer": timer, "last_run": last,
            "window": reboot_window(values)}


# --- the gateway's listeners ----------------------------------------------

def mptcp_listeners(ss_lines):
    """Public listeners from `ss -MlnH` -- every line IS an MPTCP listener
    (with one family `ss` prints no Netid column, so nothing is counted by
    the word 'mptcp'); loopback ones (Caddy's admin port, Docker's DNS)
    are not public and fall out."""
    out = []
    for line in ss_lines.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0] != "LISTEN":
            continue
        local = parts[3]
        host = local.rsplit(":", 1)[0]
        if host.startswith("127.") or host in ("[::1]", "::1", "localhost"):
            continue
        out.append(local)
    return out


def gateway_facts(run=sh):
    pid = run(["docker", "inspect", "-f", "{{.State.Pid}}", GATEWAY_CONTAINER]).strip()
    if not pid.isdigit() or pid == "0":
        return {"mptcp": "unknown", "listeners": [],
                "detail": "gateway container not running"}
    lines = run(["nsenter", "-t", pid, "-n", "ss", "-MlnH"])
    # `ss` prints nothing (and succeeds) when no MPTCP socket exists; a
    # failed nsenter also yields '' -- tell the two apart with a probe.
    if not lines and run(["nsenter", "-t", pid, "-n", "sh", "-c", "echo ok"]).strip() != "ok":
        return {"mptcp": "unknown", "listeners": [],
                "detail": "gateway namespace not readable"}
    public = mptcp_listeners(lines)
    return {"mptcp": "on" if public else "off", "listeners": public,
            "detail": (", ".join(public) + " negotiate MPTCP") if public
                      else "listeners are plain TCP"}


# --- the measurement -------------------------------------------------------

def measure(run=sh, os_release="/etc/os-release", **probes):
    pretty = ""
    try:
        with open(os_release, encoding="utf-8") as f:
            for line in f:
                if line.startswith("PRETTY_NAME="):
                    pretty = line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    auto = auto_updates_facts(run, probes.get("stamp_mtime"))
    return {
        "schema": SCHEMA,
        "measured": now_iso(),
        "os": pretty,
        "kernel": kernel_facts(run, probes.get("marker_exists")),
        "auto_updates": {k: v for k, v in auto.items() if k != "window"},
        "reboot_window": auto["window"],
        "gateway": gateway_facts(run),
    }


def care_path(apps_dir):
    return os.path.join(apps_dir, FILE_NAME)


def write(facts, apps_dir):
    path = care_path(apps_dir)
    os.makedirs(apps_dir, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(facts, f, indent=2)
    os.replace(tmp, path)
    os.chmod(path, 0o644)
    return path


def read(apps_dir):
    try:
        with open(care_path(apps_dir), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def is_stale(facts, now=None, after=STALE_AFTER):
    return age(facts, now) is None or age(facts, now) > after


def age(facts, now=None):
    """Seconds since the measurement, None when unreadable."""
    try:
        t = datetime.strptime(facts["measured"], "%Y-%m-%dT%H:%M:%SZ")
    except (KeyError, TypeError, ValueError):
        return None
    t = t.replace(tzinfo=timezone.utc)
    now = now if now is not None else datetime.now(timezone.utc)
    return (now - t).total_seconds()


def ensure_marker(facts, marker=REBOOT_MARKER):
    """2.6.3: with a window set, the distribution reboots only when its
    marker exists -- and the marker is written by a hook that runs when
    unattended-upgrades installs a kernel. A kernel installed before the
    switch was set never gets one (oaapx02). So the window writes it
    itself when the comparison says reboot. Returns True when written."""
    if facts.get("reboot_window", "off") == "off":
        return False
    if not facts.get("kernel", {}).get("reboot_required"):
        return False
    if os.path.exists(marker):
        return False
    try:
        with open(marker, "w", encoding="utf-8") as f:
            f.write("*** System restart required ***\n")
        return True
    except OSError:
        return False


# --- the settings the platform writes (2.6.3) ------------------------------

def conf_text(window="off"):
    lines = [
        "// Written by OAAP (oaap.core.updates 2.6). Change it with:",
        "//   sudo oaap node care auto-updates on",
        "//   sudo oaap node care reboot-window HH:MM | off",
        "// The distribution's own files are not touched; this one sorts",
        "// after 50unattended-upgrades and therefore wins where both speak.",
        'APT::Periodic::Update-Package-Lists "1";',
        'APT::Periodic::Unattended-Upgrade "1";',
    ]
    if window != "off":
        lines += [
            "// The node reboots itself at this local hour -- only when a newer",
            "// kernel is installed than the one running, also with users",
            "// logged in (a node is headless; a forgotten shell must not keep",
            "// a security fix from running).",
            'Unattended-Upgrade::Automatic-Reboot "true";',
            f'Unattended-Upgrade::Automatic-Reboot-Time "{window}";',
            'Unattended-Upgrade::Automatic-Reboot-WithUsers "true";',
        ]
    else:
        lines += ['Unattended-Upgrade::Automatic-Reboot "false";']
    return "\n".join(lines) + "\n"


def read_conf(path=APT_CONF):
    """The window this file holds, `off` when absent."""
    try:
        with open(path, encoding="utf-8") as f:
            return reboot_window(apt_values(f.read()))
    except OSError:
        return "off"


def write_conf(window="off", path=APT_CONF):
    text = conf_text(window)
    try:
        with open(path, encoding="utf-8") as f:
            if f.read() == text:
                return False
    except OSError:
        pass
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)
    os.chmod(path, 0o644)
    return True


def validate_window(at):
    at = (at or "").strip().lower()
    if at == "off":
        return "off"
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", at):
        raise ValueError("the window wants HH:MM in 24-hour form (e.g. 04:15) or 'off'")
    return at


def auto_updates_enable(run=sh, path=APT_CONF, say=print):
    """Install the program and WRITE THE SWITCH (2.6.3). Idempotent."""
    status = run(["dpkg-query", "-W", "-f=${Status}", PACKAGE]).strip()
    if not status.endswith("installed") or "not-installed" in status:
        say(f"Installing {PACKAGE} ...")
        env = dict(os.environ, DEBIAN_FRONTEND="noninteractive")
        # The pre-seeded answer first: with it the package writes its own
        # 20auto-upgrades too; without it, nothing (oaapx02).
        subprocess.run(["debconf-set-selections"], input=(
            f"{PACKAGE} {PACKAGE}/enable_auto_updates boolean true\n"),
            text=True, capture_output=True, env=env)
        r = subprocess.run(["apt-get", "install", "-y", "-q", PACKAGE],
                           capture_output=True, text=True, env=env, timeout=600)
        if r.returncode != 0:
            raise RuntimeError("apt-get install failed: "
                               + (r.stderr or r.stdout).strip()[-400:])
    else:
        say(f"{PACKAGE} is installed.")
    if write_conf(read_conf(path), path):
        say(f"Periodic switch written: {path}")
    else:
        say(f"Periodic switch already set: {path}")
    subprocess.run(["systemctl", "enable", "--now", "apt-daily.timer", TIMER],
                   capture_output=True, text=True)


def reboot_window_set(at, run=sh, path=APT_CONF, say=print):
    window = validate_window(at)
    status = run(["dpkg-query", "-W", "-f=${Status}", PACKAGE]).strip()
    if window != "off" and (not status.endswith("installed") or "not-installed" in status):
        raise ValueError("a reboot window needs automatic updates first: "
                         "sudo oaap node care auto-updates on")
    if write_conf(window, path):
        say(f"Reboot window {'cleared' if window == 'off' else 'set to ' + window}: {path}")
    else:
        say(f"Reboot window unchanged ({window}).")
    return window


# --- the three readers -----------------------------------------------------

def status_lines(facts, now=None, de=False):
    """[(state, text)] -- the kernel line, the auto-update line, the
    gateway line, each with its recommendation when it is not fine.
    One source for `oaap status` (English) and the portal (German)."""
    if not facts:
        return [("unknown",
                 "Pflegestand noch nicht gemessen — misst der Minutenlauf, "
                 "oder: sudo oaap node care" if de else
                 "Care state not measured yet — the minute job measures it, "
                 "or: sudo oaap node care")]
    out = []
    k = facts.get("kernel", {})
    win = facts.get("reboot_window", "off")
    if k.get("reboot_required"):
        if de:
            t = (f"Neustart nötig — läuft {k.get('running')}, installiert "
                 f"{k.get('installed')}. "
                 + (f"Der Knoten startet um {win} selbst neu."
                    if win != "off" else
                    "In einer ruhigen Stunde: sudo systemctl reboot"))
        else:
            t = (f"Kernel: REBOOT REQUIRED — running {k.get('running')}, "
                 f"installed {k.get('installed')}. "
                 + (f"The node reboots itself at {win}."
                    if win != "off" else
                    "In a quiet hour: sudo systemctl reboot"))
        out.append(("warn", t))
    else:
        out.append(("ok", (f"Kernel {k.get('running')} läuft, ist der neueste "
                           "installierte — kein Neustart nötig") if de else
                    (f"Kernel: {k.get('running')} running, newest installed "
                     "— no reboot needed")))
    a = facts.get("auto_updates", {})
    st = a.get("state", "unknown")
    last = (a.get("last_run") or "")[:16].replace("T", " ")
    if st == "ok":
        if de:
            t = ("Automatische Sicherheitsupdates: an"
                 + (f", letzter Lauf {last} UTC" if last else "")
                 + (f"; Neustart-Fenster {win}" if win != "off" else
                    "; kein Neustart-Fenster — ein neuer Kernel wartet auf einen "
                    "Neustart von Hand (sudo oaap node care reboot-window HH:MM)"))
        else:
            t = ("Auto-updates: on"
                 + (f", last run {last} UTC" if last else "")
                 + (f"; reboot window {win}" if win != "off" else
                    "; no reboot window — a new kernel waits for a manual "
                    "reboot (sudo oaap node care reboot-window HH:MM)"))
        out.append(("ok", t))
    elif st == "warn":
        out.append(("warn", (
            f"Automatische Updates: installiert, aber aus ({a.get('detail')}). "
            "Einschalten: sudo oaap node care auto-updates on") if de else (
            f"Auto-updates: INSTALLED BUT OFF ({a.get('detail')}). "
            "Switch on: sudo oaap node care auto-updates on")))
    elif st == "error":
        out.append(("err", (
            "Automatische Updates: NICHT installiert — Sicherheitsfixes warten "
            "auf einen Menschen. Einschalten: sudo oaap node care auto-updates on")
            if de else (
            "Auto-updates: NOT INSTALLED — security fixes wait for a human. "
            "Switch on: sudo oaap node care auto-updates on")))
    else:
        out.append(("unknown", "Automatische Updates: nicht messbar" if de
                    else "Auto-updates: not measurable"))
    g = facts.get("gateway", {})
    m = g.get("mptcp", "unknown")
    if m == "off":
        out.append(("ok", "Gateway: Listener sind reines TCP (MPTCP aus)" if de
                    else "Gateway: listeners are plain TCP (MPTCP off)"))
    elif m == "on":
        ports = ", ".join(g.get("listeners") or [])
        out.append(("warn", (
            f"Gateway: Listener handeln MPTCP aus ({ports}) — die Tür von "
            "CVE-2026-98432. Gateway mit dieser Plattformfassung neu anlegen: "
            "sudo oaap update") if de else (
            f"Gateway: listeners NEGOTIATE MPTCP ({ports}) — the door of "
            "CVE-2026-98432. Recreate the gateway with this platform version: "
            "sudo oaap update")))
    else:
        out.append(("unknown", (f"Gateway: nicht messbar ({g.get('detail', '')})")
                    if de else f"Gateway: not measurable ({g.get('detail', '')})"))
    a_s = age(facts, now)
    if a_s is not None and a_s > OLD_AFTER:
        days = int(a_s // 86400)
        out.append(("warn", (
            f"Messung ist {days} Tag(e) alt — läuft der Minutenlauf? "
            "Frisch: sudo oaap node care") if de else (
            f"Measurement is {days} day(s) old — is the minute job running? "
            "Fresh: sudo oaap node care")))
    return out


def worst(lines):
    order = {"ok": 0, "unknown": 1, "warn": 2, "err": 3}
    return max((s for s, _ in lines), key=lambda s: order.get(s, 1), default="unknown")


def print_status(facts, now=None):
    """The block `oaap status` prints (2.6.2)."""
    when = (facts or {}).get("measured", "")
    print("Host care" + (f" (measured {when[:16].replace('T', ' ')} UTC):" if when else ":"))
    for _state, text in status_lines(facts, now):
        print("  " + text)


def fleet_host(facts):
    """The `host` block of oaap.fleet.status 0.5 -- a whitelist, never a
    copy: no package list, no file contents, no sources."""
    if not facts:
        return None
    k = facts.get("kernel", {})
    a = facts.get("auto_updates", {})
    g = facts.get("gateway", {})
    block = {
        "measured": facts.get("measured", ""),
        "os": facts.get("os", ""),
        "kernel_running": k.get("running", ""),
        "kernel_installed": k.get("installed", ""),
        "reboot_required": bool(k.get("reboot_required")),
        "auto_updates": a.get("state", "unknown"),
        "reboot_window": facts.get("reboot_window", "off"),
        "gateway_mptcp": g.get("mptcp", "unknown"),
    }
    if a.get("last_run"):
        block["auto_updates_last_run"] = a["last_run"]
    return block


def fleet_attention(facts):
    """The three attention kinds of oaap.fleet.status 0.5."""
    if not facts:
        return []
    items = []
    k = facts.get("kernel", {})
    if k.get("reboot_required"):
        items.append({"kind": "reboot_required",
                      "detail": f"running {k.get('running')}, installed {k.get('installed')}"})
    a = facts.get("auto_updates", {})
    if a.get("state") in ("warn", "error"):
        items.append({"kind": "auto_updates_off", "detail": a.get("detail", "")})
    g = facts.get("gateway", {})
    if g.get("mptcp") == "on":
        items.append({"kind": "gateway_mptcp",
                      "detail": ", ".join(g.get("listeners") or []) + " negotiate MPTCP"})
    return items
