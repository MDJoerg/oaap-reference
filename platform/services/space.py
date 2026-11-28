"""Freeing disk space (RFC-0128): which images a node may remove.

Why this exists -- 2026-11-24, measured on oaap-demo: +101 GB in one week,
mostly ten versions of `webtop` at 13 GB each. Every new version of an app
builds a new image and the old one stays; the platform removed one image
only, the `-prev` of a successful rebuild. By hand, old app images and the
build cache freed 53 GB.

The classes (§2), each a rule a test executes without Docker:

- **A** the build cache and dangling layers -- always (`docker builder
  prune`, `docker image prune`; counted from `docker system df`, removed by
  Docker itself, never listed one by one here);
- **B** an older version of an image the node built
  (`oaap-inst/<instance>[-<svc>]:<v>`, before RFC-0130 `oaap-app/<id>...`)
  in a repository a registered instance still uses -- the newest other tag
  of that repository stays as the fast way back (D2);
- **C** an image the node built that no registered instance uses (its
  repository is nobody's, or the `-prev` of an instance that is gone) --
  after a grace of seven days counted from when a run first saw it so (D6),
  not from when it was built: an app removed yesterday was built months ago;
- **D** an image the platform did not build -- never automatically, listed
  for a person (D3);
- **E** data -- volumes, storage, backups, packages: never touched here,
  nothing in this module even names them.

What is never a candidate, whatever its class: an image a container uses
(running or stopped -- a sleeping instance is a stopped container), an image
a registered instance needs to start (its service images in the registry and
its `-prev`), an image younger than a day (a deploy may be writing it while
the registry still names the old one), the platform's own images.

Everything that touches Docker goes through `run`, so the tests replace it.
"""
import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone

SCHEMA = "oaap.space/0.1"
STATE_FILE = "space-state.json"
VIEW_FILE = "space.json"
GRACE_DAYS = 7               # D6
YOUNG_SECONDS = 24 * 3600    # never an image younger than this
BUILT_PREFIXES = ("oaap-inst/", "oaap-app/")
PREV_PREFIX = "oaap-prev/"
AUTO_CLASSES = ("A", "B", "C")
DUE_HOURS = 23               # the nightly run: at most once in this
NIGHT_HOUR = 3               # without a nightly backup: from this local hour
AFTER_BACKUP_HOURS = 6       # with one: within this after it finished
PRESSURE_EVERY = 6 * 3600    # a pressure run at most this often

CLASS_TITLES = {
    "A": "Zwischenspeicher (Build-Cache, verwaiste Schichten)",
    "B": "Ältere Versionen einer App",
    "C": "Images ohne Instanz",
    "D": "Fremde Images (nur von Hand)",
}


def sh(cmd, timeout=600):
    env = dict(os.environ, LC_ALL="C", LANG="C")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 else None


def now_iso(t=None):
    return datetime.fromtimestamp(t if t is not None else time.time(),
                                  timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    """Docker's `Created` (RFC 3339, nanoseconds, any zone) or ours -> epoch."""
    s = str(s or "").strip()
    if not s:
        return None
    m = re.match(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.\d+)?(Z|[+-]\d\d:\d\d)?$", s)
    if not m:
        return None
    zone = m.group(2) or "Z"
    zone = "+00:00" if zone == "Z" else zone
    try:
        return datetime.fromisoformat(m.group(1) + zone).timestamp()
    except ValueError:
        return None


_UNITS = {"B": 1, "KB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12,
          "KIB": 1024, "MIB": 1024 ** 2, "GIB": 1024 ** 3, "TIB": 1024 ** 4}


def parse_size(text):
    """`13.2GB`, `512kB`, `0B` (docker's human sizes) -> bytes; None if not."""
    m = re.match(r"^\s*([\d.]+)\s*([kKMGT]?i?B)\s*$", str(text or ""))
    if not m:
        return None
    try:
        return int(float(m.group(1)) * _UNITS[m.group(2).upper()])
    except (KeyError, ValueError):
        return None


def fmt_bytes(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return (f"{n:.0f} {unit}" if unit in ("B", "KB") or n >= 100
                    else f"{n:.1f} {unit}").replace(".", ",")
        n /= 1000
    return ""


def repo_of(ref):
    """`a/b:1.2` -> `a/b`; a registry port is not a tag (`h:5000/a:1`)."""
    ref = str(ref or "")
    head, _, tail = ref.rpartition(":")
    if head and "/" not in tail:
        return head
    return ref


def is_built(ref):
    return str(ref or "").startswith(BUILT_PREFIXES)


def is_platform(ref, platform_refs=()):
    """The platform's own images: what compose builds (`oaap-<svc>`, no
    slash) and what its compose file names (`caddy:2` ...)."""
    r = str(ref or "")
    if r in platform_refs or repo_of(r) in {repo_of(p) for p in platform_refs}:
        return True
    return bool(re.match(r"^oaap-[a-z0-9][a-z0-9_.-]*(:[^/]*)?$", r))


# --- what the registry needs -----------------------------------------------

def prev_tag(name, service):
    return f"{PREV_PREFIX}{name}{('-' + service) if service else ''}:prev"


def bases(instances):
    """{ref: number of services} the node built an instance's image FROM
    (recorded at build, RFC-0071) -- a foreign image, but a rebuild pulls
    it again when it is gone."""
    out = {}
    for inst in (instances or {}).values():
        for s in inst.get("services") or []:
            ref = str((s.get("base") or {}).get("ref") or "")
            if ref:
                out[ref] = out.get(ref, 0) + 1
    return out


def needs(instances):
    """From the registry: (refs an instance needs to start, repositories in
    use). A rehearsal and a sleeping instance are registered like any."""
    refs, repos = set(), set()
    for name, inst in (instances or {}).items():
        svcs = inst.get("services") or []
        if not svcs and inst.get("image"):
            svcs = [{"service": "", "image": inst["image"]}]
        for s in svcs:
            img = str(s.get("image") or "")
            if img:
                refs.add(img)
                repos.add(repo_of(img))
            refs.add(prev_tag(name, s.get("service", "")))
    return refs, repos


# --- the plan ----------------------------------------------------------------

def plan(images, used_ids, instances, platform_refs=(), seen=None, now=None,
         grace_days=GRACE_DAYS):
    """Per image row one verdict. `images`: [{id, ref, created, size}] (one
    row per tag; untagged rows have ref '' and are class A's, skipped here);
    `used_ids`: image ids any container uses; `seen`: {ref: epoch} when a run
    first saw the ref without an instance. Returns (rows, seen'), rows
    [{ref, id, size, created, cls, why}] with cls in B/C/D or 'keep'."""
    now = now if now is not None else time.time()
    seen = dict(seen or {})
    need_refs, need_repos = needs(instances)
    base_of = bases(instances)
    need_ids = {i["id"] for i in images if i.get("ref") in need_refs}
    rows, orphan_now = [], set()
    # The previous version per repository in use (D2): the newest tag built
    # BEFORE the one in service -- not a newer one (a deploy writing right
    # now stays anyway as younger than a day; an older failed one is not
    # the way back). Decided before anything is classed.
    newest_other, served = {}, {}
    for i in images:
        if i.get("ref") in need_refs:
            rp = repo_of(i["ref"])
            served[rp] = max(served.get(rp, 0), i.get("created") or 0)
    for i in sorted(images, key=lambda x: x.get("created") or 0, reverse=True):
        ref = i.get("ref") or ""
        rp = repo_of(ref)
        if (ref and is_built(ref) and rp in need_repos and ref not in need_refs
                and (i.get("created") or 0) < served.get(rp, 0) and rp not in newest_other):
            newest_other[rp] = ref
    for i in images:
        ref = i.get("ref") or ""
        if not ref:
            continue
        row = {"ref": ref, "id": i.get("id", ""), "size": i.get("size") or 0,
               "created": i.get("created") or 0}
        rp = repo_of(ref)
        age = now - (i.get("created") or now)
        if ref in need_refs:
            row.update(cls="keep", why="eine Instanz braucht es")
        elif i.get("id") in used_ids:
            row.update(cls="keep", why="ein Container benutzt es")
        elif i.get("id") in need_ids:
            row.update(cls="keep", why="dasselbe Image braucht eine Instanz")
        elif is_platform(ref, platform_refs):
            row.update(cls="keep", why="Image der Plattform")
        elif not (is_built(ref) or ref.startswith(PREV_PREFIX)):
            n = base_of.get(ref) or 0
            row.update(cls="D", why=(f"Grundlage von {n} gebauten Image(s) -- ein Neubau lädt "
                                     "es dann neu" if n else "nicht von der Plattform gebaut"))
        elif age < YOUNG_SECONDS:
            row.update(cls="keep", why="jünger als ein Tag")
        elif is_built(ref) and rp in need_repos:
            if newest_other.get(rp) == ref:
                row.update(cls="keep", why="vorige Version (schneller Rückweg)")
            else:
                row.update(cls="B", why="ältere Version")
        else:
            orphan_now.add(ref)
            first = seen.get(ref) or now
            seen[ref] = first
            left = grace_days * 86400 - (now - first)
            if left > 0:
                row.update(cls="keep", why=f"ohne Instanz seit {now_iso(first)[:10]}, "
                                           f"Frist noch {max(1, -int(-left // 86400))} Tag(e)",
                           grace=first + grace_days * 86400)
            else:
                row.update(cls="C", why=f"ohne Instanz seit {now_iso(first)[:10]}")
        rows.append(row)
    seen = {r: t for r, t in seen.items() if r in orphan_now}
    return rows, seen


def summary(rows, cache=None):
    """Per class count and bytes. Bytes of B/C/D count each image id once
    (several tags of one image are one image); layers shared between
    images make the sum an upper bound -- the disk before/after is the
    measured truth."""
    out = {c: {"count": 0, "bytes": 0} for c in ("A", "B", "C", "D")}
    ids = {c: set() for c in out}
    for r in rows:
        c = r.get("cls")
        if c not in out:
            continue
        out[c]["count"] += 1
        if r["id"] not in ids[c]:
            ids[c].add(r["id"])
            out[c]["bytes"] += int(r.get("size") or 0)
    if cache:
        out["A"] = {"count": int(cache.get("count") or 0), "bytes": int(cache.get("bytes") or 0)}
    return out


def waiting(rows):
    """Images without an instance still inside the grace of D6 -- kept now,
    class C once it ends. Found on oaap-test 2026-11-28: 118 old images
    were "C 0" in the summary because the first run had seen them only two
    days before; the tool said nothing about them. -> {count, bytes, from}
    with `from` the epoch the first of them turns C (0 if none)."""
    w = [r for r in rows if r.get("cls") == "keep" and r.get("grace")]
    ids, size = set(), 0
    for r in w:
        if r["id"] not in ids:
            ids.add(r["id"])
            size += int(r.get("size") or 0)
    return {"count": len(w), "bytes": size, "from": min((r["grace"] for r in w), default=0)}


# --- when the nightly run is due --------------------------------------------

def due(last_run, now, local_hour, backup_on=False, backup_finished=None,
        backup_running=False):
    """The nightly run (D1): once a day, after the node's backup when it
    has one, otherwise from NIGHT_HOUR local. All times epoch."""
    if backup_running:
        return False
    if last_run and now - last_run < DUE_HOURS * 3600:
        return False
    if backup_on:
        return bool(backup_finished and backup_finished > (last_run or 0)
                    and now - backup_finished < AFTER_BACKUP_HOURS * 3600)
    return local_hour >= NIGHT_HOUR and local_hour < NIGHT_HOUR + AFTER_BACKUP_HOURS


# --- reading Docker ------------------------------------------------------------

def read_images(run=sh):
    """[{id, ref, created, size}] -- one row per tag, untagged with ref ''."""
    out = run(["docker", "image", "ls", "-a", "--no-trunc", "--format",
               "{{.ID}}\t{{.Repository}}\t{{.Tag}}\t{{.Size}}"])
    if out is None:
        return None
    pairs, shown = [], {}
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) not in (3, 4):
            continue
        iid, repo, tag = parts[:3]
        ref = "" if repo == "<none>" else (f"{repo}:{tag}" if tag and tag != "<none>" else "")
        pairs.append((iid, ref))
        # The size `docker image ls` shows is the unpacked one. `inspect`'s
        # .Size is the packed content with the containerd image store
        # (measured 2026-11-26 on oaap-test: 53 MB against 214 MB).
        if len(parts) == 4 and parse_size(parts[3]) is not None:
            shown[iid] = parse_size(parts[3])
    ids = sorted({p[0] for p in pairs})
    meta = {}
    for k in range(0, len(ids), 50):
        got = run(["docker", "image", "inspect", "--format",
                   "{{.Id}}\t{{.Created}}\t{{.Size}}", *ids[k:k + 50]])
        for line in (got or "").splitlines():
            p = line.split("\t")
            if len(p) == 3:
                try:
                    meta[p[0]] = (parse_iso(p[1]), int(p[2]))
                except ValueError:
                    pass
    rows, seen_pair = [], set()
    for iid, ref in pairs:
        if (iid, ref) in seen_pair:
            continue
        seen_pair.add((iid, ref))
        c, s = meta.get(iid, (None, 0))
        rows.append({"id": iid, "ref": ref, "created": c, "size": shown.get(iid, s)})
    return rows


def read_used_ids(run=sh):
    ids = (run(["docker", "ps", "-aq", "--no-trunc"]) or "").split()
    used = set()
    for k in range(0, len(ids), 50):
        got = run(["docker", "inspect", "--format", "{{.Image}}", *ids[k:k + 50]])
        used.update(x.strip() for x in (got or "").splitlines() if x.strip())
    return used


def read_cache(run=sh):
    """The build cache and dangling layers: {count, bytes} reclaimable."""
    out = run(["docker", "system", "df", "--format", "{{json .}}"]) or ""
    cache = {"count": 0, "bytes": 0}
    for line in out.splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("Type") == "Build Cache":
            cache["count"] = int(str(d.get("TotalCount") or "0").split()[0] or 0)
            cache["bytes"] = parse_size(str(d.get("Reclaimable") or "").split(" (")[0]) or 0
    return cache


def disk(path):
    try:
        st = os.statvfs(path)
    except (OSError, AttributeError):
        return None
    total = st.f_blocks * st.f_frsize
    free = st.f_bavail * st.f_frsize
    used = total - st.f_bfree * st.f_frsize
    return {"total": total, "used": used, "free": free,
            "percent": round(100.0 * used / (used + free), 1) if used + free else 0.0}


# --- doing it ----------------------------------------------------------------

def remove(rows, classes, run=sh):
    """`docker rmi` per image of the classes (never -f: Docker refuses an
    image in use on its own), then class A by Docker's prune. Returns
    {cls: {"removed": n, "refused": [(ref, why)]}}."""
    res = {c: {"removed": 0, "refused": []} for c in classes}
    for r in rows:
        c = r.get("cls")
        if c not in classes or c in ("A", "D"):
            continue
        out = run(["docker", "rmi", r["ref"]], timeout=300)
        if out is None:
            res[c]["refused"].append((r["ref"], "Docker hat abgelehnt"))
        else:
            res[c]["removed"] += 1
    if "A" in classes:
        ok1 = run(["docker", "image", "prune", "-f"], timeout=900) is not None
        # Not `-a --filter until=...`: measured 2026-11-26 on oaap-test
        # (Docker 29.7.1, buildx 0.36.1) it freed 0 B of 66 GB, whatever the
        # filter. `prune -f` frees the cache no image shares (66 GB there)
        # and keeps what an image still stands on (1.6 GB).
        ok2 = run(["docker", "builder", "prune", "-f"], timeout=900) is not None
        res["A"]["removed"] = int(ok1) + int(ok2)
        if not (ok1 and ok2):
            res["A"]["refused"].append(("prune", "Docker hat abgelehnt"))
    return res


def remove_one(ref, rows):
    """Class D, one image, by a person (D3): only what the plan calls D --
    an image in use or one the platform built is refused with the reason."""
    for r in rows:
        if r["ref"] == ref:
            if r["cls"] != "D":
                return False, (f"{ref} ist kein fremdes Image ({r['why']}) -- "
                               "das entfernt nur der Lauf selbst oder niemand")
            return True, ""
    return False, f"{ref} gibt es auf diesem Knoten nicht"


# --- the view the portal reads ----------------------------------------------

def view(rows, cache, disk_now, last=None, now=None):
    s = summary(rows, cache)
    w = waiting(rows)
    return {
        "schema": SCHEMA, "measured": now_iso(now),
        "disk": disk_now or {},
        "classes": {c: dict(s[c], title=CLASS_TITLES[c]) for c in s},
        "foreign": sorted(({"ref": r["ref"], "id": r["id"][:19], "size": r["size"],
                            "why": r["why"],
                            "created": now_iso(r["created"])[:10] if r["created"] else ""}
                           for r in rows if r["cls"] == "D"),
                          key=lambda x: -x["size"]),
        "candidates": [{"ref": r["ref"], "cls": r["cls"], "size": r["size"], "why": r["why"]}
                       for r in rows if r["cls"] in ("B", "C")],
        "kept": [{"ref": r["ref"], "why": r["why"]} for r in rows if r["cls"] == "keep"],
        "waiting": dict(w, **{"from": now_iso(w["from"])[:10] if w["from"] else ""}),
        "last": last or {},
    }


def result_line(res, before, after, freeable):
    """One German sentence for audit, mail and CLI."""
    parts = []
    for c in ("B", "C"):
        if c in res and res[c]["removed"]:
            parts.append(f"{res[c]['removed']} {'ältere Version(en)' if c == 'B' else 'ohne Instanz'}")
    freed = max(0, (before or {}).get("used", 0) - (after or {}).get("used", 0))
    if "A" in res and freed and freeable.get("A", {}).get("bytes"):
        parts.append(f"Zwischenspeicher bis {fmt_bytes(freeable['A']['bytes'])}")
    freed = max(0, (before or {}).get("used", 0) - (after or {}).get("used", 0))
    text = (f"{fmt_bytes(freed)} freigegeben" + (f" ({', '.join(parts)})" if parts else "")
            if freed or parts else "nichts freizugeben")
    if before and after:
        text += f"; Platte {before['percent']:.0f} % → {after['percent']:.0f} %"
    refused = sum(len(v["refused"]) for v in res.values())
    if refused:
        text += f"; {refused} abgelehnt"
    return text, freed
