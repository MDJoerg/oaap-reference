#!/usr/bin/env python3
"""The MQTT broker's node profile and its access control (RFC-0032 D2).

RFC-0032's own build order §3 step 1: `oaap.events.broker` — Mosquitto
as a new, independent node profile `broker`, checked live against
identity for every CONNECT and every topic action, since a broker
carries no local password file and no local ACL file of its own. This
file defends what is provable without Docker or a running node:

    'broker' exists in appctl.py's PROFILES enum and cmd_node's
    add-profile/remove-profile branches start/stop it, the same way
    'store' already does -- and 'exposed' being added or removed
    republishes the raw device port when 'broker' is already held
    (both directions, source-checked).
    docker-compose.yml's 'broker' service carries no 'ports:' of its
    own -- the raw device port lives ONLY in the
    docker-compose.broker-exposed.yml overlay, applied by
    _broker_compose_files() only when 'exposed' is also held.
    The Caddyfile's '/broker/*' route gates the WebSocket path with a
    logged-in session, same as every other protected route, then
    hands off to the broker's own port 9001, never published directly.
    migrate.sh's safety net passes the SAME file set as
    _broker_compose_files(), or a node with both profiles would lose
    the raw port on every update.
    identity's two new routes (/mqtt-auth/getuser, /mqtt-auth/aclcheck)
    accept only an unscoped RFC-0027 machine key whose MQTT username is
    the key's own id, reject a scoped one (twin's own kind), and gate
    every topic to the caller's own 'oaap/<tenant>/...' tree (RFC-0032
    §1.1) -- exercised end-to-end with Flask's test client.

Run: python3 test/test_broker_profile.py
The identity half needs flask + werkzeug (as the service does). If they
are not installed, that section reports SKIP rather than a false PASS.
"""
import ast
import importlib
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PLATFORM_DIR = os.path.join(HERE, "..", "platform")
IDENTITY_DIR = os.path.join(PLATFORM_DIR, "services", "identity")

sys.path.insert(0, PLATFORM_DIR)
import appctl  # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:400]}")


def read(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as f:
        return f.read()


def block(text, start_marker, end_markers):
    """The slice of `text` from `start_marker` up to the first of
    `end_markers` that appears after it -- enough to check one YAML/
    Caddyfile block without a parser, the same technique
    test_launchpad_hints.py uses for Python source slices."""
    i = text.index(start_marker)
    j = min((text.index(m, i + len(start_marker)) for m in end_markers
              if m in text[i + len(start_marker):]), default=len(text))
    return text[i:i + len(start_marker) + j] if j != len(text) else text[i:]


print("appctl.py: the 'broker' node profile (RFC-0011)")

ok("'broker' is a registered profile", "broker" in appctl.PROFILES)
appctl_src = read(PLATFORM_DIR, "appctl.py")
ok("_broker_compose_files() exists",
   "def _broker_compose_files():" in appctl_src)
ok("_broker_compose_files() layers the overlay only with 'exposed'",
   'has_profile("exposed")' in appctl_src.split(
       "def _broker_compose_files():", 1)[1].split("\ndef ", 1)[0])

node_src = appctl_src.split("def cmd_node(args):", 1)[1]
add_half, remove_half = node_src.split("\n    else:\n", 1)
ok("add-profile 'broker' starts the service",
   'if profile == "broker":' in add_half
   and '"broker"' in add_half.split('if profile == "broker":', 1)[1]
       .split("up", 1)[0])
ok("adding 'exposed' republishes the port when 'broker' is already held",
   'if profile == "exposed" and has_profile("broker"):' in add_half)
ok("remove-profile 'broker' stops the service",
   'if profile == "broker":' in remove_half
   and '_compose("stop", "broker"' in remove_half)
ok("removing 'exposed' republishes broker WITHOUT the port",
   'if profile == "exposed" and has_profile("broker"):' in remove_half)

# appctl.py must still be syntactically whole after all the edits above.
ast.parse(appctl_src)


print("")
print("docker-compose.yml / the overlay: the raw port lives only in one place")

compose_src = read(PLATFORM_DIR, "docker-compose.yml")
broker_block = block(compose_src, "\n  broker:\n",
                      ["\n  gateway:", "\n  identity:", "\n  portal:",
                       "\n  store:", "\n  twin:"])
ok("the 'broker' service exists", "broker:" in compose_src)
ok("it carries the 'broker' profile", 'profiles: ["broker"]' in broker_block)
ok("it depends on identity (the auth backend it calls)",
   "depends_on" in broker_block and "identity" in broker_block)
ok("it publishes NO port of its own -- the raw device port is additive",
   "ports:" not in broker_block)

overlay_src = read(PLATFORM_DIR, "docker-compose.broker-exposed.yml")
ok("the overlay targets the 'broker' service", "broker:" in overlay_src)
ok("the overlay publishes 1883, and only 1883",
   '"1883:1883"' in overlay_src)


print("")
print("Caddyfile: the WebSocket path is a protected route, like every other")

caddy_src = read(PLATFORM_DIR, "Caddyfile")
broker_route = block(caddy_src, "handle /broker/* {", ["\n\thandle "])
ok("/broker/* exists", "handle /broker/* {" in caddy_src)
ok("it requires a logged-in session (forward_auth, roles=user)",
   "forward_auth identity:8000" in broker_route
   and "roles=user" in broker_route)
ok("it hands off to the broker's WebSocket port, not the raw one",
   "reverse_proxy broker:9001" in broker_route)


print("")
print("migrate.sh: the safety net passes the same files appctl.py would")

migrate_src = read(PLATFORM_DIR, "migrate.sh")
ok("checks for the 'broker' profile in node.json",
   '\'"broker"\'' in migrate_src)
ok("also checks 'exposed', to decide the same overlay appctl.py would add",
   '\'"exposed"\'' in migrate_src
   and "docker-compose.broker-exposed.yml" in migrate_src)


print("")
print("identity: /mqtt-auth/getuser and /mqtt-auth/aclcheck (RFC-0032 §1.1/D2)")

try:
    import flask  # noqa: F401
except ImportError:
    print("SKIP  flask/werkzeug fehlen -- der Identity-Dienst laesst sich "
          "hier nicht laden.")
    sys.exit(1 if fails else 0)


def load_identity(data_dir, internal_key="test-internal-key"):
    os.environ["SESSION_SECRET"] = "test-session-secret"
    os.environ["SETUP_TOKEN"] = "test-setup-token"
    if internal_key is None:
        os.environ.pop("INTERNAL_API_KEY", None)
    else:
        os.environ["INTERNAL_API_KEY"] = internal_key
    os.environ["OAAP_IDENTITY_DATA_DIR"] = data_dir
    sys.path.insert(0, IDENTITY_DIR)
    sys.modules.pop("app", None)
    m = importlib.reload(importlib.import_module("app"))
    m.USERS_FILE = os.path.join(data_dir, "users.json")
    m.KEYS_FILE = os.path.join(data_dir, "api-keys.json")
    return m


def user(name, roles, kind="human", tenant="", active=True):
    return {"username": name, "display_name": "", "password_hash": "",
            "kind": kind, "roles": roles, "groups": [],
            "tenant": tenant, "active": active, "session_epoch": 0}


def put_users(m, users):
    with open(m.USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(users, f)


# _topic_allowed is pure -- no Flask, no disk -- checked directly first.
DATA = tempfile.mkdtemp(prefix="oaap-broker-test-")
m = load_identity(DATA)

ok("topic == 'oaap/<tenant>' (bare root) is allowed",
   m._topic_allowed("oaap/t-cls", "t-cls"))
ok("topic under 'oaap/<tenant>/...' is allowed",
   m._topic_allowed("oaap/t-cls/Machine/obj1/telemetry", "t-cls"))
ok("a subscription filter 'oaap/<tenant>/#' is allowed",
   m._topic_allowed("oaap/t-cls/#", "t-cls"))
ok("another tenant's tree is refused",
   not m._topic_allowed("oaap/t-other/Machine/obj1/telemetry", "t-cls"))
ok("a bare '#' is refused", not m._topic_allowed("#", "t-cls"))
ok("a prefix-alike ('oaap/t-clsx/...') is refused, not just startswith",
   not m._topic_allowed("oaap/t-clsx/Machine", "t-cls"))
ok("no tenant on the key -> always refused",
   not m._topic_allowed("oaap/anything", ""))

put_users(m, [
    user("relay-1", ["user"], kind="machine", tenant="t-cls"),
])
rec, token = m.issue_key(m.load_users(), "relay-1", ["user"], "",
                          "broker test key", 90, "test-suite")
scoped_rec, scoped_token = m.issue_key(m.load_users(), "relay-1", ["user"],
                                        "oaap.twin", "scoped like twin's",
                                        90, "test-suite")

c = m.app.test_client()


def getuser(username, password, k="test-internal-key"):
    return c.post(f"/mqtt-auth/getuser?k={k}",
                   json={"username": username, "password": password,
                         "clientid": "probe"})


def aclcheck(username, topic, acc=1, k="test-internal-key"):
    return c.post(f"/mqtt-auth/aclcheck?k={k}",
                   json={"username": username, "topic": topic,
                         "clientid": "probe", "acc": acc})


r = getuser(rec["id"], token)
ok("valid unscoped key, matching username/password -> Ok:true",
   r.get_json() == {"Ok": True, "Error": ""}, r.get_json())

r = getuser(rec["id"], token, k="wrong-key")
ok("wrong shared secret in the query string -> denied, not 401/500",
   r.status_code == 200 and r.get_json()["Ok"] is False)

r = getuser(rec["id"], token, k="")
ok("missing shared secret -> denied",
   r.status_code == 200 and r.get_json()["Ok"] is False)

r = getuser("not-the-kid", token)
ok("username that is not the token's own id -> refused",
   r.get_json()["Ok"] is False)

r = getuser(rec["id"], "not-a-real-token")
ok("malformed password (not an oaapk_ token) -> refused",
   r.get_json()["Ok"] is False)

r = getuser(scoped_rec["id"], scoped_token)
ok("an instance-SCOPED key (like twin's own) is refused here",
   r.get_json()["Ok"] is False)

r = aclcheck(rec["id"], "oaap/t-cls/Machine/obj1/telemetry")
ok("aclcheck: own tenant topic -> Ok:true", r.get_json()["Ok"] is True)

r = aclcheck(rec["id"], "oaap/t-other/Machine/obj1/telemetry")
ok("aclcheck: another tenant's topic -> Ok:false", r.get_json()["Ok"] is False)

r = aclcheck(rec["id"], "oaap/t-cls/#", acc=4)
ok("aclcheck: a wildcard subscription within the own tenant -> Ok:true",
   r.get_json()["Ok"] is True)

r = aclcheck("unknown-kid", "oaap/t-cls/x")
ok("aclcheck: unknown key id -> Ok:false", r.get_json()["Ok"] is False)

print()
print("all broker-profile checks passed" if not fails
      else f"{fails} check(s) FAILED")
sys.exit(1 if fails else 0)
