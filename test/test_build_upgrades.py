#!/usr/bin/env python3
"""A build that upgrades OS packages heals in the gap between Debian's fix
and the base image's republication (0.1.252, RFC-0071 §16).

Anlass (2026-11-11): zlib DSA-6613-1 vom 07.11. -- Debian hatte die
Korrektur, `python:3.13-slim` noch nicht; "Neu bauen" holte dieselbe Basis,
die Meldung blieb, und die Karte sagte nicht klar, warum.

1. images.dockerfile_upgrades: apt-get/apt upgrade, dist-upgrade, apk
   upgrade in a RUN of the FINAL stage; continuation lines; comments and
   earlier stages do not count; `pip install --upgrade` is not an OS upgrade;
2. appctl.docker_build_cmd: --no-cache exactly then (otherwise Docker takes
   the RUN layer from the cache and the upgrade never runs again); every
   build path uses it -- no second `docker build` in appctl;
3. base_record records `upgrades`; the reading carries it to the summary;
4. host_care.rebuild_helps: upgrading app -> "likely" with its own sentence
   even when the base equals upstream; not upgrading (recorded False) ->
   "only-upgrade" with the sentence that says no and why; unrecorded
   (None, built before 0.1.252) keeps the general sentence;
5. the image card names package, DSA, date, installed and fixed version
   when the view carries them (server_admin), and nothing of it otherwise.

Run: python3 test/test_build_upgrades.py   (no Docker, no node)
"""
import ast
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-build-upgrades-test-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, os.path.join(HERE, "..", "platform"))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services"))
sys.path.insert(0, HERE)

import appctl as m  # noqa: E402
import images  # noqa: E402
import host_care  # noqa: E402
from template_src import resolve  # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:700]}")


# 1. what counts as an upgrade
UP = images.dockerfile_upgrades
ok("1 apt-get update && apt-get -y upgrade",
   UP("FROM python:3.13-slim\nRUN apt-get update && apt-get -y upgrade && rm -rf /var/lib/apt/lists/*\n"))
ok("1 continuation lines",
   UP("FROM debian:13\nRUN apt-get update \\\n    && DEBIAN_FRONTEND=noninteractive apt-get \\\n"
      "       -y --no-install-recommends upgrade\n"))
ok("1 apt full-upgrade, apt-get dist-upgrade, apk upgrade",
   UP("FROM x\nRUN apt full-upgrade -y\n") and UP("FROM x\nRUN apt-get -qq dist-upgrade\n")
   and UP("FROM alpine:3\nRUN apk --no-cache upgrade\n"))
ok("1 no upgrade: install only", not UP("FROM x\nRUN apt-get update && apt-get install -y curl\n"))
ok("1 pip install --upgrade is not an OS upgrade",
   not UP("FROM x\nRUN pip install --upgrade pip\n"))
ok("1 a comment does not count",
   not UP("FROM x\n# RUN apt-get -y upgrade\nRUN true\n"))
ok("1 only the final stage counts",
   not UP("FROM debian:13 AS b\nRUN apt-get -y upgrade\nFROM python:3.13-slim\nCOPY --from=b /x /x\n")
   and UP("FROM debian:13 AS b\nRUN true\nFROM python:3.13-slim\nRUN apt-get -y upgrade\n"))
ok("1 no Dockerfile text -> False", UP("") is False and UP(None) is False)

# 2. the one build command
tmp = tempfile.mkdtemp(dir=DATA)
with open(os.path.join(tmp, "Dockerfile"), "w", encoding="utf-8") as f:
    f.write("FROM python:3.13-slim\nRUN apt-get update && apt-get -y upgrade\n")
plain = tempfile.mkdtemp(dir=DATA)
with open(os.path.join(plain, "Dockerfile"), "w", encoding="utf-8") as f:
    f.write("FROM python:3.13-slim\nCOPY app.py /srv/\n")
ok("2 upgrading Dockerfile -> --no-cache",
   m.docker_build_cmd("oaap-app/a:1", tmp) == ["docker", "build", "-q", "--no-cache",
                                               "-t", "oaap-app/a:1", tmp])
ok("2 plain Dockerfile -> cached build as before",
   m.docker_build_cmd("oaap-app/b:1", plain) == ["docker", "build", "-q", "-t",
                                                 "oaap-app/b:1", plain])
ok("2 a missing Dockerfile -> no --no-cache, the build itself says what is wrong",
   "--no-cache" not in m.docker_build_cmd("x", os.path.join(DATA, "nope")))
src = open(m.__file__, encoding="utf-8").read()
builds = re.findall(r'\["docker",\s*"build"', src)
ok("2 appctl has exactly one docker build (two paths, one rule)", len(builds) == 1, builds)

# 3. the record and the reading
runs = []


def fake(cmd, timeout=60):
    runs.append(cmd)
    if cmd[:3] == ["docker", "image", "inspect"]:
        return '[{"Id": "sha256:base", "RepoDigests": ["python@sha256:' + "a" * 64 + '"]}]'
    return ""


rec = images.base_record(tmp, run=fake)
ok("3 base_record: upgrades True", rec.get("upgrades") is True and rec["ref"] == "python:3.13-slim",
   rec)
ok("3 base_record: upgrades False", images.base_record(plain, run=fake).get("upgrades") is False)
t = images.targets({"a": {"services": [{"service": "", "container": "c", "image": "oaap-app/a:1",
                                        "base": {"ref": "python:3.13-slim",
                                                 "digest": "sha256:" + "a" * 64,
                                                 "upgrades": True}}]},
                    "old": {"services": [{"service": "", "container": "o", "image": "oaap-app/o:1",
                                          "base": {"ref": "python:3.13-slim", "digest": "d"}}]},
                    "f": {"container": "f", "image": "quay.io/k/k:1"}})
by = {r["instance"]: r for r in t}
ok("3 targets carry upgrades: True / None (before 0.1.252) / None (foreign)",
   by["a"]["upgrades"] is True and by["old"]["upgrades"] is None and by["f"]["upgrades"] is None, t)
state = {"rows": [dict(by["a"], image_id="sha256:i1")],
         "images": {"sha256:i1": {"open": 1, "oldest": "2026-11-07", "items": []}},
         "upstream": {"python:3.13-slim": {"digest": "sha256:" + "a" * 64,
                                           "asked": images.now_iso()}}}
sm = images.summary(state)
ok("3 summary carries upgrades per service",
   sm["instances"]["a"]["services"][0]["upgrades"] is True, sm)

# 4. will a rebuild help
base = {"open": 1, "behind": False}
w, txt = host_care.rebuild_helps([dict(base, upgrades=True)], de=True)
ok("4 upgrading app, base = upstream -> likely, says it heals", w == "likely"
   and "apt-get upgrade" in txt and "heilt" in txt, (w, txt))
w, txt = host_care.rebuild_helps([dict(base, upgrades=False)], de=True)
ok("4 not upgrading, base = upstream -> only-upgrade, says no and why",
   w == "only-upgrade" and txt.startswith("Nein.") and "Debian hat die Korrektur" in txt
   and "dieselben Pakete" in txt and "Store-Update" in txt, (w, txt))
w, txt = host_care.rebuild_helps([dict(base, upgrades=None)], de=True)
ok("4 unrecorded (before 0.1.252) -> the general sentence as before",
   w == "only-upgrade" and "apt-get upgrade" in txt and not txt.startswith("Nein."), (w, txt))
w, _ = host_care.rebuild_helps([dict(base, behind=True, upgrades=False)])
ok("4 base moved upstream -> likely, regardless", w == "likely")
w, _ = host_care.rebuild_helps([dict(base, upgrades=True), dict(base, upgrades=False)])
ok("4 two services, one does not upgrade -> not likely", w == "only-upgrade")
w, _ = host_care.rebuild_helps([dict(base, behind=None, upgrades=True)])
ok("4 upstream unknown but the app upgrades -> likely", w == "likely")
ok("4 nothing open -> nothing to say", host_care.rebuild_helps([dict(base, open=0)]) == ("", ""))

# 5. the card
APP_PY = os.path.join(HERE, "..", "platform", "services", "portal", "app.py")
tree = ast.parse(open(APP_PY, encoding="utf-8").read())
card = None
for n in tree.body:
    if isinstance(n, ast.Assign) and any(getattr(x, "id", "") == "IMAGE_CARD" for x in n.targets):
        card = resolve(tree, n.value)
from jinja2 import Environment  # noqa: E402
CARD = Environment(autoescape=True).from_string(card)
SVC = {"service": "", "image": "oaap-app/package-catalog:0.2.0", "word": "", "state": "open",
       "open": 1, "oldest": "2026-11-07", "base": "python:3.13-slim", "built": "2026-11-09",
       "behind": False, "why": ""}
PK = [{"package": "zlib", "dsa": "DSA-6613-1", "published": "2026-11-07",
       "installed": "1:1.3.dfsg+really1.3.1-1+b1", "fixed": "1:1.3.dfsg+really1.3.1-1+deb13u1"}]


def render(svc, helps=("", "")):
    return CARD.render(i={"key": "x", "imgcare": {
        "known": True, "services": [svc], "state": "open", "word": "offen", "computed": "c",
        "helps": helps[0], "helps_text": helps[1], "rebuild": {}, "self_heal_text": "x",
        "rebuildable": False, "why_not": ""}})


h = render(dict(SVC, pkgs=PK), host_care.rebuild_helps([dict(base, upgrades=False)], de=True))
ok("5 server_admin: package, DSA, date, both versions",
   "<code>zlib</code>" in h and "DSA-6613-1 vom 2026-11-07" in h
   and "really1.3.1-1+b1" in h and "deb13u1" in h, h)
ok("5 and the plain no", "Hilft ein Neubau?" in h and "Nein." in h, h)
h = render(dict(SVC, pkgs=[]))
ok("5 tenant admin (no names in the view): count and date only",
   "zlib" not in h and "DSA" not in h and "1 offene Sicherheitsmeldung" in h, h)
src_p = open(APP_PY, encoding="utf-8").read()
ok("5 the view gives names only to a server_admin",
   "names = _image_names(name) if is_server_admin() else {}" in src_p)

print()
print("ALL PASS (0 failing)" if not fails else f"FAIL ({fails} failing)")
sys.exit(1 if fails else 0)
