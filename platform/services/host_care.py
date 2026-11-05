"""Host care (oaap.core.updates 2.6 and 2.7): the operating system under
the platform, measured by the node and read in three places -- and,
since 2.7 (RFC-0069), the node's own reboot: planned, announced, held
by an instance that says why, overridden when the facts say so.

Why this exists -- 2026-11-05, CVE-2026-98432: the kernel fix had been
INSTALLED on the internet node for two days and was not running (no
reboot in ten weeks); four nodes had no automatic updates; on one node
the auto-update package was installed by hand and did nothing, because
the periodic switch it is supposed to write was missing. All of it was
a fact on the machine. Nothing on the platform said so.

And why 2.7 -- the same day: the window of 2.6 rebooted oaap-demo and
ended a desk session with its open applications. A window is a time; it
knows nothing of the work on the node, and the distribution's reboot
(marker file plus clock) can never know it either. So the platform owns
the reboot: the minute job plans it, announces it 24 h ahead, re-measures
at the hour, and reboots. A protection per instance holds a `care`
reboot; a `critical` one (security kernel on an exposed node) overrides
it, visibly.

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
- the origin of the waiting kernel is read from the package manager's
  policy (security archive, main archive, vendor archive) -- the one
  fact the urgency needs that an advisory would refine, later;
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
from datetime import date, datetime, timedelta, timezone

SCHEMA = "oaap.host-care/0.2"
PLAN_SCHEMA = "oaap.reboot-plan/0.1"
FILE_NAME = "host-care.json"
PLAN_FILE_NAME = "reboot-plan.json"
PROTECTIONS_FILE_NAME = "reboot-protections.json"
# The platform's own file beside the distribution's defaults; 50 is the
# package's, 52 sorts after it and therefore wins where both speak.
APT_CONF = "/etc/apt/apt.conf.d/52oaap-care"
WINDOW_KEY = "OAAP::Care::Reboot-Window"
REBOOT_MARKER = "/var/run/reboot-required"
STAMP = "/var/lib/apt/periodic/unattended-upgrades-stamp"
GATEWAY_CONTAINER = "oaap-gateway-1"
STALE_AFTER = 600           # the minute job re-measures past this
OLD_AFTER = 86400           # a reader says so past this
PACKAGE = "unattended-upgrades"
TIMER = "apt-daily-upgrade.timer"
LEAD = 24 * 3600            # 2.7.1: a plan is announced at least this far ahead
DUE_GRACE = 900             # 2.7.1: the minute job reboots within this after T
PROTECT_DEFAULT_DAYS = 7    # 2.7.2
PROTECT_MAX_DAYS = 30
DEFER_AFTER_DAYS = 7        # 2.7.3: a care plan held longer is `deferred`


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


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def local_tz(timezone_file="/etc/timezone", localtime="/etc/localtime"):
    """The node's own clock: the window is a local hour (2.6.3).

    Debian 13 no longer writes /etc/timezone (measured 2026-11-05 on
    oaap-bernd, then on every node of the fleet): the zone is the target
    of the /etc/localtime link, `.../zoneinfo/Europe/Berlin`. Read the
    file first, then the link; only without both the offset of now,
    which carries no DST rule -- a plan across the switch night would be
    an hour off."""
    fallback = datetime.now().astimezone().tzinfo
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        return fallback
    name = zone_name(timezone_file, localtime)
    try:
        return ZoneInfo(name) if name else fallback
    except Exception:  # noqa: BLE001 -- a name the zoneinfo does not know
        return fallback


def zone_name(timezone_file="/etc/timezone", localtime="/etc/localtime"):
    """The zone's name ('Europe/Berlin') from the file, else from the
    link's target; '' when neither says."""
    try:
        with open(timezone_file, encoding="utf-8") as f:
            name = f.read().strip()
            if name:
                return name
    except OSError:
        pass
    target = os.path.realpath(localtime).replace("\\", "/")
    return target.split("/zoneinfo/", 1)[1] if "/zoneinfo/" in target else ""


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


def kernel_origin(policy):
    """`apt-cache policy linux-image-<release>` -> where the INSTALLED
    version came from: `security` (the distribution's security
    archive), `distribution` (its main archive -- a point release),
    `vendor` (another archive, e.g. the Raspberry Pi Foundation's),
    `unknown` (no policy, or only the dpkg status line).

    The installed version's block is the one marked `***`; its source
    lines follow until the next version line. Security wins when a
    version sits in both archives (after a point release it does): the
    conservative reading, 2.7.3."""
    sources, in_block = [], False
    for line in policy.splitlines():
        if re.match(r"^\s*\*\*\*\s+\S+\s+\d+", line):
            in_block = True
            continue
        if not in_block:
            continue
        m = re.match(r"^\s+\d+\s+(\S+)\s+(\S+)", line)
        if m and m.group(1).startswith(("http://", "https://", "file:", "cdrom:")):
            sources.append((m.group(1), m.group(2)))
        elif re.match(r"^\s+\S+\s+\d+\s*$", line):
            break
    if not sources:
        return "unknown"
    kinds = set()
    for url, suite in sources:
        text = (url + " " + suite).lower()
        if "security" in text:
            kinds.add("security")
        elif "raspberrypi" in text or "raspbian" in text:
            kinds.add("vendor")
        else:
            kinds.add("distribution")
    for k in ("security", "distribution", "vendor"):
        if k in kinds:
            return k
    return "unknown"


def kernel_facts(run=sh, marker_exists=None):
    running = run(["uname", "-r"]).strip()
    flavour = kernel_flavour(running)
    newest = newest_of_flavour(
        installed_kernels(run(["dpkg-query", "-W",
                               "-f=${Package} ${Status}\n", "linux-image-*"])),
        flavour)
    if marker_exists is None:
        marker_exists = os.path.exists(REBOOT_MARKER)
    required = reboot_required(running, newest)
    origin = kernel_origin(run(["apt-cache", "policy", f"linux-image-{newest}"])) \
        if required else ""
    return {"running": running, "installed": newest or running,
            "flavour": flavour, "reboot_required": required,
            "origin": origin, "marker": bool(marker_exists)}


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


def _hhmm(s):
    return bool(re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", s or ""))


def reboot_window(values):
    """`off`, or the local HH:MM the node reboots at.

    2.7.4: the platform's own key first. A 2.6.3-style file (the
    distribution's `Automatic-Reboot true` + time) still reads, so an
    updated node keeps its window until the first measurement rewrites
    the file."""
    own = values.get(WINDOW_KEY, "")
    if _hhmm(own):
        return own
    on = values.get("Unattended-Upgrade::Automatic-Reboot", "false").lower()
    at = values.get("Unattended-Upgrade::Automatic-Reboot-Time", "")
    if on in ("true", "1", "yes") and _hhmm(at):
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
    # 2.7.4: the distribution's own reboot must be off on this node.
    dist_reboot = values.get("Unattended-Upgrade::Automatic-Reboot", "false").lower() \
        in ("true", "1", "yes")
    return {"state": state, "detail": detail, "installed": installed,
            "switch": switch, "timer": timer, "last_run": last,
            "window": reboot_window(values), "distribution_reboot": dist_reboot}


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

def measure(run=sh, os_release="/etc/os-release", external="", **probes):
    """The facts of 2.6.1 plus, for 2.7.3, whether the node is exposed:
    `external` is the node's registered external hostname ('' when it
    has none) -- the caller knows the registry, this module does not."""
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
        "boot_time": probes.get("boot_time") or boot_time(),
        "os": pretty,
        "kernel": kernel_facts(run, probes.get("marker_exists")),
        "auto_updates": {k: v for k, v in auto.items() if k != "window"},
        "reboot_window": auto["window"],
        "gateway": gateway_facts(run),
        "external_name": external or "",
    }


def boot_time(stat="/proc/stat"):
    """When this machine last booted (UTC ISO), '' when unreadable."""
    try:
        with open(stat, encoding="utf-8") as f:
            for line in f:
                if line.startswith("btime "):
                    return iso(datetime.fromtimestamp(int(line.split()[1]), timezone.utc))
    except (OSError, ValueError):
        pass
    return ""


def care_path(apps_dir):
    return os.path.join(apps_dir, FILE_NAME)


def _write_json(path, data, mode=0o644):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)
    os.chmod(path, mode)
    return path


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write(facts, apps_dir):
    return _write_json(care_path(apps_dir), facts)


def read(apps_dir):
    return _read_json(care_path(apps_dir))


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


# --- the settings the platform writes (2.6.3, 2.7.4) -----------------------

def conf_text(window="off"):
    lines = [
        "// Written by OAAP (oaap.core.updates 2.6 and 2.7). Change it with:",
        "//   sudo oaap node care auto-updates on",
        "//   sudo oaap node care reboot-window HH:MM | off",
        "// The distribution's own files are not touched; this one sorts",
        "// after 50unattended-upgrades and therefore wins where both speak.",
        'APT::Periodic::Update-Package-Lists "1";',
        'APT::Periodic::Unattended-Upgrade "1";',
        "// The distribution's own reboot is OFF on purpose (2.7.4): the",
        "// platform plans and runs the reboot itself, because only it knows",
        "// a protection. Two mechanisms on one machine are two truths.",
        'Unattended-Upgrade::Automatic-Reboot "false";',
    ]
    if window != "off":
        lines += [
            "// The platform's window: the local hour the node reboots itself",
            "// after a kernel update -- announced 24 h ahead, held by a",
            "// protection unless the fix is critical (RFC-0069).",
            f'{WINDOW_KEY} "{window}";',
        ]
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


def migrate_conf(path=APT_CONF):
    """2.7.4: a 2.6.3-style file (the distribution's reboot on) is
    rewritten in the 2.7 form, window kept. True when rewritten; an
    absent file is left absent (the node never opted in)."""
    if not os.path.exists(path):
        return False
    return write_conf(read_conf(path), path)


def validate_window(at):
    at = (at or "").strip().lower()
    if at == "off":
        return "off"
    if not _hhmm(at):
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


# --- the protections (2.7.2) ------------------------------------------------

def protections_path(apps_dir):
    return os.path.join(apps_dir, PROTECTIONS_FILE_NAME)


def load_protections(apps_dir):
    """{instance: {until, reason, who, tenant, since}}"""
    return _read_json(protections_path(apps_dir)) or {}


def save_protections(prot, apps_dir):
    return _write_json(protections_path(apps_dir), prot)


def parse_day(s):
    try:
        return date.fromisoformat((s or "").strip())
    except ValueError:
        return None


def protect(prot, name, reason, who, tenant, until=None, today=None):
    """Add or extend a protection. Three conditions, none optional
    (2.7.2): a reason, an end within the maximum, and the caller files
    it in the tenant's log (not here -- this module knows no log)."""
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("say why, in words the tenant can read: --reason \"...\"")
    today = today or date.today()
    if until is None or until == "":
        end = today + timedelta(days=PROTECT_DEFAULT_DAYS)
    else:
        end = parse_day(until)
        if end is None:
            raise ValueError("--until wants a day as YYYY-MM-DD")
    if end < today:
        raise ValueError(f"--until {end} is in the past")
    if end > today + timedelta(days=PROTECT_MAX_DAYS):
        raise ValueError(f"a protection ends within {PROTECT_MAX_DAYS} days at most "
                         f"({today + timedelta(days=PROTECT_MAX_DAYS)}); extend it then, "
                         "with a reason again")
    rec = {"until": end.isoformat(), "reason": reason, "who": who or "root",
           "tenant": tenant or "", "since": now_iso()}
    prot[name] = rec
    return rec


def release(prot, name):
    return prot.pop(name, None) is not None


def live_protections(prot, known, today=None):
    """The protections that count: not expired, naming an instance the
    node still has. A stale name is ignored rather than trusted (the
    posture of the backup exclusion): a protection that outlived its
    instance and matched a NEW one would hold a fix nobody meant."""
    today = today or date.today()
    out = {}
    for name, rec in (prot or {}).items():
        if name not in known:
            continue
        end = parse_day((rec or {}).get("until"))
        if end is None or end < today:
            continue
        out[name] = rec
    return out


def latest_end(live):
    ends = [parse_day(r.get("until")) for r in (live or {}).values()]
    ends = [e for e in ends if e]
    return max(ends) if ends else None


# --- the plan (2.7.1, 2.7.3) ------------------------------------------------

def plan_path(apps_dir):
    return os.path.join(apps_dir, PLAN_FILE_NAME)


def load_plan(apps_dir):
    p = _read_json(plan_path(apps_dir))
    return p if p and p.get("at") else None


def save_plan(plan, apps_dir):
    return _write_json(plan_path(apps_dir), dict(plan, schema=PLAN_SCHEMA))


def clear_plan(apps_dir):
    try:
        os.remove(plan_path(apps_dir))
        return True
    except OSError:
        return False


def exposed(facts):
    """2.7.3: an external name, or listeners that negotiate MPTCP."""
    return bool(facts.get("external_name")) or facts.get("gateway", {}).get("mptcp") == "on"


def urgency_of(facts):
    """(`care` | `critical`, why) for the waiting kernel -- from facts,
    never from a hand."""
    k = facts.get("kernel", {})
    origin = k.get("origin", "unknown")
    if origin == "security" and exposed(facts):
        how = ("external name " + facts["external_name"]) if facts.get("external_name") \
            else "gateway listeners negotiate MPTCP"
        return "critical", f"security kernel {k.get('installed')} on an exposed node ({how})"
    why = {"security": "security kernel, node not exposed",
           "distribution": "kernel from the main archive (a point release)",
           "vendor": "vendor kernel"}.get(origin, "kernel of unknown origin")
    return "care", f"{why}: {k.get('installed')} waits"


def window_at(window, day, tz):
    h, m = (int(x) for x in window.split(":"))
    return datetime(day.year, day.month, day.day, h, m, tzinfo=tz)


def first_window(window, earliest, tz, not_before_day=None):
    """The first window hour at or after `earliest` (UTC), on a day not
    before `not_before_day` (a local calendar day)."""
    local = earliest.astimezone(tz)
    day = local.date()
    if not_before_day and not_before_day > day:
        day = not_before_day
    for _ in range(0, 400):
        cand = window_at(window, day, tz)
        if cand >= earliest:
            return cand
        day += timedelta(days=1)
    return window_at(window, day, tz)


def local_text(dt, tz):
    return dt.astimezone(tz).strftime("%Y-%m-%d %H:%M %Z")


def _held_rows(live):
    return [{"instance": n, "until": r.get("until", ""), "reason": r.get("reason", "")}
            for n, r in sorted((live or {}).items())]


def compute_plan(facts, live, stored, now=None, tz=None):
    """(plan or None, events): the plan of 2.7.1 from the facts, the live
    protections and the stored plan -- run every minute.

    Events are (action, subject, detail) in the vocabulary of
    oaap.core.tenant 1.7, for the caller to file; this module knows no
    log. `stored` keeps two things across minutes: the announcement
    time (so a moved plan never gains a shorter lead) and a hand-made
    plan (which no measurement produces)."""
    now = now or datetime.now(timezone.utc)
    tz = tz or timezone.utc
    stored = stored or None
    events = []
    k = facts.get("kernel", {})
    window = facts.get("reboot_window", "off")
    today = now.astimezone(tz).date()
    end = latest_end(live)
    day_after = end + timedelta(days=1) if end else None

    # The reboot was started: the stored plan closes with the proof once
    # the machine has booted since -- the boot time is the fact, not
    # the kernel (a hand-made plan changes no kernel).
    if stored and stored.get("state") == "rebooting":
        bt = parse_iso(facts.get("boot_time"))
        started = parse_iso(stored.get("rebooting_at"))
        if bt and started and bt > started:
            events.append(("reboot.done", stored.get("source", ""),
                           f"rebooted after {stored.get('at_local')}; running {k.get('running')}"
                           + ("" if not k.get("reboot_required") else
                              f" -- but {k.get('installed')} still waits")))
            stored = None
        else:
            plan = dict(stored)
            if started and now > started + timedelta(seconds=DUE_GRACE) \
                    and plan.get("overdue_said") is not True:
                events.append(("reboot.overdue", plan.get("source", ""),
                               f"reboot started {stored.get('rebooting_at')}, "
                               "the machine has not booted since"))
                plan["overdue_said"] = True
            return plan, events

    if stored and stored.get("source") == "manual":
        plan = dict(stored)
        wish = parse_iso(plan.get("wish") or plan.get("at")) or now
        at = wish
        if plan.get("urgency") == "care" and day_after and day_after > wish.astimezone(tz).date():
            at = window_at(wish.astimezone(tz).strftime("%H:%M"), day_after, tz)
        plan["held_by"] = _held_rows(live) if plan.get("urgency") == "care" else []
        plan["overridden"] = _held_rows(live) if plan.get("urgency") == "critical" else []
        if iso(at) != plan.get("at"):
            holders = ", ".join(r["instance"] for r in plan["held_by"])
            events.append(("reboot.moved", "manual",
                           f"to {local_text(at, tz)}: "
                           + (f"held by {holders}" if holders else "no longer held")))
        plan["at"] = iso(at)
        plan["at_local"] = local_text(at, tz)
        _finish(plan, now, events, live)
        return plan, events

    if not k.get("reboot_required") or window == "off":
        if stored and stored.get("source") == "kernel":
            events.append(("reboot.done" if not k.get("reboot_required") else "reboot.cancelled",
                           "kernel",
                           f"running {k.get('running')}" if not k.get("reboot_required")
                           else "the reboot window was removed"))
        return None, events

    urgency, why = urgency_of(facts)
    same = (stored and stored.get("source") == "kernel"
            and stored.get("kernel") == k.get("installed"))
    announced = parse_iso(stored.get("announced")) if same else None
    announced = announced or now
    earliest = announced + timedelta(seconds=LEAD)
    at = first_window(window, earliest, tz,
                      not_before_day=day_after if urgency == "care" else None)
    plan = {
        "source": "kernel", "kernel": k.get("installed", ""),
        "origin": k.get("origin", ""),
        "urgency": urgency, "reason": why,
        "announced": iso(announced), "at": iso(at), "at_local": local_text(at, tz),
        "held_by": _held_rows(live) if urgency == "care" and live else [],
        "overridden": _held_rows(live) if urgency == "critical" and live else [],
        "by": "",
    }
    if same:
        # What was already said stays said: the state carries over, so an
        # overdue plan is not announced overdue every minute -- and so do
        # the cases the announcement opened (2.7.6).
        plan["state"] = stored.get("state", "planned")
        if stored.get("cases"):
            plan["cases"] = stored["cases"]
    if not same:
        events.append(("reboot.planned", "kernel",
                       f"{plan['at_local']} ({urgency}): {why}"))
        for r in plan["overridden"]:
            events.append(("reboot.override", r["instance"],
                           f"protection until {r['until']} overridden: critical"))
    else:
        if stored.get("at") != plan["at"] or stored.get("urgency") != urgency:
            events.append(("reboot.moved", "kernel",
                           f"from {stored.get('at_local')} to {plan['at_local']} ({urgency})"
                           + (": held by " + ", ".join(r["instance"] for r in plan["held_by"])
                              if plan["held_by"] else "")))
        before = {r["instance"] for r in stored.get("overridden") or []}
        for r in plan["overridden"]:
            if r["instance"] not in before:
                events.append(("reboot.override", r["instance"],
                               f"protection until {r['until']} overridden: critical"))
    _finish(plan, now, events, live)
    return plan, events


def _finish(plan, now, events, live):
    at = parse_iso(plan["at"])
    announced = parse_iso(plan.get("announced")) or now
    plan["deferred"] = bool(plan.get("held_by")) and \
        (at - announced).total_seconds() > DEFER_AFTER_DAYS * 86400
    if plan.get("state") == "rebooting":
        return
    if now > at + timedelta(seconds=DUE_GRACE):
        if plan.get("state") != "overdue":
            events.append(("reboot.overdue", plan.get("source", ""),
                           f"planned {plan['at_local']}, no reboot happened"))
        plan["state"] = "overdue"
    else:
        plan["state"] = "planned"


def manual_plan(at_text, reason, who, critical=False, now=None, tz=None):
    """2.7.1: a hand-made plan. `at_text` is `now` or `YYYY-MM-DD HH:MM`
    on the node's clock. Sooner than 24 h is the server_admin's explicit
    act and is recorded as such (`shortened`)."""
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("say why, in words every tenant can read: --reason \"...\"")
    now = now or datetime.now(timezone.utc)
    tz = tz or timezone.utc
    text = (at_text or "").strip().lower()
    if text == "now":
        at = now
    else:
        try:
            at = datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=tz)
        except ValueError:
            raise ValueError("the time wants 'YYYY-MM-DD HH:MM' on this node's clock, or 'now'")
        if at < now:
            raise ValueError("that time is in the past")
    return {
        "source": "manual", "kernel": "", "urgency": "critical" if critical else "care",
        "reason": reason, "by": who or "root", "announced": iso(now),
        "wish": iso(at), "at": iso(at), "at_local": local_text(at, tz),
        "held_by": [], "overridden": [], "state": "planned",
        "shortened": (at - now).total_seconds() < LEAD,
    }


def due(plan, now=None):
    """2.7.1: T has come and the grace has not passed."""
    if not plan or plan.get("state") == "rebooting":
        return False
    now = now or datetime.now(timezone.utc)
    at = parse_iso(plan.get("at"))
    return bool(at) and at <= now <= at + timedelta(seconds=DUE_GRACE)


def may_reboot(facts, plan, live):
    """The act on fresh facts (2.7.1): a hand-made plan, or a reboot
    still required that no live protection holds (`care`) or that
    overrides them (`critical`)."""
    if not plan:
        return False, "no plan"
    if plan.get("source") == "manual":
        if plan.get("urgency") == "care" and live:
            return False, "held by " + ", ".join(sorted(live))
        return True, "hand-made plan"
    if not facts.get("kernel", {}).get("reboot_required"):
        return False, "no reboot required any more"
    if plan.get("urgency") == "care" and live:
        return False, "held by " + ", ".join(sorted(live))
    return True, plan.get("reason", "")


# --- the cases (2.7.6, RFC-0069 stage 2) -----------------------------------
#
# One case per plan in every inbox (`vorgaenge`) on the node, opened with
# the announcement, written on at every move, closed with the proof. The
# texts are the tenants' (German, like the band on their place) and carry
# NO kernel version: an inbox says when and why in words, `oaap status`
# and the fleet document say which kernel. The reason in the operator's
# own words only where the operator wrote one (a plan by hand).

ORIGIN_DE = {"security": "aus dem Sicherheitsarchiv der Distribution",
             "distribution": "ein Punktrelease aus dem Hauptarchiv",
             "vendor": "ein Hersteller-Kernel"}


def plan_ref(plan):
    """What makes two announcements the same plan: source, kernel and the
    announcement time -- stable across minutes, new with every plan."""
    return f"{plan.get('source', '')}:{plan.get('kernel') or 'hand'}:{plan.get('announced', '')}"


def _held_de(rows):
    return ", ".join(f"{r['instance']} bis {r.get('until', '')}"
                     + (f" ({r['reason']})" if r.get("reason") else "")
                     for r in rows or [])


def case_title(plan, host):
    u = "kritisch" if plan.get("urgency") == "critical" else "Pflege"
    return f"Neustart des Knotens {host}: {plan.get('at_local')} ({u})"


def case_body(plan, host, tz=None):
    tz = tz or timezone.utc
    lines = [f"Der Knoten {host} startet am {plan.get('at_local')} neu."]
    if plan.get("urgency") == "critical":
        lines.append("Dringlichkeit: kritisch — ein Kernel aus dem Sicherheitsarchiv auf einem "
                     "exponierten Knoten. Ein Schutz hält diesen Neustart nicht; er wird gezeigt "
                     "und überstimmt.")
    else:
        lines.append("Dringlichkeit: Pflege — ein Schutz je Instanz (mit Begründung und Ende) "
                     "hält den Neustart bis zum Tag nach seinem Ende.")
    if plan.get("source") == "manual":
        lines.append(f"Grund ({plan.get('by') or 'root'}, von Hand geplant): {plan.get('reason', '')}")
    else:
        lines.append("Grund: ein Kernel-Update wartet ("
                     + ORIGIN_DE.get(plan.get("origin", ""), "Herkunft unbekannt") + ").")
    if plan.get("held_by"):
        lines.append("Gehalten durch Schutz: " + _held_de(plan["held_by"]) + ".")
    if plan.get("overridden"):
        lines.append("Schutz überstimmt (kritisch): " + _held_de(plan["overridden"]) + ".")
    ann = parse_iso(plan.get("announced"))
    if plan.get("shortened"):
        lines.append("Vom Betreiber ausdrücklich kürzer als 24 Stunden angesetzt.")
    elif ann:
        lines.append(f"Angekündigt am {local_text(ann, tz)}; der Termin liegt mindestens "
                     "24 Stunden danach.")
    lines.append("Der Knoten schreibt diesen Vorgang bei jeder Änderung fort und schließt ihn "
                 "nach dem Neustart. Sitzungen in Apps auf diesem Knoten enden mit dem Neustart; "
                 "einen Schutz für eine Instanz setzt der Betreiber.")
    return "\n".join(lines)


def case_ops(events, plan, stored, host, tz=None):
    """(op dicts) for the inboxes, from the events compute_plan produced:
    `open` (ref, title, body) once per plan, `note` (text) per move,
    override, overdue and start, `close` (text) with the proof or the
    cancellation. `plan` is the plan now, `stored` the one before -- a
    closing event has only the latter."""
    tz = tz or timezone.utc
    cur = plan or stored or {}
    ops = []
    for action, subject, detail in events:
        if action == "reboot.planned" and plan:
            ops.append({"op": "open", "ref": plan_ref(plan), "title": case_title(plan, host),
                        "body": case_body(plan, host, tz)})
        elif action == "reboot.moved" and plan:
            held = _held_de(plan.get("held_by"))
            ops.append({"op": "note", "text": f"Verschoben auf {plan.get('at_local')}: "
                        + (f"gehalten durch {held}" if held else "nicht mehr gehalten") + "."})
        elif action == "reboot.override":
            r = next((r for r in cur.get("overridden") or [] if r.get("instance") == subject), {})
            ops.append({"op": "note", "text": f"Schutz von {subject}"
                        + (f" bis {r['until']}" if r.get("until") else "")
                        + " überstimmt: der Neustart ist kritisch und wartet nicht."})
        elif action == "reboot.overdue":
            ops.append({"op": "note", "text": f"Überfällig: geplant {cur.get('at_local')}, "
                        + ("der Knoten ist seit dem Start nicht wieder hochgekommen."
                           if "started" in detail else "kein Neustart erfolgt. Läuft der Minutenlauf?")})
        elif action == "reboot.started":
            ops.append({"op": "note", "text": "Der Knoten startet jetzt neu."})
        elif action == "reboot.done":
            ops.append({"op": "close", "text": f"Neustart erfolgt nach {cur.get('at_local')}; "
                        f"der Knoten {host} läuft wieder."
                        + (" Ein weiterer Kernel wartet; ein neuer Plan folgt."
                           if "still waits" in detail else "")})
        elif action == "reboot.cancelled":
            if detail == "the reboot window was removed":
                why = "das Neustart-Fenster wurde entfernt"
            else:
                tail = detail.split("cancelled", 1)[1].lstrip(": ").strip() if "cancelled" in detail else ""
                why = "vom Betreiber zurückgenommen" + (f": {tail}" if tail else "")
            ops.append({"op": "close", "text": f"Abgesagt — {why}."})
    return ops


# --- the three readers -----------------------------------------------------

def plan_text(plan, de=False):
    """One sentence for the plan, both languages."""
    if not plan:
        return ""
    u = plan.get("urgency", "care")
    held = ", ".join(f"{r['instance']} bis {r['until']}" if de else
                     f"{r['instance']} until {r['until']}" for r in plan.get("held_by") or [])
    over = ", ".join(r["instance"] for r in plan.get("overridden") or [])
    if de:
        t = (f"Geplanter Neustart: {plan.get('at_local')} "
             f"({'KRITISCH' if u == 'critical' else 'Pflege'}) — {plan.get('reason', '')}")
        if plan.get("source") == "manual":
            t += f" (von Hand geplant durch {plan.get('by') or 'root'})"
        if held:
            t += f"; gehalten durch {held}"
        if over:
            t += f"; Schutz überstimmt: {over}"
        if plan.get("state") == "overdue":
            t = "ÜBERFÄLLIG — " + t + " — Termin verstrichen ohne Neustart; läuft der Minutenlauf?"
        elif plan.get("state") == "rebooting":
            t = "Startet jetzt neu — " + t
    else:
        t = (f"Reboot planned: {plan.get('at_local')} "
             f"({'CRITICAL' if u == 'critical' else 'care'}) — {plan.get('reason', '')}")
        if plan.get("source") == "manual":
            t += f" (planned by hand by {plan.get('by') or 'root'})"
        if held:
            t += f"; held by {held}"
        if over:
            t += f"; protection overridden: {over}"
        if plan.get("state") == "overdue":
            t = "OVERDUE — " + t + " — the time passed without a reboot; is the minute job running?"
        elif plan.get("state") == "rebooting":
            t = "Rebooting now — " + t
    return t


def protections_text(rows, de=False):
    if not rows:
        return ""
    parts = [f"{r['instance']} bis {r['until']} ({r['reason']})" if de else
             f"{r['instance']} until {r['until']} ({r['reason']})" for r in rows]
    return ("Neustart-Schutz: " if de else "Reboot protection: ") + "; ".join(parts)


def status_lines(facts, now=None, de=False):
    """[(state, text)] -- the kernel line, the plan line (2.7), the
    auto-update line, the gateway line, each with its recommendation
    when it is not fine. One source for `oaap status` (English) and the
    portal (German)."""
    if not facts:
        return [("unknown",
                 "Pflegestand noch nicht gemessen — misst der Minutenlauf, "
                 "oder: sudo oaap node care" if de else
                 "Care state not measured yet — the minute job measures it, "
                 "or: sudo oaap node care")]
    out = []
    k = facts.get("kernel", {})
    win = facts.get("reboot_window", "off")
    plan = facts.get("plan")
    if k.get("reboot_required"):
        if de:
            t = (f"Neustart nötig — läuft {k.get('running')}, installiert "
                 f"{k.get('installed')}. "
                 + ("Der Knoten plant den Neustart selbst (siehe Plan)."
                    if plan else
                    "In einer ruhigen Stunde: sudo systemctl reboot"))
        else:
            t = (f"Kernel: REBOOT REQUIRED — running {k.get('running')}, "
                 f"installed {k.get('installed')}. "
                 + ("The node plans the reboot itself (see the plan)."
                    if plan else
                    "In a quiet hour: sudo systemctl reboot"))
        out.append(("warn", t))
    else:
        out.append(("ok", (f"Kernel {k.get('running')} läuft, ist der neueste "
                           "installierte — kein Neustart nötig") if de else
                    (f"Kernel: {k.get('running')} running, newest installed "
                     "— no reboot needed")))
    if plan:
        st = {"overdue": "err", "rebooting": "warn"}.get(plan.get("state"), "warn")
        out.append((st, plan_text(plan, de)))
    elif facts.get("protections"):
        out.append(("ok", protections_text(facts["protections"], de)))
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
    if a.get("distribution_reboot"):
        out.append(("warn", (
            "Die Distribution würde selbst neu starten (Automatic-Reboot an) — zwei "
            "Mechanismen. Umschreiben: sudo oaap node care measure") if de else (
            "The distribution's own reboot is on (Automatic-Reboot) — two mechanisms. "
            "Rewrite: sudo oaap node care measure")))
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
    """The `host` block of oaap.fleet.status 0.6 -- a whitelist, never a
    copy: no package list, no file contents, no sources, no reasons."""
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
    plan = facts.get("plan")
    if plan:
        block["reboot_plan"] = {
            "at": plan.get("at", ""), "at_local": plan.get("at_local", ""),
            "urgency": plan.get("urgency", "care"), "source": plan.get("source", ""),
            "held": bool(plan.get("held_by")), "overridden": bool(plan.get("overridden")),
            "state": plan.get("state", "planned"),
        }
    rows = facts.get("protections") or []
    if rows:
        block["protections"] = {"count": len(rows),
                                "until": max(r.get("until", "") for r in rows)}
    return block


def fleet_attention(facts):
    """The attention kinds of oaap.fleet.status 0.5 and 0.6."""
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
    plan = facts.get("plan")
    if plan:
        items.append({"kind": "reboot_planned",
                      "detail": f"{plan.get('at_local')} ({plan.get('urgency')})"})
        if plan.get("deferred"):
            items.append({"kind": "reboot_deferred",
                          "detail": "held since " + plan.get("announced", "")[:10] + " by "
                          + ", ".join(r["instance"] for r in plan.get("held_by") or [])})
        if plan.get("state") == "overdue":
            items.append({"kind": "reboot_overdue",
                          "detail": f"planned {plan.get('at_local')}, no reboot happened"})
    return items
