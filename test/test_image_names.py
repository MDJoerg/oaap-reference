#!/usr/bin/env python3
"""RFC-0130 stage 1 -- the reading, repaired (no node, no Docker):

1. one image name per instance (`oaap-inst/<instance>[-<svc>]:<tag>`), and the
   node still knows the shared `oaap-app/` images of instances built before;
2. a rebuild that pulled its base after upstream was last asked is not
   "behind" -- only a later upstream answer can say so (2026-11-25, oaapx01:
   28 instances "behind" a minute after their rebuild);
3. "unknown" carries one of its reasons, each with its own sentence.

Run: python3 test/test_image_names.py
"""
import os
import sys
import tempfile
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("OAAP_DATA_DIR", tempfile.mkdtemp(prefix="oaap-imgnames-"))
sys.path.insert(0, os.path.join(HERE, "..", "platform"))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services"))

import appctl as m  # noqa: E402
import host_care as hc  # noqa: E402
import images as im  # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    print(("ok    " if cond else "FAIL  ") + label + ("" if cond else f"  -- {detail}"))
    if not cond:
        fails += 1


print("--- 1. one name per instance")
ok("single service", m.instance_image("pxx-chat", "", "0.22.1") == "oaap-inst/pxx-chat:0.22.1")
ok("multi service, build metadata", m.instance_image("pxx-projects", "web", "15.0.11+oaap.1")
   == "oaap-inst/pxx-projects-web:15.0.11_oaap.1")
ok("built: oaap-inst/ and oaap-app/", m.is_built_image("oaap-inst/a:1") and m.is_built_image("oaap-app/a:1"))
ok("foreign is not built", not m.is_built_image("python:3.13-slim") and not m.is_built_image(""))
ok("host care: an instance on its own name is built (rebuild from its source)",
   hc._built({"services": [{"image": "oaap-inst/x:1"}]}))
rows = im.targets({"a": {"services": [{"container": "oaap-app-a", "image": "oaap-inst/a:1",
                                       "service": "", "base": {"ref": "python:3.13-slim",
                                                               "digest": "sha256:aa",
                                                               "pulled": "2026-11-25T10:30:00Z"}}]},
                   "b": {"services": [{"container": "oaap-app-b", "image": "redis:7",
                                       "service": ""}]}})
ok("reading: oaap-inst/ is not foreign, carries base and pulled",
   rows[0]["foreign"] is False and rows[0]["base_ref"] == "python:3.13-slim"
   and rows[0]["pulled"] == "2026-11-25T10:30:00Z", rows[0])
ok("reading: a foreign image stays foreign", rows[1]["foreign"] is True and rows[1]["pulled"] == "")

print("--- 2. pulled after asked")
ok("pulled after the last ask", im.pulled_after_asked("2026-11-25T10:30:00Z", "2026-11-24T12:37:14Z"))
ok("asked after the pull", not im.pulled_after_asked("2026-11-25T10:30:00Z", "2026-11-25T11:00:00Z"))
ok("never pulled", not im.pulled_after_asked("", "2026-11-24T12:37:14Z"))
ok("never asked, but pulled", im.pulled_after_asked("2026-11-25T10:30:00Z", ""))

NOW = datetime(2026, 11, 25, 10, 35, tzinfo=timezone.utc)


def state(pulled, asked, up_digest, open_n=0, why=""):
    row = {"instance": "pxx-chat", "service": "", "container": "oaap-app-pxx-chat",
           "image": "oaap-inst/pxx-chat:0.22.1", "foreign": False,
           "base_ref": "python:3.13-slim", "base_digest": "sha256:7249",
           "built": "2026-11-25T10:30:15Z", "pulled": pulled, "image_id": "sha256:img"}
    img = {"open": open_n, "oldest": ""} if why == "" else {"open": None, "why": why}
    return {"rows": [row], "images": {"sha256:img": img},
            "upstream": {"python:3.13-slim": {"digest": up_digest, "asked": asked}},
            "computed": "2026-11-25T10:35:00Z"}


s = im.summary(state("2026-11-25T10:30:15Z", "2026-11-24T12:37:14Z", "sha256:1b5c"), NOW)
svc = s["instances"]["pxx-chat"]["services"][0]
ok("the oaapx01 case: rebuilt after the last ask -> fresh, not behind",
   svc["state"] == "fresh" and svc["behind"] is False, svc)
s = im.summary(state("2026-11-25T10:30:15Z", "2026-11-25T11:00:00Z", "sha256:9999"),
               datetime(2026, 11, 25, 11, 5, tzinfo=timezone.utc))
svc = s["instances"]["pxx-chat"]["services"][0]
ok("upstream asked AFTER the rebuild and moved -> behind", svc["state"] == "behind", svc)
s = im.summary(state("", "2026-11-24T12:37:14Z", "sha256:1b5c"), NOW)
ok("without a pull record the old rule stays (behind)",
   s["instances"]["pxx-chat"]["services"][0]["state"] == "behind")

print("--- 3. unknown with a reason")
s = im.summary(state("", "2026-11-24T12:37:14Z", "sha256:7249", why="no dpkg in the image"), NOW)
svc = s["instances"]["pxx-chat"]["services"][0]
ok("no dpkg -> unreadable, a sentence", svc["unknown"] == "unreadable" and "Alpine" in svc["unknown_de"], svc)
s = im.summary(state("", "2026-11-24T12:37:14Z", "sha256:7249",
                     why="the image this container runs was removed (a newer build took its tag)"), NOW)
svc = s["instances"]["pxx-chat"]["services"][0]
ok("removed image -> gone, says it is a fault", svc["unknown"] == "gone" and "Fehler" in svc["unknown_de"], svc)
st = state("", "2026-11-24T12:37:14Z", "sha256:7249")
st["rows"][0]["base_digest"] = ""
svc = im.summary(st, NOW)["instances"]["pxx-chat"]["services"][0]
ok("no base recorded -> no-base", svc["unknown"] == "no-base", svc)
s = im.summary(state("", "2026-11-10T00:00:00Z", "sha256:7249"), NOW)
svc = s["instances"]["pxx-chat"]["services"][0]
ok("upstream answer older than a week -> upstream", svc["unknown"] == "upstream", svc)
s = im.summary(state("", "2026-11-24T12:37:14Z", "sha256:7249"), NOW)
ok("a known state carries no reason", s["instances"]["pxx-chat"]["services"][0]["unknown"] == "")

print("FAILED (%d failing)" % fails if fails else "all ok")
sys.exit(1 if fails else 0)
