"""Advisories (oaap.core.updates 2.8, RFC-0070 stage 1): the node reads
what is known about what it runs.

Why this exists -- 2026-11-05, the evening after CVE-2026-98432: the
stage-1 line of host care said "Auto-updates: on" on oaap-test while
13 Debian security advisories were open there, the oldest since
14 August (util-linux), openssl since 30 September. Program, switch and
timer were in place -- since that morning; the "last run" was the stamp
of a dry run. "On" is a setting; "open" is a fact. This module computes
the fact.

The rules, each in a function so a test can execute it without a node:

- the source is Debian's own DSA list (one text file from the security
  team's repository); it is DATA, parsed with a fixed grammar; a line the
  grammar does not know is skipped and COUNTED, never taken as "no
  advisory" (`parse`);
- versions are compared with the package manager's own comparator
  (`dpkg --compare-versions`), never as strings (`older`);
- the kernel is named differently in the two lists: dpkg says
  `linux-signed-amd64 6.12.115+2`, the DSA says `linux 6.12.115-2`
  (`source_name`); for the kernel the NEWEST installed version counts --
  older images are inert files, and whether the newest RUNS is the
  kernel pair's verdict (2.6.1), not this one's;
- a package installed from an origin the list does not cover (Docker CE,
  the Raspberry Pi archive) is `unknown`, never clean (`origins`);
- a failed read keeps the previous list and becomes `stale` after two
  days; it never yields "0 open" (`refresh`).

Everything that touches the host goes through `run` / `fetch`, so the
tests replace them.
"""
import json
import os
import re
import subprocess
import urllib.request
from datetime import datetime, timedelta, timezone

LIST_URL = ("https://salsa.debian.org/security-tracker-team/security-tracker"
            "/-/raw/master/data/DSA/list")
SOURCE = "debian-dsa"
LIST_FILE = "advisories.txt"
STATE_FILE = "advisories.json"
DPKG_STATUS = "/var/lib/dpkg/status"
UPGRADE_STAMP = "/var/lib/apt/periodic/upgrade-stamp"
READ_EVERY = 24 * 3600      # the list is read once a day ...
RETRY_AFTER = 3600          # ... a failed read is retried hourly, not every minute
STALE_AFTER = 2 * 86400     # 4.2: a list older than this is `stale`
MAX_BYTES = 16 * 1024 * 1024
KERNEL_SOURCES = ("linux",)

# The header has varied over 24 years: "DSA-6612-1 symfony - security
# update", but also "DSA-123 linux-kernel-2.4.18 - ...", "DSA-1-1 clamav"
# with no title, and several packages before the dash. The first word is
# the package; the release lines below name the source that counts.
_HEAD = re.compile(r"^\[(\d\d) (\w\w\w) (\d{4})\] (DSA-\d+(?:-\d+)?)\s+(\S+)(?:\s.*)?$")
_CVES = re.compile(r"^\s+\{([^}]*)\}\s*$")
_FIX = re.compile(r"^\s+\[([a-z]+)\] - (\S+) (\S+)(\s.*)?$")
_NOTE = re.compile(r"^\s+NOTE:")
_MONTHS = {m: i for i, m in enumerate(
    "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(), 1)}


def sh(cmd, timeout=60, input=None):
    """stdout, '' on failure -- in the C locale: the nodes speak German
    (`Installationskandidat:`), the parsers read apt's English."""
    env = dict(os.environ, LC_ALL="C", LANG="C")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, input=input, env=env)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return r.stdout if r.returncode == 0 else ""


def now_iso(now=None):
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


# --- the list --------------------------------------------------------------

def parse(text):
    """The DSA list -> ([entry], skipped).

    entry = {id, date (YYYY-MM-DD), package, cves, fixed: {suite: (source, version)}}.
    A release line whose version is not a version (`<not-affected>`,
    `<unfixed>`, `<end-of-life>`) is no fix and is known; a NOTE line is
    known; every other line the grammar does not know is skipped and
    counted (4.1)."""
    entries, cur, skipped = [], None, 0
    for line in text.splitlines():
        if not line.strip():
            continue
        m = _HEAD.match(line)
        if m:
            d, mon, y = m.group(1), _MONTHS.get(m.group(2)), m.group(3)
            if not mon:
                skipped += 1
                cur = None
                continue
            cur = {"id": m.group(4), "date": f"{y}-{mon:02d}-{d}",
                   "package": m.group(5), "cves": [], "fixed": {}}
            entries.append(cur)
            continue
        if cur is None:
            skipped += 1
            continue
        m = _CVES.match(line)
        if m:
            cur["cves"] = m.group(1).split()
            continue
        m = _FIX.match(line)
        if m:
            ver = m.group(3)
            if ver[:1].isdigit():
                cur["fixed"][m.group(1)] = (m.group(2), ver)
            continue
        if _NOTE.match(line):
            continue
        skipped += 1
    return entries, skipped


# --- what is installed -------------------------------------------------------

def source_name(src, version):
    """dpkg's source name and version -> the DSA's. The signed kernel is
    built from `linux`: `linux-signed-amd64 6.12.115+2` is `linux
    6.12.115-2` (measured 2026-11-05: a naive match reports the kernel
    as never advised)."""
    m = re.fullmatch(r"linux-signed-[a-z0-9]+", src or "")
    if m:
        v = re.sub(r"\+(\d+)$", r"-\1", version or "")
        return "linux", v
    return src, version


def installed(dpkg_lines):
    """`dpkg-query -W -f '${source:Package}\\t${source:Version}\\t${Package}\\t${Status}\\n'`
    -> {source: {"versions": set, "binaries": [names]}} for packages that
    are installed (a removed package's leftover config is not installed)."""
    out = {}
    for line in dpkg_lines.splitlines():
        parts = line.split("\t")
        if len(parts) != 4 or not parts[3].endswith(" installed"):
            continue
        src, ver = source_name(parts[0], parts[1])
        row = out.setdefault(src, {"versions": set(), "binaries": []})
        row["versions"].add(ver)
        row["binaries"].append(parts[2])
    return out


def release_origins(policy_global):
    """`apt-cache policy` (no package) -> {"<url> <dist>": origin} -- the
    archive line followed by its `release ... o=<Origin>` line."""
    out, key = {}, None
    for line in policy_global.splitlines():
        m = re.match(r"^\s*-?\d+\s+(\S+)\s+(\S+)\s+\S+\s+Packages\s*$", line)
        if m:
            key = f"{m.group(1)} {m.group(2)}"
            continue
        m = re.match(r"^\s+release\s+(.*)$", line)
        if m and key:
            fields = dict(kv.split("=", 1) for kv in m.group(1).split(",") if "=" in kv)
            out[key] = fields.get("o", "")
            key = None
    return out


def package_origins(policy_pkgs, release_map):
    """`apt-cache policy <pkg>...` -> {package: set(origins)} of the
    archives that carry its INSTALLED or its CANDIDATE version. Only the
    dpkg status line (`/var/lib/dpkg/status`) is no origin: a package
    no archive carries any more."""
    out, pkg, want, inblock = {}, None, set(), False
    for line in policy_pkgs.splitlines():
        m = re.match(r"^(\S+):\s*$", line)
        if m:
            pkg, want, inblock = m.group(1), set(), False
            out.setdefault(pkg, set())
            continue
        if pkg is None:
            continue
        m = re.match(r"^\s+(Installed|Candidate|Installiert|Installationskandidat):\s+(\S+)", line)
        if m:
            want.add(m.group(2))
            continue
        m = re.match(r"^\s+(?:\*\*\*\s+)?(\S+)\s+-?\d+\s*$", line)
        if m:
            inblock = m.group(1) in want
            continue
        m = re.match(r"^\s+-?\d+\s+(\S+)\s+(\S+)\s+\S+\s+Packages\s*$", line)
        if m and inblock:
            o = release_map.get(f"{m.group(1)} {m.group(2)}")
            if o is not None:
                out[pkg].add(o or "?")
    return out


def classify(inst, pkg_origins, distribution="Debian"):
    """{source: "debian" | "vendor" | "local"} -- `vendor` when a binary
    of the source comes from another archive and none from the
    distribution's; `local` when no archive carries it at all."""
    out = {}
    for src, row in inst.items():
        seen = set()
        for b in row["binaries"]:
            seen |= pkg_origins.get(b.split(":")[0], set()) | pkg_origins.get(b, set())
        if distribution in seen:
            out[src] = "debian"
        elif seen:
            out[src] = "vendor"
        else:
            out[src] = "local"
    return out


# --- the comparison ----------------------------------------------------------

def dpkg_older(a, b):
    return subprocess.run(["dpkg", "--compare-versions", a, "lt", b],
                          capture_output=True).returncode == 0


def compare(entries, inst, suite, kinds, older=dpkg_older):
    """The advisory state's core: per installed source package covered by
    the distribution, the newest advisory for this suite, and whether the
    installed version is older than its fix.

    Returns (items, covered): items of the OPEN ones, sorted oldest first;
    covered = how many installed packages have an advisory at all."""
    newest = {}
    for e in entries:
        fx = e["fixed"].get(suite)
        if not fx:
            continue
        src, ver = fx
        if src not in inst or kinds.get(src) != "debian":
            continue
        prev = newest.get(src)
        if prev is None or older(prev["fixed"][suite][1], ver):
            newest[src] = e
    items = []
    for src, e in newest.items():
        fixed = e["fixed"][suite][1]
        versions = sorted(inst[src]["versions"])
        if src in KERNEL_SOURCES:
            have = versions[0]
            for v in versions[1:]:
                if older(have, v):
                    have = v
        else:
            have = versions[0]
            for v in versions[1:]:
                if older(v, have):
                    have = v
        if older(have, fixed):
            items.append({"package": src, "dsa": e["id"], "published": e["date"],
                          "fixed": fixed, "installed": have, "cves": len(e["cves"])})
    items.sort(key=lambda r: (r["published"], r["package"]))
    return items, len(newest)


def suite_of(os_release="/etc/os-release"):
    try:
        with open(os_release, encoding="utf-8") as f:
            for line in f:
                if line.startswith("VERSION_CODENAME="):
                    return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return ""


def security_candidates(upgradable):
    """`apt list --upgradable` -> how many packages apt would install from
    the security archive now -- the second reading, from apt's own
    knowledge, needing no network."""
    return sum(1 for line in upgradable.splitlines()
               if re.search(r"/\S*-security", line.split(" ", 1)[0]))


def evaluate(text, suite, run=sh, older=dpkg_older):
    """The list text -> the advisory facts (without read_at/stale)."""
    entries, skipped = parse(text)
    inst = installed(run(["dpkg-query", "-W", "-f",
                          "${source:Package}\t${source:Version}\t${Package}\t${Status}\n"]))
    rel = release_origins(run(["apt-cache", "policy"]))
    names = sorted({b for row in inst.values() for b in row["binaries"]})
    pol = run(["apt-cache", "policy"] + names, timeout=120) if names else ""
    kinds = classify(inst, package_origins(pol, rel))
    items, covered = compare(entries, inst, suite, kinds, older)
    unknown = sorted(s for s, k in kinds.items() if k == "vendor")
    local = sorted(s for s, k in kinds.items() if k == "local")
    return {
        "suite": suite,
        "entries": len(entries),
        "skipped": skipped,
        "installed": len(inst),
        "covered": covered,
        "open": len(items),
        "oldest": items[0]["published"] if items else "",
        "oldest_package": items[0]["package"] if items else "",
        "items": items,
        "unknown": unknown,
        "local": local,
        "security_candidates": security_candidates(
            run(["apt", "list", "--upgradable"], timeout=120)),
    }


# --- the cache and the refresh -------------------------------------------------

def _mtime(path):
    try:
        return int(os.path.getmtime(path))
    except OSError:
        return 0


def load(apps_dir):
    try:
        with open(os.path.join(apps_dir, STATE_FILE), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(apps_dir, state):
    path = os.path.join(apps_dir, STATE_FILE)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)
    os.chmod(path, 0o644)


def http_fetch(url=LIST_URL, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "oaap-node-care/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 -- fixed https URL
        data = r.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("list larger than expected")
    return data.decode("utf-8", "replace")


def need_read(state, now, upgrade_stamp=0, force=False):
    """Read the list when forced, when it is older than a day, or when an
    automatic-update run came after the last read -- but a failed read is
    retried hourly, not with every minute job."""
    last_try = parse_iso(state.get("last_attempt", ""))
    if not force and last_try and (now - last_try).total_seconds() < RETRY_AFTER \
            and state.get("last_error"):
        return False
    read_at = parse_iso(state.get("read_at", ""))
    if force or read_at is None:
        return True
    if (now - read_at).total_seconds() >= READ_EVERY:
        return True
    return bool(upgrade_stamp and upgrade_stamp > read_at.timestamp())


def is_stale(state, now):
    read_at = parse_iso(state.get("read_at", ""))
    return read_at is None or (now - read_at).total_seconds() > STALE_AFTER


def refresh(apps_dir, now=None, force=False, run=sh, fetch=http_fetch,
            older=dpkg_older, dpkg_status=DPKG_STATUS, upgrade_stamp=UPGRADE_STAMP,
            os_release="/etc/os-release"):
    """Read the list when due, compare when anything changed, keep the
    result beside the registry. Never raises; returns the state.

    4.2: a read that fails leaves the previous list and the previous
    comparison; without any list there is no count at all, never 0."""
    now = now or datetime.now(timezone.utc)
    os.makedirs(apps_dir, exist_ok=True)
    state = load(apps_dir)
    list_path = os.path.join(apps_dir, LIST_FILE)
    if need_read(state, now, _mtime(upgrade_stamp), force):
        state["last_attempt"] = now_iso(now)
        try:
            text = fetch()
            if not _HEAD.search(text.split("\n", 1)[0] or ""):
                raise ValueError("the first line is not an advisory")
            tmp = list_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, list_path)
            os.chmod(list_path, 0o644)
            state["read_at"] = now_iso(now)
            state.pop("last_error", None)
        except Exception as e:  # noqa: BLE001 -- any failure is a stale list
            state["last_error"] = f"{type(e).__name__}: {e}"[:200]
    inputs = {"list": state.get("read_at", ""), "dpkg": _mtime(dpkg_status)}
    if os.path.exists(list_path) and (force or state.get("inputs") != inputs
                                      or "facts" not in state):
        try:
            with open(list_path, encoding="utf-8") as f:
                text = f.read()
            state["facts"] = evaluate(text, suite_of(os_release), run, older)
            state["computed"] = now_iso(now)
            state["inputs"] = inputs
        except Exception as e:  # noqa: BLE001
            state["compute_error"] = f"{type(e).__name__}: {e}"[:200]
    try:
        _save(apps_dir, state)
    except OSError:
        pass
    return state


def care_block(state, now=None):
    """The advisory state as host care carries it (2.8.1): one block,
    `None` when the node never had a list -- an absent reading, not 0."""
    now = now or datetime.now(timezone.utc)
    f = state.get("facts") if state else None
    if not f:
        if state and state.get("last_error"):
            return {"source": SOURCE, "read_at": "", "stale": True, "open": None,
                    "error": state["last_error"]}
        return None
    return {
        "source": SOURCE,
        "read_at": state.get("read_at", ""),
        "stale": is_stale(state, now),
        "error": state.get("last_error", ""),
        "open": f["open"],
        "oldest": f["oldest"],
        "oldest_package": f["oldest_package"],
        "items": f["items"],
        "unknown": f["unknown"],
        "local": f.get("local", []),
        "security_candidates": f["security_candidates"],
        "skipped": f["skipped"],
        "covered": f["covered"],
        "suite": f["suite"],
    }


if __name__ == "__main__":  # a probe: python3 advisories.py <dir>
    import sys
    st = refresh(sys.argv[1] if len(sys.argv) > 1 else "/tmp/oaap-adv", force=True)
    blk = care_block(st)
    print(json.dumps({k: v for k, v in (blk or {}).items() if k not in ("items",)},
                     indent=1)[:3000])
    for it in (blk or {}).get("items", []):
        print(" ", it)
