#!/usr/bin/env python3
"""The rebuild (RFC-0071 stage 2, 0.1.228) -- executed against a recorded
Docker, no node:

1. only from the retained package: a git/local source with a built service
   is refused before anything is pulled (§8.1); a foreign-only instance
   needs no package (its rebuild is a pull);
2. a held instance is not rebuilt (RFC-0069), the card does not stay "running";
3. the happy path: pull, the running image kept as the predecessor (D2),
   build on the pulled base (§8.2), recreate, health, the base record, the
   audit line with both ids;
4. nothing moved upstream -> nothing recreated;
5. not healthy -> back to the predecessor by itself, the case says so, the
   previous base record comes back (D5);
6. --back by hand;
7. a failing build and a refused pull change nothing;
8. test first (RFC-0050): production waits for its test instance's base;
9. the page's door: another tenant's instance is one the node does not have,
   and the refusal is filed.

Run: python3 test/test_rebuild.py
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-rebuild-test-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, os.path.join(HERE, "..", "platform"))

import appctl as m  # noqa: E402

im = m.images
fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:700]}")


# --- a recorded Docker --------------------------------------------------------
D_OLD, D_NEW = "sha256:" + "1" * 64, "sha256:" + "2" * 64
UP = {"python:3.13-slim": D_NEW, "vaultwarden/server:latest": "sha256:" + "4" * 64}
TAGS = {"python:3.13-slim": D_OLD, "vaultwarden/server:latest": "sha256:" + "3" * 64}
RUNNING = {}
CALLS = []
FAIL = {"pull": set(), "build": False}
UNHEALTHY = set()


def docker_ok(cmd, timeout=900):
    CALLS.append(cmd)
    if cmd[1] == "pull":
        ref = cmd[-1]
        if ref in FAIL["pull"]:
            return False, "denied: requested access to the resource is denied"
        TAGS[ref] = UP.get(ref, TAGS.get(ref))
        return True, ""
    if cmd[1] == "tag":
        src = TAGS.get(cmd[2], cmd[2])
        TAGS[cmd[3]] = src
        return True, ""
    if cmd[1] == "build":
        if FAIL["build"]:
            return False, "ERROR: failed to solve: pip install exit code 1"
        TAGS[cmd[4]] = "sha256:app-on-" + TAGS["python:3.13-slim"][7:15]
        return True, ""
    return True, ""


def inspect_image(ref, run=None):
    iid = TAGS.get(ref) or (ref if ref in TAGS.values() else "")
    if not iid:
        return {}
    repo = im.repo_of(ref) if not ref.startswith("sha256:") else ""
    return {"Id": iid, "RepoDigests": [f"{repo}@{iid}"] if repo and not iid.startswith("sha256:app") else []}


m._docker_ok = docker_ok
im.inspect_image = inspect_image
im.container_image_id = lambda container, image, run=None: RUNNING.get(container, "")


def recreate(name):
    inst = m.load_registry()["instances"][name]
    for s in m.instance_services(inst):
        RUNNING[s["container"]] = TAGS.get(s["image"], "")
    return all(RUNNING[s["container"]] not in UNHEALTHY for s in m.instance_services(inst))


m._rebuild_recreate_and_wait = recreate
PKG = tempfile.mkdtemp()
open(os.path.join(PKG, "Dockerfile"), "w").write("FROM python:3.13-slim\nCOPY . /app\n")
m._rebuild_package = lambda name, inst, tmp: PKG if (inst.get("source") or {}).get("kind") == "artifact" else None
AUDIT = []
m.audit_tenant = lambda action, tid, subject="", result="ok", **kw: AUDIT.append(
    (action, subject, result, kw.get("who"), kw.get("detail", "")))

os.makedirs(m.APPS_DIR, exist_ok=True)
DEFAULT = m.ensure_default_tenant()
PZ, _ = m.tenant_create("pz")
OTHER, _ = m.tenant_create("other")


def svc(name, image, base=None):
    s = {"service": "", "container": f"oaap-app-{name}", "image": image, "build": "." if
         image.startswith("oaap-app/") else "", "port": 8000}
    if base:
        s["base"] = base
    return [s]


APP_OLD = "sha256:app-on-" + D_OLD[7:15]
REG = {"instances": {
    "wegweiser": {"app": "wegweiser", "channel": "production", "tenant": PZ,
                  "source": {"kind": "artifact", "stored": "0.4.1-x.zip"},
                  "services": svc("wegweiser", "oaap-app/wegweiser:0.4.1"), "port": 8101},
    "studio": {"app": "studio", "channel": "test", "tenant": PZ,
               "source": {"kind": "git", "url": "https://x", "path": "apps/studio"},
               "services": svc("studio", "oaap-app/studio:0.4.3"), "port": 8102},
    "vaultwarden": {"app": "vaultwarden", "channel": "production", "tenant": PZ,
                    "source": {"kind": "git", "url": "https://x", "path": "apps/vw"},
                    "services": svc("vaultwarden", "vaultwarden/server:latest"), "port": 8103},
    "hub": {"app": "hub", "channel": "production", "tenant": PZ,
            "source": {"kind": "artifact", "stored": "1.zip"},
            "services": svc("hub", "oaap-app/hub:1"), "port": 8104},
    "hub-test": {"app": "hub", "channel": "test", "tenant": PZ,
                 "source": {"kind": "artifact", "stored": "1.zip"},
                 "services": svc("hub-test", "oaap-app/hub:1"), "port": 8105},
}, "retained": {}}
m.save_registry(REG)
for n, i in REG["instances"].items():
    TAGS[i["services"][0]["image"]] = APP_OLD if i["services"][0]["image"].startswith("oaap-app/") \
        else TAGS["vaultwarden/server:latest"]
    RUNNING[i["services"][0]["container"]] = TAGS[i["services"][0]["image"]]


def inst(name):
    return m.load_registry()["instances"][name]


def refused(fn):
    try:
        fn()
    except m.RebuildRefused as e:
        return str(e)
    return None


# 1. the package rule
CALLS.clear()
why = refused(lambda: m.rebuild_instance("studio", say=lambda *_: None))
ok("1 a git source with a built service: refused, the reason names §8.1",
   why and "retained package" in why and "§8.1" in why, why)
ok("1 ... before anything was pulled", not any(c[1] == "pull" for c in CALLS), CALLS)
ok("1 ... and the card does not stay 'running'", not (inst("studio").get("rebuild") or {}).get("state"))

# 2. protection
from services import host_care  # noqa: E402
prot = host_care.load_protections(m.APPS_DIR)
host_care.protect(prot, "wegweiser", "Schulung bis Freitag", "anna", PZ)
host_care.save_protections(prot, m.APPS_DIR)
why = refused(lambda: m.rebuild_instance("wegweiser", say=lambda *_: None))
ok("2 a held instance is not rebuilt; the hold is named", why and "Schulung" in why, why)
host_care.save_protections({}, m.APPS_DIR)

# 3. happy path
AUDIT.clear()
okk, msg = m.rebuild_instance("wegweiser", who="anna", role="tenant_admin", say=lambda *_: None)
w = inst("wegweiser")
ok("3 rebuilt and answering", okk and "rebuilt" in msg, msg)
ok("3 the running container is on the new image", RUNNING["oaap-app-wegweiser"] == "sha256:app-on-" + D_NEW[7:15])
ok("3 the predecessor is the image that ran before (D2)", TAGS.get(m._prev_tag("wegweiser", "")) == APP_OLD)
ok("3 the base record: the pulled reference and digest (§8.2)",
   w["services"][0]["base"]["ref"] == "python:3.13-slim" and w["services"][0]["base"]["digest"] == D_NEW,
   w["services"][0].get("base"))
ok("3 the state for the card", w["rebuild"]["state"] == "done" and w["rebuild"]["by"] == "anna")
ok("3 one audit line with both ids", len(AUDIT) == 1 and AUDIT[0][2] == "ok"
   and "-> app-on-2" in AUDIT[0][4], AUDIT)

# 4. unchanged
okk, msg = m.rebuild_instance("wegweiser", say=lambda *_: None)
ok("4 upstream did not move: nothing to rebuild on", okk and "not moved" in msg
   and inst("wegweiser")["rebuild"]["state"] == "unchanged", msg)

# 5. fallback
TAGS["python:3.13-slim"] = D_OLD                 # the node holds the old base again ...
UP["python:3.13-slim"] = "sha256:" + "5" * 64    # ... and upstream moved once more
NEW5 = "sha256:app-on-" + "5" * 8
UNHEALTHY.add(NEW5)
before = RUNNING["oaap-app-wegweiser"]
AUDIT.clear()
okk, msg = m.rebuild_instance("wegweiser", say=lambda *_: None)
w = inst("wegweiser")
ok("5 not answering: back to the predecessor by itself", not okk
   and RUNNING["oaap-app-wegweiser"] == before and "previous image" in msg, (msg, RUNNING))
ok("5 the tag points to the previous image again", TAGS["oaap-app/wegweiser:0.4.1"] == before)
ok("5 the base record of the previous image came back", w["services"][0]["base"]["digest"] == D_NEW,
   w["services"][0]["base"])
ok("5 the case says so", w["rebuild"]["state"] == "fallback" and AUDIT[-1][2] == "failed"
   and "previous image" in AUDIT[-1][4], (w["rebuild"], AUDIT))
UNHEALTHY.clear()

# 6. --back
okk, _ = m.rebuild_instance("hub-test", say=lambda *_: None)          # on base 5
okk2, msg = m.rebuild_instance("hub-test", back=True, say=lambda *_: None)
ok("6 --back: the predecessor runs again", okk and okk2 and RUNNING["oaap-app-hub-test"] == APP_OLD
   and inst("hub-test")["rebuild"]["state"] == "back", (msg, RUNNING["oaap-app-hub-test"]))
why = refused(lambda: m.rebuild_instance("hub", back=True, say=lambda *_: None))
ok("6 --back without a predecessor: refused", why and "no previous image" in why, why)

# 7. failing build, refused pull
UP["python:3.13-slim"] = "sha256:" + "6" * 64
FAIL["build"] = True
before = RUNNING["oaap-app-hub-test"]
okk, msg = m.rebuild_instance("hub-test", say=lambda *_: None)
ok("7 a failing build: nothing changed, said", not okk and RUNNING["oaap-app-hub-test"] == before
   and TAGS["oaap-app/hub:1"] == before and inst("hub-test")["rebuild"]["state"] == "build-failed", msg)
FAIL["build"] = False
FAIL["pull"].add("python:3.13-slim")
st = inst("hub-test")["rebuild"]
why = refused(lambda: m.rebuild_instance("hub-test", say=lambda *_: None))
ok("7 a refused pull: refused, nothing changed, the card keeps its last state",
   why and "nothing changed" in why and inst("hub-test")["rebuild"] == st, why)
FAIL["pull"].clear()

# 8. test first
okk, _ = m.rebuild_instance("hub-test", say=lambda *_: None)            # test on base 6
UP["python:3.13-slim"] = "sha256:" + "7" * 64                           # upstream moves
why = refused(lambda: m.rebuild_instance("hub", say=lambda *_: None))
ok("8 production waits: its test instance stands on another base", why and "test first" in why
   and "hub-test" in why, why)
okk, _ = m.rebuild_instance("hub-test", say=lambda *_: None)            # test on base 7
okk2, msg = m.rebuild_instance("hub", say=lambda *_: None)
ok("8 after the test instance: production follows", okk and okk2, msg)

# 1b. foreign only
okk, msg = m.rebuild_instance("vaultwarden", say=lambda *_: None)
ok("1b a foreign image from a git source: the rebuild is a pull, no package needed",
   okk and RUNNING["oaap-app-vaultwarden"] == UP["vaultwarden/server:latest"], msg)

# 9. the page's door
AUDIT.clear()
r = m.rebuild_job({"op": "start"}, "wegweiser", inst("wegweiser"), OTHER, "tenant_admin", "eve")
ok("9 another tenant's instance: answered as one the node does not have, filed",
   not r[0] and r[1] == m.cross_tenant_refusal("rebuild", "wegweiser")
   and AUDIT and AUDIT[-1][2] == "denied" and AUDIT[-1][3] == "eve", (r, AUDIT))
r = m.rebuild_job({"op": "start"}, "wegweiser", inst("wegweiser"), PZ, "user", "bob")
ok("9 a plain user may not", not r[0] and "tenant admin" in r[1], r)
src = open(os.path.join(HERE, "..", "platform", "appctl.py"), encoding="utf-8").read()
cli = src.split("def cmd_rebuild", 1)[1].split("\ndef ", 1)[0]
job = src.split("def rebuild_job", 1)[1].split("\ndef ", 1)[0]
ok("9 two doors, one function", "rebuild_instance(" in cli and "rebuild_instance(" in job)

print()
print("FAILED" if fails else "ALL PASS", f"({fails} failing)")
sys.exit(1 if fails else 0)
