#!/usr/bin/env python3
"""Identity's internal API requires the platform key (RFC-0015 A4).

The hole this guards (found 2026-08-11 while answering RFC-0015): the
only protection on identity's /internal/* API was "reachable on the
container network" — and EVERY app instance runs on that same network
(`--network oaap_default`). So any code inside any app container could
POST /internal/users with roles:["server_admin"] and take the platform.

The fix is a shared key held only by the portal. This test drives the
identity app directly, with no running node, and checks all three
outcomes: no key configured -> disabled (503), wrong/absent key -> 401,
correct key -> through to the handler. It also asserts the guard is by
path prefix, so a NEW /internal/* route is covered the day it is added
— that is the exact failure mode (someone adds a route, forgets the
check) this design is meant to survive.

Run: python3 test/test_internal_api_guard.py
Needs flask + werkzeug (as the identity service does). If they are not
installed, the test reports SKIP rather than a false PASS.
"""
import importlib
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
IDENTITY_DIR = os.path.join(HERE, "..", "platform", "services", "identity")

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {detail}")


def load_identity(internal_key):
    """Import services/identity/app.py fresh with a chosen key in env.

    The guard reads INTERNAL_API_KEY at import time (module constant), so
    each scenario needs its own import — clear it from sys.modules first.
    """
    os.environ["SESSION_SECRET"] = "test-session-secret"
    os.environ["SETUP_TOKEN"] = "test-setup-token"
    if internal_key is None:
        os.environ.pop("INTERNAL_API_KEY", None)
    else:
        os.environ["INTERNAL_API_KEY"] = internal_key
    sys.path.insert(0, IDENTITY_DIR)
    for m in ("app",):
        sys.modules.pop(m, None)
    mod = importlib.import_module("app")
    return importlib.reload(mod)


HEADER = "X-OAAP-Internal-Key"
KEY = "the-real-platform-key"

try:
    # --- key configured: absent/wrong key rejected, correct key passes ---
    app = load_identity(KEY).app
    c = app.test_client()

    r = c.get("/internal/users")
    ok("no key header -> 401", r.status_code == 401,
       f"got HTTP {r.status_code}")

    r = c.get("/internal/users", headers={HEADER: "wrong"})
    ok("wrong key -> 401", r.status_code == 401, f"got HTTP {r.status_code}")

    r = c.get("/internal/users", headers={HEADER: KEY})
    ok("correct key -> reaches handler (not 401/503)",
       r.status_code not in (401, 503), f"got HTTP {r.status_code}")

    # the attack itself: create a server_admin from a bare request
    attack = {"username": "zz-probe", "password": "longenough123",
              "roles": ["server_admin"], "groups": []}
    r = c.post("/internal/users", json=attack)
    ok("the escalation POST without the key -> 401",
       r.status_code == 401, f"got HTTP {r.status_code}")

    # a route that does not exist yet, but under /internal/ -> still guarded
    r = c.post("/internal/some-future-route", json={})
    ok("an unknown /internal/* route is guarded too (prefix, not per-route)",
       r.status_code == 401, f"got HTTP {r.status_code}")

    # public surfaces must NOT be caught by the guard
    r = c.get("/auth/login")
    ok("/auth/login is not behind the internal guard",
       r.status_code != 401, f"got HTTP {r.status_code}")

    # --- no key configured: fail closed (disabled), never open ---
    app2 = load_identity(None).app
    c2 = app2.test_client()
    r = c2.get("/internal/users", headers={HEADER: KEY})
    ok("no key on the node -> internal API disabled (503), not open",
       r.status_code == 503, f"got HTTP {r.status_code}")
    r = c2.get("/internal/users")
    ok("no key on the node, no header -> also 503, never 200",
       r.status_code == 503, f"got HTTP {r.status_code}")

except ModuleNotFoundError as e:
    print(f"SKIP  identity dependencies not importable here ({e}).")
    print("      Install flask + werkzeug to run this guard test.")
    sys.exit(0)

print()
print("all guard checks passed" if not fails else f"{fails} check(s) FAILED")
sys.exit(1 if fails else 0)
