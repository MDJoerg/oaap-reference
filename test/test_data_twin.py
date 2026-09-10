#!/usr/bin/env python3
"""The twin holds instances; nothing here holds a definition (oaap.data.twin
0.1, RFC-0031 Schritt 3).

This file defends what is provable without Postgres and without the
'twin' container itself -- the same split `test_data_store.py` and
`test_data_model.py` use for their own capabilities:

    The rehearsal-env scrub (§8, D8's stand-in until the schema copy
    exists) is pure and file-based -- no registry, no database -- and
    it is the one place a real leak was possible: a copied
    OAAP_PLATFORM_KEY would let a rehearsal read and write the
    PRODUCTION tenant's live twin data, which RFC-0030 exists to
    prevent. Proven here directly.
    The install hook, the node-profile wiring, the compose service and
    the gateway route are checked by source text, exactly like
    `test_data_model.py` checks `_install_from_dir` -- each is a single
    place, and a duplicate or a missing line is the failure mode this
    guards against, same as the "one reader stayed in the old path"
    class of fund this project keeps finding.
    The service's own module (`platform/services/twin/app.py`) is
    checked for being valid Python (no Flask, no Postgres needed for
    that) and for the security invariant §8 states in words: the
    caller's tenant and origin never come from the request.

What this file CANNOT check, because it would need Postgres AND the
'twin' container running: that a create/read/write against a real
tenant schema actually behaves as RFC-0031 §9 steps 1-3 describe. That
belongs on `oaap-test`, like the other two data capabilities' own live
findings.

Run: python3 test/test_data_twin.py
"""
import ast
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PLATFORM = os.path.join(HERE, "..", "platform")
DATA = tempfile.mkdtemp(prefix="oaap-data-twin-test-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, PLATFORM)

import appctl as m  # noqa: E402

ok_n = fail_n = 0


def ok(label, cond, detail=""):
    global ok_n, fail_n
    if cond:
        ok_n += 1
        print(f"PASS  {label}")
    else:
        fail_n += 1
        print(f"FAIL  {label} {detail}")


def read(*parts):
    with open(os.path.join(PLATFORM, *parts), encoding="utf-8") as f:
        return f.read()


appctl_src = read("appctl.py")
compose_src = read("docker-compose.yml")
caddy_src = read("Caddyfile")
migrate_src = read("migrate.sh")
twin_src = read("services", "twin", "app.py")

print("=== _scrub_rehearsal_env: no production twin credential survives (D8 stand-in) ===")
tid = m.ensure_default_tenant()
ident = {"id": "test-twin-scrub-inst", "tenant": tid}
m.save_env("twin-scrub-test", {
    "OAAP_APP_SECRET": "das-echte-app-geheimnis",
    "OAAP_PLATFORM_KEY": "oaapk_deadbeef_das-echte-twin-schluessel",
    "OAAP_TWIN_URL": "http://oaap-gateway-1/twin",
    "SMTP_PASSWORD": "streng-geheim",
    "SMTP_HOST": "mail.example.org",
}, ident)
dropped = m._scrub_rehearsal_env("twin-scrub-test", ident, {"SMTP_PASSWORD"})
remaining = m.load_env("twin-scrub-test", ident)
ok("OAAP_PLATFORM_KEY does not survive into a rehearsal's copied env",
   "OAAP_PLATFORM_KEY" not in remaining)
ok("OAAP_TWIN_URL goes with it -- a dead key with a live URL helps nobody",
   "OAAP_TWIN_URL" not in remaining)
ok("OAAP_APP_SECRET still scrubbed too (unchanged behaviour)",
   "OAAP_APP_SECRET" not in remaining)
ok("a real manifest secret is still scrubbed and still reported",
   "SMTP_PASSWORD" not in remaining and dropped == ["SMTP_PASSWORD"])
ok("a non-secret manifest key survives untouched",
   remaining.get("SMTP_HOST") == "mail.example.org")
ok("platform-owned keys are dropped WITHOUT being reported -- an operator "
   "is not asked to 'fill in' a machine credential the next install mints",
   "OAAP_PLATFORM_KEY" not in dropped and "OAAP_TWIN_URL" not in dropped
   and "OAAP_APP_SECRET" not in dropped)

print("\n=== RESERVED_ENV: an operator cannot see or edit the twin credential ===")
ok("OAAP_PLATFORM_KEY is platform-owned",
   "OAAP_PLATFORM_KEY" in m.RESERVED_ENV)
ok("OAAP_TWIN_URL is platform-owned",
   "OAAP_TWIN_URL" in m.RESERVED_ENV)

print("\n=== the install hook provisions the twin schema and the E1 key, "
      "exactly once each, never for a rehearsal ===")
install_body = appctl_src.split("def _install_from_dir")[1].split("\ndef ")[0]
ok("the tenant's twin schema is provisioned alongside binding",
   "_twin_ensure_schema(twin_tenant)" in install_body)
ok("...but never for a rehearsal (D8 is not built -- see oaap.data.twin spec §2)",
   "if not rehearsal:\n                    _twin_ensure_schema(twin_tenant)" in install_body)
ok("a node without a working store is told its twin schema was NOT "
   "provisioned either, not just its data-model declarations",
   "NOT provisioned (oaap.data.twin" in install_body)
ok("the E1 key is minted only when the app actually talks to the twin",
   "(contributes or consumes) and not rehearsal and "
   '"OAAP_PLATFORM_KEY" not in env' in install_body)
ok("...idempotent across a redeploy: no re-mint once OAAP_PLATFORM_KEY exists",
   install_body.count('env["OAAP_PLATFORM_KEY"] = _twin_issue_instance_key') == 1)
ok("OAAP_TWIN_URL points through the gateway, never straight at 'twin' -- "
   "an app's own network (RFC-0016) cannot reach 'twin' directly",
   'f"http://{GATEWAY_CONTAINER}/twin"' in install_body)

print("\n=== _twin_issue_instance_key: scoped to the reserved instance "
      "'oaap.twin' (RFC-0027 D5) -- NOT left unscoped ===")
# Found live on oaap-test 2026-09-10 (CURRENT_STATE 125): an unscoped key
# is refused nowhere but by role and tenant, so it authenticated against
# every OTHER app's own route asking only role 'user' in the same
# tenant, not only '/twin/*'. TWIN_KEY_SCOPE names a value app.id's own
# pattern can never produce (it contains a '.'), so nothing this
# platform ever installs can collide with it.
ok("TWIN_KEY_SCOPE is reserved and can never be a real app.id "
   "([a-z0-9][a-z0-9-]{1,38}[a-z0-9] never contains a dot)",
   re.fullmatch(r"[a-z0-9][a-z0-9-]{1,38}[a-z0-9]", m.TWIN_KEY_SCOPE) is None
   and "." in m.TWIN_KEY_SCOPE)
key_fn = appctl_src.split("def _twin_issue_instance_key")[1].split("\ndef ")[0]
ok("the key is issued WITH --instance scoping, to TWIN_KEY_SCOPE -- "
   "not an empty string",
   "issue_key(users, name, ['user'], os.environ['OAAP_T_SCOPE']," in key_fn
   and "OAAP_T_SCOPE" in key_fn)
ok("the scope value handed into the exec environment IS TWIN_KEY_SCOPE, "
   "not some other string",
   '"OAAP_T_SCOPE": TWIN_KEY_SCOPE' in key_fn)
ok("the machine principal is named 'instance:<name>', never the bare name",
   'principal = f"instance:{name}"' in key_fn)

print("\n=== oaap.core.host: 'store' profile carries 'twin' with it, both ways ===")
ok("add-profile starts 'twin' alongside 'store'",
   '_compose("--profile", "store", "up", "-d", "store", "twin")' in appctl_src)
ok("remove-profile stops 'twin' alongside 'store'",
   '_compose("stop", "store", "twin")' in appctl_src)

print("\n=== docker-compose.yml: 'twin' is gated exactly like 'store' ===")
twin_block = compose_src.split("\n  twin:")[1].split("\n\n")[0]
ok("'twin' carries the SAME profile gate as 'store' -- no store, no twin",
   'profiles: ["store"]' in twin_block)
ok("'twin' publishes no port -- reached only through the gateway",
   "ports:" not in twin_block)
ok("'twin' depends on 'store'",
   "- store" in twin_block)
ok("'twin' mounts the registry+secrets directory read-only",
   '/platform-apps:ro' in twin_block)

print("\n=== Caddyfile: '/twin/*' is verified like every other protected "
      "route, scoped to the SAME reserved instance appctl.py issues the "
      "key for ===")
twin_route = caddy_src.split("handle /twin/*")[1].split("\n\thandle")[0]
ok("the route goes through identity's real /verify, not a bespoke check",
   "forward_auth identity:8000" in twin_route and "uri /verify" in twin_route)
ok("the verified principal and roles are handed to the twin service",
   "copy_headers X-OAAP-User X-OAAP-Roles" in twin_route)
ok("the route carries '?instance=' scoped to TWIN_KEY_SCOPE -- an "
   "unscoped key here would authenticate against every other app's "
   "own route asking only role 'user' in the same tenant, not only "
   "this one (found live on oaap-test 2026-09-10, CURRENT_STATE 125)",
   f"instance={m.TWIN_KEY_SCOPE}" in twin_route)
ok("proxies to the 'twin' service, not 'portal' or 'identity'",
   "reverse_proxy twin:8000" in twin_route)

print("\n=== migrate.sh: 'twin' gets its own repair, independent of Postgres's ===")
ok("a profiled node that already has Postgres running still gets 'twin' "
   "checked -- the pg_isready gate above would otherwise skip it forever",
   "oaap-twin-1" in migrate_src and 'up -d twin' in migrate_src)

print("\n=== the twin service module: valid Python, and the security "
      "invariant of §8 in code, not just in the spec ===")
try:
    ast.parse(twin_src)
    ok("services/twin/app.py parses as valid Python", True)
except SyntaxError as e:
    ok("services/twin/app.py parses as valid Python", False, str(e))
ok("a caller must present a MACHINE principal -- 'instance:' prefix checked",
   'user.startswith("instance:")' in twin_src)
ok("tenant and origin come from the RESOLVED instance record, never a "
   "field the request supplies (RFC-0031 §8: 'a request that names a "
   "tenant is rejected' -- restated here for the twin: it never reads one)",
   "request.json" not in twin_src.replace("get_json", "")
   and "tenant" not in "".join(
       l for l in twin_src.splitlines() if "request.args" in l or "body.get" in l))
ok("a group already owned by a different origin refuses the write "
   "('nobody else writes there', RFC-0031 §3.3)",
   'existing["origin"] != origin' in twin_src)
ok("every write records an event -- the outbox RFC-0032 will read (0.1 "
   "writes it; nothing reads it yet, by design)",
   twin_src.count("_record_event(") >= 2)

print(f"\n{ok_n} bestanden, {fail_n} fehlgeschlagen")
print("ALLE PRUEFUNGEN BESTANDEN" if not fail_n else "FEHLGESCHLAGEN")
sys.exit(1 if fail_n else 0)
