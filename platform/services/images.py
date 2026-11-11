"""Images (oaap.apps.runtime 2.30, oaap.core.updates 2.9, RFC-0071 stage 1):
what every instance's image stands on, whether upstream has moved, and
how many Debian advisories are open INSIDE it.

Why this exists -- 2026-11-05, measured on oaap-test: an instance's image
is built on the node from the package, on a base the node pulled once,
and no later build pulls again. `python:3.13-slim` (fourteen of our apps)
was from 28 October and behind upstream; the gateway's `caddy:2` and the
broker's `eclipse-mosquitto:2` were from 22 June. RFC-0070 told the node
what is open in ITS packages; the images carry packages of their own.

This stage READS; it rebuilds nothing (stage 2). The rules, each a
function a test executes without Docker:

- the base of a built image is the `FROM` of its Dockerfile -- the last
  stage's, followed through stage aliases; a `FROM` with a variable is
  not guessed (`dockerfile_base`);
- the base record is written AT BUILD (the node's local image for that
  reference, its digest, the time); an image built before has none and is
  `unknown`, never `fresh` (`base_record`);
- a foreign image (`image:` in the manifest) is its own base: its local
  digest is compared with upstream directly;
- upstream is asked once a day per DISTINCT reference (the registries
  rate-limit), and an answer that does not come keeps the last one with
  its age (`need_ask`);
- the advisories inside an image are RFC-0070's comparison against the
  image's own package list, read with no network, read-only, the image's
  own user, never in a running container (`read_image`); an image without
  dpkg (Alpine) is `unknown`, never "0 open";
- one image serves many instances: read once per image id, attributed to
  each (`summary`).

Everything that touches Docker goes through `run`, so the tests replace it.
"""
import json
import os
import re
import subprocess
import time
from datetime import datetime, timedelta, timezone

import advisories

STATE_FILE = "images-state.json"
ASK_EVERY = 24 * 3600
STALE_AFTER = 2 * 86400     # an upstream answer older than this: `stale`
UNKNOWN_AFTER = 7 * 86400   # ... older than this: `unknown`
BUDGET = 25                 # seconds of image reading per minute job
ORDER = {"fresh": 0, "behind": 1, "stale": 2, "unknown": 3, "open": 4}
DPKG_FORMAT = "${source:Package}\t${source:Version}\t${Package}\t${Status}\n"


def sh(cmd, timeout=60):
    env = dict(os.environ, LC_ALL="C", LANG="C")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 else None


def now_iso(now=None):
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _age(iso_s, now):
    t = advisories.parse_iso(iso_s or "")
    return None if t is None else (now - t).total_seconds()


# --- the base of a built image -------------------------------------------------

def dockerfile_base(text):
    """The reference the final stage stands on, '' when it cannot be said
    without guessing (a variable, `scratch`, no FROM)."""
    stages, last = {}, ""
    for raw in (text or "").splitlines():
        line = raw.strip()
        m = re.match(r"(?i)^FROM\s+(?:--\S+\s+)*(\S+)(?:\s+AS\s+(\S+))?\s*$", line)
        if not m:
            continue
        ref = m.group(1)
        ref = stages.get(ref.lower(), ref)      # a stage built on a stage
        if m.group(2):
            stages[m.group(2).lower()] = ref
        last = ref
    if not last or "$" in last or last.lower() == "scratch":
        return ""
    return last


_UPGRADE_RE = re.compile(
    r"(?i)(?:\b(?:apt-get|apt|aptitude)\b(?:\s+-\S+)*\s+(?:-\S+\s+)*"
    r"(?:upgrade|dist-upgrade|full-upgrade)\b)"
    r"|(?:\bapk\b(?:\s+-\S+)*\s+upgrade\b)")


def dockerfile_upgrades(text):
    """True when a RUN instruction of the final stage upgrades the OS
    packages (apt-get/apt upgrade, dist-upgrade, apk upgrade).

    Why the node needs to know (2026-11-11, zlib DSA-6613-1): Debian fixes
    a package days before the official base image is published again. An
    app that upgrades while building heals on a rebuild in that gap -- but
    only if the build does not take that RUN layer from Docker's cache,
    which it would, because the base did not move. So: such a Dockerfile
    is built with --no-cache (see appctl.docker_build_cmd), and the card
    says a rebuild helps."""
    lines, cur = [], ""
    for raw in (text or "").splitlines():
        s = raw.strip()
        if s.startswith("#") and not cur:
            continue
        if s.endswith("\\"):
            cur += s[:-1] + " "
            continue
        lines.append(cur + s)
        cur = ""
    if cur:
        lines.append(cur)
    final = []
    for line in lines:
        if re.match(r"(?i)^FROM\s", line):
            final = []
        elif re.match(r"(?i)^RUN\s", line):
            final.append(line)
    return any(_UPGRADE_RE.search(line) for line in final)


def repo_of(ref):
    """`python:3.13-slim` -> `python`; `quay.io/x/y:1@sha256:..` -> `quay.io/x/y`."""
    r = ref.split("@", 1)[0]
    if ":" in r.rsplit("/", 1)[-1]:
        r = r.rsplit(":", 1)[0]
    return r


def _norm(repo):
    r = repo
    for p in ("docker.io/library/", "docker.io/", "index.docker.io/library/"):
        if r.startswith(p):
            r = r[len(p):]
    return r


def local_digest(repo_digests, ref):
    """The index digest the node holds for `ref`, from `RepoDigests`."""
    want = _norm(repo_of(ref))
    for d in repo_digests or []:
        repo, _, dig = d.partition("@")
        if _norm(repo) == want and dig.startswith("sha256:"):
            return dig
    return ""


def inspect_image(ref, run=sh):
    out = run(["docker", "image", "inspect", ref])
    try:
        data = json.loads(out or "[]")
    except ValueError:
        data = []
    return data[0] if data else {}


def base_record(build_dir, run=sh, now=None):
    """Written by the deploy right after `docker build` (RFC-0071 §3): the
    base the build used is the node's local image of the FROM reference."""
    try:
        with open(os.path.join(build_dir, "Dockerfile"), encoding="utf-8") as f:
            text = f.read()
    except OSError:
        text = ""
    ref = dockerfile_base(text)
    if not ref:
        return {"ref": "", "digest": "", "built": now_iso(now)}
    info = inspect_image(ref, run)
    return {"ref": ref, "digest": local_digest(info.get("RepoDigests"), ref),
            "image": info.get("Id", ""), "built": now_iso(now),
            # the build upgrades OS packages itself (dockerfile_upgrades)
            "upgrades": dockerfile_upgrades(text)}


def build_dir_upgrades(build_dir):
    try:
        with open(os.path.join(build_dir, "Dockerfile"), encoding="utf-8") as f:
            return dockerfile_upgrades(f.read())
    except OSError:
        return False


def package_list(image_id, run=sh):
    """The image's dpkg list, sorted -- '' when it cannot be read. Used to
    tell a no-cache rebuild that brought nothing new from one that did."""
    out = run(["docker", "run", "--rm", "--network", "none", "--read-only",
               "--pull", "never", "--log-driver", "none", "--entrypoint",
               "dpkg-query", image_id, "-W", "-f", DPKG_FORMAT], timeout=60)
    return "\n".join(sorted((out or "").splitlines()))


# --- upstream ---------------------------------------------------------------------

def parse_imagetools(out):
    m = re.search(r"^Digest:\s+(sha256:[0-9a-f]{64})", out or "", re.M)
    return m.group(1) if m else ""


def need_ask(entry, now):
    a = _age((entry or {}).get("asked", ""), now)
    return a is None or a >= ASK_EVERY


def ask_upstream(ref, run=sh):
    return parse_imagetools(run(["docker", "buildx", "imagetools", "inspect", ref], timeout=30))


# --- inside an image --------------------------------------------------------------

def suite_from(os_release):
    for line in (os_release or "").splitlines():
        if line.startswith("VERSION_CODENAME="):
            return line.split("=", 1)[1].strip().strip('"')
    return ""


def read_image(image_id, list_text, run=sh, older=advisories.dpkg_older):
    """RFC-0070's comparison against the image's own packages.

    No network, read-only, no pull, the image's own user, removed after.
    No dpkg in the image -> `open: None` (unknown), never 0."""
    base = ["docker", "run", "--rm", "--network", "none", "--read-only",
            "--pull", "never", "--log-driver", "none"]
    pk = run(base + ["--entrypoint", "dpkg-query", image_id, "-W", "-f", DPKG_FORMAT],
             timeout=60)
    if not pk:
        return {"open": None, "why": "no dpkg in the image"}
    suite = suite_from(run(base + ["--entrypoint", "cat", image_id, "/etc/os-release"],
                           timeout=60))
    if not suite:
        return {"open": None, "why": "no release name in the image"}
    entries, _ = advisories.parse(list_text)
    inst = advisories.installed(pk)
    # Inside an image the archives are not known (slim images carry no
    # package lists): every package is compared as the distribution's.
    kinds = {s: "debian" for s in inst}
    items, covered = advisories.compare(entries, inst, suite, kinds, older)
    return {"open": len(items), "suite": suite, "covered": covered,
            "oldest": items[0]["published"] if items else "",
            "items": items}


# --- the state of an instance -------------------------------------------------------

def service_state(open_n, behind, upstream_age):
    """One word (RFC-0071 §3) for one service."""
    if open_n is None:
        return "unknown"
    if open_n:
        return "open"
    if behind is None:
        return "unknown"
    if upstream_age is not None and upstream_age > UNKNOWN_AFTER:
        return "unknown"
    if upstream_age is not None and upstream_age > STALE_AFTER:
        return "stale"
    return "behind" if behind else "fresh"


def worst(words):
    return max(words, key=lambda w: ORDER.get(w, 3), default="unknown")


def targets(instances, run=sh):
    """[(instance, service, image ref, base_ref, base_digest, foreign)] from
    the registry records; the image id is what the container RUNS."""
    out = []
    for name, inst in sorted(instances.items()):
        for s in inst.get("services") or [{"container": inst.get("container"),
                                           "image": inst.get("image"),
                                           "build": inst.get("build", ""),
                                           "service": ""}]:
            img = s.get("image") or ""
            if not img:
                continue
            foreign = not img.startswith("oaap-app/")
            base = s.get("base") or {}
            out.append({"instance": name, "service": s.get("service", ""),
                        "container": s.get("container", ""), "image": img,
                        "foreign": foreign,
                        "base_ref": img if foreign else base.get("ref", ""),
                        "base_digest": base.get("digest", ""),
                        "built": base.get("built", ""),
                        # None: recorded before 0.1.252 (or a foreign image)
                        "upgrades": None if foreign else base.get("upgrades")})
    return out


def core_targets(run=sh):
    """The platform's own containers (RFC-0071 §6: read now, rebuilt in
    stage 4) -- the compose project `oaap`."""
    out = run(["docker", "ps", "--filter", "label=com.docker.compose.project=oaap",
               "--format", "{{.Names}}\t{{.Image}}"]) or ""
    rows = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) == 2:
            # A bare id: the tag moved on to a newer build while this
            # container kept running the old one (measured 2026-11-06 on
            # oaap-test: connect, twin, relay) -- built here, base unknown.
            bare = re.fullmatch(r"(sha256:)?[0-9a-f]{12,64}", parts[1]) is not None
            foreign = not bare and not parts[1].startswith(("oaap-", "oaap_", "oaap/"))
            rows.append({"instance": "", "service": parts[0], "container": parts[0],
                         "image": parts[1], "foreign": foreign,
                         "base_ref": parts[1] if foreign else "",
                         "base_digest": "", "built": ""})
    return rows


def container_image_id(container, image, run=sh):
    iid = (run(["docker", "inspect", "-f", "{{.Image}}", container]) or "").strip() \
        if container else ""
    if not iid:
        iid = inspect_image(image, run).get("Id", "")
    return iid


# --- the cache and the refresh ----------------------------------------------------

def load(apps_dir):
    try:
        with open(os.path.join(apps_dir, STATE_FILE), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save(apps_dir, state):
    path = os.path.join(apps_dir, STATE_FILE)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1, ensure_ascii=False)
    os.replace(tmp, path)
    os.chmod(path, 0o644)


def refresh(apps_dir, instances, now=None, budget=BUDGET, run=sh,
            older=advisories.dpkg_older, clock=time.monotonic, force=False,
            include_core=True):
    """Read what is due inside the budget; the rest comes next minute.
    Never raises. Returns the state."""
    now = now or datetime.now(timezone.utc)
    state = load(apps_dir)
    adv = advisories.load(apps_dir)
    list_at = adv.get("read_at", "")
    list_path = os.path.join(apps_dir, advisories.LIST_FILE)
    rows = targets(instances, run) + (core_targets(run) if include_core else [])
    t0 = clock()
    up = state.setdefault("upstream", {})
    imgs = state.setdefault("images", {})
    seen_ids = set()
    text = None
    for r in rows:
        r["image_id"] = container_image_id(r["container"], r["image"], run)
        if r["foreign"] and r["image_id"]:
            r["base_digest"] = local_digest(
                inspect_image(r["image_id"], run).get("RepoDigests"), r["image"])
    for ref in sorted({r["base_ref"] for r in rows if r["base_ref"]}):
        if (force or need_ask(up.get(ref), now)) and clock() - t0 < budget:
            d = ask_upstream(ref, run)
            e = up.setdefault(ref, {})
            if d:
                e.update({"digest": d, "asked": now_iso(now), "error": ""})
            else:
                e["error"] = "no answer"
                e["tried"] = now_iso(now)
                e.setdefault("asked", "")
                if not e.get("digest"):
                    # retried after an hour, not every minute
                    e["asked"] = now_iso(now - timedelta(seconds=ASK_EVERY - 3600))
    for r in rows:
        iid = r["image_id"]
        if not iid or iid in seen_ids:
            continue
        seen_ids.add(iid)
        cur = imgs.get(iid)
        if cur and cur.get("list") == list_at and not force:
            continue
        if clock() - t0 >= budget:
            continue
        if text is None:
            try:
                with open(list_path, encoding="utf-8") as f:
                    text = f.read()
            except OSError:
                text = ""
        if not text:
            break
        if not inspect_image(iid, run):
            # Measured 2026-11-06 on oaap-test (connect, twin, relay): the
            # tag moved to a newer build, the old image was removed, the
            # container still runs on it. Nothing can be read from it.
            res = {"open": None, "why": "the image this container runs was removed "
                                        "(a newer build took its tag)"}
        else:
            res = read_image(iid, text, run, older)
        res.update({"list": list_at, "read": now_iso(now), "image": r["image"]})
        imgs[iid] = res
    # forget images no container uses any more
    for iid in list(imgs):
        if iid not in {r["image_id"] for r in rows}:
            imgs.pop(iid, None)
    state["rows"] = [{k: r[k] for k in ("instance", "service", "container", "image",
                                        "foreign", "base_ref", "base_digest", "built",
                                        "image_id")}
                     | {"upgrades": r.get("upgrades")} for r in rows]
    state["computed"] = now_iso(now)
    try:
        save(apps_dir, state)
    except OSError:
        pass
    return state


def summary(state, now=None):
    """What host care carries (no package names): per instance and per
    core container the word, the count and the oldest; and the counts."""
    now = now or datetime.now(timezone.utc)
    if not state or "rows" not in state:
        return None
    up, imgs = state.get("upstream", {}), state.get("images", {})
    inst, core = {}, {}
    for r in state["rows"]:
        im = imgs.get(r.get("image_id")) or {}
        u = up.get(r["base_ref"]) or {}
        behind = None
        if r["base_ref"] and r["base_digest"] and u.get("digest"):
            behind = u["digest"] != r["base_digest"]
        open_n = im.get("open") if im else None
        word = service_state(open_n, behind, _age(u.get("asked", ""), now))
        svc = {"service": r["service"], "image": r["image"], "state": word,
               "open": open_n, "oldest": im.get("oldest", ""),
               "base": r["base_ref"], "built": r["built"], "behind": behind,
               "upgrades": r.get("upgrades"),
               "why": im.get("why", "") if open_n is None else ""}
        if r["instance"]:
            e = inst.setdefault(r["instance"], {"services": []})
            e["services"].append(svc)
        else:
            core[r["service"]] = svc
    for e in inst.values():
        e["state"] = worst(s["state"] for s in e["services"])
        opens = [s["open"] for s in e["services"] if s["open"]]
        e["open"] = sum(opens) if opens else (0 if all(s["open"] == 0 for s in e["services"])
                                              else None)
        olds = [s["oldest"] for s in e["services"] if s["oldest"]]
        e["oldest"] = min(olds) if olds else ""
    words = [e["state"] for e in inst.values()]
    olds = [e["oldest"] for e in inst.values() if e["oldest"]]
    counts = {w: words.count(w) for w in ORDER}
    counts["instances"] = len(inst)
    counts["oldest"] = min(olds) if olds else ""
    counts["core_open"] = sum(1 for s in core.values() if s["state"] == "open")
    return {"computed": state.get("computed", ""), "counts": counts,
            "instances": inst, "core": core}


def names_for(state, image_id):
    """The package names of one image -- the server_admin's view only."""
    return ((state or {}).get("images", {}).get(image_id) or {}).get("items") or []
