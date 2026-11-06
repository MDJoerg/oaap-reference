#!/usr/bin/env python3
"""Images (RFC-0071 stage 1, oaap.core.updates 2.9, 0.1.227).

Executed without Docker -- every docker call is a recorded answer:

1. the base of a built image is the final stage's FROM, through aliases;
   a variable or `scratch` is not guessed;
2. the local digest is the one of the SAME repository in RepoDigests;
   upstream's digest is read from `imagetools inspect`;
3. the words: open > unknown > stale > behind > fresh; no record -> unknown;
4. the reading: inside the image with no network, read-only, no pull;
   no dpkg -> unknown (never 0); a removed image -> unknown, said why;
   one image read once for many instances; a cache that holds per list;
5. upstream asked once a day per distinct reference, inside the budget;
6. the readers: one line, fleet counts without names, attention.

Run: python3 test/test_images.py
"""
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services"))

import advisories as adv  # noqa: E402
import host_care as hc  # noqa: E402
import images as im  # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f"  -- {detail}"))


def older(a, b):
    import re
    return tuple(int(n) for n in re.findall(r"\d+", a)) < tuple(int(n) for n in re.findall(r"\d+", b))


# 1. the base
ok("1 one stage", im.dockerfile_base("FROM python:3.13-slim\nRUN x\n") == "python:3.13-slim")
ok("1 multi-stage: the last FROM",
   im.dockerfile_base("FROM node:22 AS build\nRUN b\nFROM python:3.13-slim\n") == "python:3.13-slim")
ok("1 a stage on a stage: followed to its base",
   im.dockerfile_base("FROM python:3.13-slim AS base\nFROM base AS final\n") == "python:3.13-slim")
ok("1 --platform is skipped", im.dockerfile_base("FROM --platform=$B debian:13\n") == "debian:13")
ok("1 a variable is not guessed", im.dockerfile_base("ARG V=3\nFROM python:${V}-slim\n") == "")
ok("1 scratch is no base", im.dockerfile_base("FROM scratch\n") == "")
ok("1 lower-case from", im.dockerfile_base("from caddy:2\n") == "caddy:2")

# 2. digests
D1, D2 = "sha256:" + "a" * 64, "sha256:" + "b" * 64
ok("2 repo of a tagged ref", im.repo_of("python:3.13-slim") == "python")
ok("2 repo with a registry port and tag", im.repo_of("reg:5000/x/y:1") == "reg:5000/x/y")
ok("2 local digest: the same repository, docker.io normalised",
   im.local_digest(["other@" + D2, "docker.io/library/python@" + D1], "python:3.13-slim") == D1)
ok("2 no digest of that repository -> ''", im.local_digest(["other@" + D2], "python:3") == "")
ok("2 imagetools: the index digest",
   im.parse_imagetools(f"Name:      docker.io/library/python:3.13-slim\nMediaType: x\nDigest:    {D2}\n") == D2)

# 3. the words
ok("3 open wins", im.service_state(3, False, 0) == "open")
ok("3 no dpkg -> unknown", im.service_state(None, False, 0) == "unknown")
ok("3 no record -> unknown, never fresh", im.service_state(0, None, 0) == "unknown")
ok("3 behind", im.service_state(0, True, 3600) == "behind")
ok("3 fresh", im.service_state(0, False, 3600) == "fresh")
ok("3 an old answer: stale, then unknown",
   im.service_state(0, False, 3 * 86400) == "stale" and im.service_state(0, False, 8 * 86400) == "unknown")
ok("3 worst of the services", im.worst(["fresh", "open", "behind"]) == "open")

# 4./5. the reading against recorded docker answers
LIST = ("[04 Nov 2026] DSA-6607-1 apt - security update\n\t[trixie] - apt 3.0.4\n"
        "[30 Sep 2026] DSA-6500-1 openssl - security update\n\t[trixie] - openssl 3.5.4-1~deb13u1\n")
PK_OLD = "apt\t3.0.3\tapt\tinstall ok installed\nopenssl\t3.5.1\tlibssl3t64\tinstall ok installed\n"
calls = []
IDS = {"oaap-app-a": "sha256:img1", "oaap-app-b": "sha256:img1", "oaap-app-c": "sha256:img2",
       "oaap-app-k": "sha256:img3", "oaap-app-gone": "sha256:img9"}


def run(cmd, timeout=60):
    calls.append(cmd)
    if cmd[:3] == ["docker", "inspect", "-f"]:
        return IDS.get(cmd[-1], "") + "\n"
    if cmd[:3] == ["docker", "image", "inspect"]:
        ref = cmd[-1]
        if ref == "sha256:img9":
            return None                                  # removed
        if ref in ("sha256:img3", "quay.io/k/k:26"):
            return json.dumps([{"Id": "sha256:img3", "RepoDigests": ["quay.io/k/k@" + D1]}])
        return json.dumps([{"Id": ref, "RepoDigests": []}])
    if cmd[:4] == ["docker", "buildx", "imagetools", "inspect"]:
        return {"python:3.13-slim": f"Digest: {D2}\n", "quay.io/k/k:26": f"Digest: {D1}\n"}.get(cmd[-1])
    if cmd[:2] == ["docker", "run"]:
        assert "--network" in cmd and cmd[cmd.index("--network") + 1] == "none"
        assert "--read-only" in cmd and "--pull" in cmd
        img = cmd[cmd.index("--entrypoint") + 2]
        if "dpkg-query" in cmd:
            return {"sha256:img1": PK_OLD, "sha256:img2": "apt\t3.0.4\tapt\tinstall ok installed\n"}.get(img)
        return "VERSION_CODENAME=trixie\n"
    if cmd[:2] == ["docker", "ps"]:
        return "oaap-gateway-1\tcaddy:2\noaap-connect-1\ted94e9bc8436\n"
    return ""


INST = {
    "a": {"services": [{"container": "oaap-app-a", "image": "oaap-app/x:1", "build": ".",
                        "service": "", "base": {"ref": "python:3.13-slim", "digest": D1,
                                                "built": "2026-11-06T07:00:00Z"}}]},
    "b": {"services": [{"container": "oaap-app-b", "image": "oaap-app/x:1", "build": ".", "service": ""}]},
    "c": {"services": [{"container": "oaap-app-c", "image": "oaap-app/y:1", "build": ".", "service": "",
                        "base": {"ref": "python:3.13-slim", "digest": D2, "built": "x"}}]},
    "k": {"services": [{"container": "oaap-app-k", "image": "quay.io/k/k:26", "build": "", "service": ""}]},
    "gone": {"services": [{"container": "oaap-app-gone", "image": "oaap-app/z:1", "build": ".", "service": ""}]},
}
d = tempfile.mkdtemp()
open(os.path.join(d, adv.LIST_FILE), "w").write(LIST)
json.dump({"read_at": "2026-11-06T06:00:00Z"}, open(os.path.join(d, adv.STATE_FILE), "w"))
T0 = datetime(2026, 11, 6, 8, 0, tzinfo=timezone.utc)
st = im.refresh(d, INST, now=T0, run=run, older=older)
s = im.summary(st, T0)
I = s["instances"]
ok("4 a: built on a base upstream has left, 2 open -> open", I["a"]["state"] == "open"
   and I["a"]["open"] == 2 and I["a"]["oldest"] == "2026-09-30", I["a"])
ok("4 b: the same image, read once, attributed too; no record -> base unknown",
   I["b"]["open"] == 2 and I["b"]["services"][0]["base"] == "", I["b"])
ok("4 one image, one docker run per reading (img1 once)",
   sum(1 for c in calls if c[:2] == ["docker", "run"] and "sha256:img1" in c and "dpkg-query" in c) == 1)
ok("4 c: on the upstream base, 0 open -> fresh", I["c"]["state"] == "fresh", I["c"])
ok("4 k: a foreign image without dpkg -> unknown, never 0, its base = itself, = upstream",
   I["k"]["state"] == "unknown" and I["k"]["open"] is None
   and I["k"]["services"][0]["behind"] is False, I["k"])
ok("4 gone: a removed image is unknown and says why",
   I["gone"]["state"] == "unknown" and "removed" in I["gone"]["services"][0]["why"], I["gone"])
ok("4 core: the gateway read too; a bare id is not taken as a foreign reference",
   s["core"]["oaap-gateway-1"]["base"] == "caddy:2" and s["core"]["oaap-connect-1"]["base"] == "")
ok("4 the counts", s["counts"]["open"] == 2 and s["counts"]["fresh"] == 1
   and s["counts"]["instances"] == 5 and s["counts"]["oldest"] == "2026-09-30", s["counts"])
ok("4 no package name in the summary", "openssl" not in json.dumps(s))
ok("4 ... but on the node, for the server_admin", [i["package"] for i in im.names_for(st, "sha256:img1")]
   == ["openssl", "apt"])
n_runs = sum(1 for c in calls if c[:2] == ["docker", "run"])
calls.clear()
st = im.refresh(d, INST, now=T0 + timedelta(minutes=10), run=run, older=older)
ok("4 cache: the same list, the same images -> no docker run, no upstream question",
   not any(c[:2] == ["docker", "run"] or "imagetools" in c for c in calls), calls[:3])
json.dump({"read_at": "2026-11-07T06:00:00Z"}, open(os.path.join(d, adv.STATE_FILE), "w"))
calls.clear()
im.refresh(d, INST, now=T0 + timedelta(hours=1), run=run, older=older)
ok("4 a new list: the images are read again", any(c[:2] == ["docker", "run"] for c in calls))
calls.clear()
im.refresh(d, INST, now=T0 + timedelta(days=1, minutes=1), run=run, older=older)
ok("5 a day later: upstream asked again, once per distinct reference",
   sorted(c[-1] for c in calls if "imagetools" in c) == ["caddy:2", "python:3.13-slim", "quay.io/k/k:26"],
   [c[-1] for c in calls if "imagetools" in c])
tick = iter(range(0, 1000, 30))
calls.clear()
json.dump({"read_at": "2026-11-08T06:00:00Z"}, open(os.path.join(d, adv.STATE_FILE), "w"))
im.refresh(d, INST, now=T0 + timedelta(days=3), run=run, older=older, clock=lambda: next(tick), budget=25)
ok("5 the budget: past it, nothing more is read this minute",
   sum(1 for c in calls if c[:2] == ["docker", "run"]) <= 2, [c for c in calls if c[:2] == ["docker", "run"]])
shutil.rmtree(d)

# 6. the readers
facts = {"measured": "2026-11-06T08:00:00Z", "kernel": {"running": "a", "installed": "a"},
         "auto_updates": {"state": "ok", "last_run": "x"}, "gateway": {"mptcp": "off"},
         "advisories": None, "images": s}
line = hc.images_line(s)
ok("6 one line: counts, oldest, the command", line[0] == "warn"
   and "5 instances — 2 with open advisories (the oldest since 2026-09-30)" in line[1], line)
ok("6 the line is in the status", any(t.startswith("Images:") for _, t in hc.status_lines(facts)))
ok("6 German", "Abbilder: 5 Instanzen" in hc.images_line(s, de=True)[1])
fb = hc.fleet_host(facts)
ok("6 fleet: counts only", fb["images"]["open"] == 2 and fb["images"]["oldest"] == "2026-09-30"
   and "oaap-app" not in json.dumps(fb) and "python" not in json.dumps(fb), fb.get("images"))
ok("6 attention images_open", "images_open" in [a["kind"] for a in hc.fleet_attention(facts)])
ok("6 the door an instance reads carries nothing of it", "image" not in json.dumps(hc.door_facts(facts)))
ok("6 no reading -> no line, no block", hc.images_line(None) is None
   and "images" not in hc.fleet_host(dict(facts, images=None)))

print()
print("FAILED" if fails else "ALL PASS", f"({fails} failing)")
sys.exit(1 if fails else 0)
