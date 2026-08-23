#!/usr/bin/env python3
"""Fleet status rules (RFC-0021, spec oaap.fleet.status 0.1).

Two halves, both without docker and without a node:

* fleet_view (portal, Flask-free): key validation, the instance-row
  whitelist (a source URL with an embedded credential must never reach
  the document), attention derivation, state normalization.
* appctl's key store: issue shows the key once and stores only the
  digest, a duplicate label refuses, revoke removes, issue and revoke
  are audited.

Run: python3 test/test_fleet_status.py
"""
import hashlib
import io
import json
import os
import sys
import tempfile
import types
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-test-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, os.path.join(HERE, "..", "platform"))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services", "portal"))

import appctl as m  # noqa: E402
import fleet_view as fv  # noqa: E402

ok = fail = 0


def check(label, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  PASS  {label}")
    else:
        fail += 1
        print(f"  FAIL  {label} {detail}")


FORBIDDEN = ("url", "digest", "token", "secret", "password", "key")


def leaks(value, path=""):
    """Any forbidden field name anywhere in the document?"""
    found = []
    if isinstance(value, dict):
        for k, v in value.items():
            if any(w in k.lower() for w in FORBIDDEN):
                found.append(f"{path}.{k}")
            found += leaks(v, f"{path}.{k}")
    elif isinstance(value, list):
        for i, v in enumerate(value):
            found += leaks(v, f"{path}[{i}]")
    return found


print("\n-- key validation (fleet_view.valid_key)")
secret = "kAqQ7vXok"
keys = {"fleetview@oaap-demo":
            {"digest": hashlib.sha256(secret.encode()).hexdigest(),
             "created": "2026-08-23T10:00:00Z"},
        "other@somewhere":
            {"digest": hashlib.sha256(b"different").hexdigest(),
             "created": "2026-08-23T10:00:00Z"}}
check("right key returns its label",
      fv.valid_key(secret, keys) == "fleetview@oaap-demo")
check("wrong key returns ''", fv.valid_key("guess", keys) == "")
check("empty key returns ''", fv.valid_key("", keys) == "")
check("no keys stored returns ''", fv.valid_key(secret, {}) == "")
check("entry without digest never matches",
      fv.valid_key(secret, {"broken": {}}) == "")

print("\n-- instance row is a whitelist (never a copy)")
inst = {
    "app_name": "bdt-hub", "version": "0.1.0", "channel": "production",
    "address": "hub.bdt.joomp.de",
    "source": {"type": "git",
               "url": "https://x-access-token:ghp_SECRET@github.com/p/r"},
    "config": {"ROOT_API_KEY": "topsecret"},
    "promoted_from": "",
}
row = fv.instance_row("bdt-hub", inst, "ok")
check("carries the facts",
      row["instance"] == "bdt-hub" and row["version"] == "0.1.0"
      and row["channel"] == "production" and row["state"] == "ok"
      and row["address"] == "hub.bdt.joomp.de")
check("origin is the source TYPE only", row.get("origin") == "git")
check("nothing forbidden leaks", not leaks(row), leaks(row))
check("the credential value appears nowhere",
      "ghp_SECRET" not in json.dumps(row) and "topsecret" not in json.dumps(row))

row = fv.instance_row("x", dict(inst, promoted_from="x-test"), "ok")
check("promoted wins over source type", row.get("origin") == "promoted")
row = fv.instance_row("x", {"app_name": "x"}, "err")
check("'err' is normalized to 'error'", row["state"] == "error")
check("empty address/origin are omitted",
      "address" not in row and "origin" not in row)

print("\n-- attention list")
core = [{"name": "Identity", "state": "ok"},
        {"name": "Deploy-Worker", "state": "error"}]
instances = [{"instance": "a", "state": "ok"},
             {"instance": "b", "state": "error"}]
dns = [{"name": "good.example", "state": "ok"},
       {"name": "drift.example", "state": "warn"},
       {"name": "dead.example", "state": "err"}]
items = fv.attention_items(core, instances, dns, ["b-test"])
kinds = [i["kind"] for i in items]
check("core failure listed", "core_service_down" in kinds)
check("dns drift listed", "dns_drift" in kinds)
check("dns unresolved listed", "dns_unresolved" in kinds)
check("pending confirmation listed",
      {"kind": "confirmation_pending", "instance": "b-test"} in items)
check("unhealthy instance listed",
      {"kind": "instance_unhealthy", "instance": "b"} in items)
check("healthy things are not attention",
      not any(i.get("detail") == "good.example"
              or i.get("instance") == "a" for i in items))
check("a quiet node yields an empty list",
      fv.attention_items([{"name": "Identity", "state": "ok"}],
                         [{"instance": "a", "state": "ok"}], [], []) == [])

print("\n-- published names are a whitelist too (schema 0.2)")
row = fv.name_row({"name": "hub.beispiel.de", "what": "Instanz bdt-hub",
                   "state": "ok", "resolved": "203.0.113.7",
                   "label": "Zeigt hierher"})
check("instance name is parsed out",
      row == {"name": "hub.beispiel.de", "kind": "instance",
              "instance": "bdt-hub", "state": "ok",
              "resolved": "203.0.113.7"})
check("alias and node kinds",
      fv.name_row({"name": "x", "what": "Instanz app1 (Alias)",
                   "state": "warn"})["kind"] == "alias"
      and fv.name_row({"name": "x", "what": "Knoten",
                       "state": "err"}) == {"name": "x", "kind": "node",
                                            "state": "error"})
check("empty resolved is omitted",
      "resolved" not in fv.name_row({"name": "x", "what": "Knoten",
                                     "state": "ok", "resolved": "–"}))

print("\n-- the assembled document")
doc = fv.build_document(node="oaap.joomp.de", version="0.1.41",
                        profiles=["dev"], now_iso="2026-08-23T10:15:00Z",
                        core=core, instances=instances, dns_rows=dns,
                        pending_names=["b-test"], public_ip="203.0.113.7")
check("schema is versioned", doc["schema"] == "oaap.fleet.status/0.2")
check("names and public_ip carried",
      len(doc["names"]) == 3 and doc["public_ip"] == "203.0.113.7")
check("no public_ip -> field absent",
      "public_ip" not in fv.build_document(
          node="n", version="v", profiles=[], now_iso="t", core=[],
          instances=[], dns_rows=None, pending_names=[]))
check("node, version, profiles, time carried",
      doc["node"] == "oaap.joomp.de" and doc["platform_version"] == "0.1.41"
      and doc["profiles"] == ["dev"] and doc["time"] == "2026-08-23T10:15:00Z")
check("core names are lowercased, states normalized",
      {"name": "deploy-worker", "state": "error"} in doc["core"])
check("document is JSON-serializable", bool(json.dumps(doc)))
bad = leaks({k: v for k, v in doc.items() if k != "platform_version"})
check("nothing forbidden anywhere in the document", not bad, bad)

print("\n-- appctl key store (issue / list / revoke)")


def run_fleet(action, label=None):
    args = types.SimpleNamespace(object="key", action=action, label=label)
    out = io.StringIO()
    code = 0
    try:
        with redirect_stdout(out):
            m.cmd_fleet(args)
    except SystemExit as e:
        code = e.code or 0
    return code, out.getvalue()


code, out = run_fleet("issue", "fleetview@oaap-demo")
check("issue succeeds and shows a key once", code == 0 and "ONCE" in out)
shown = [l.strip() for l in out.splitlines()
         if l.startswith("  ") and " " not in l.strip()]
check("a key value was printed", len(shown) == 1 and len(shown[0]) > 30)
stored = json.load(open(m.FLEET_KEYS, encoding="utf-8"))
check("store holds the digest, not the key",
      stored["fleetview@oaap-demo"]["digest"]
      == hashlib.sha256(shown[0].encode()).hexdigest()
      and shown[0] not in json.dumps(stored))
check("the portal-side validator accepts it",
      fv.valid_key(shown[0], stored) == "fleetview@oaap-demo")

code, _ = run_fleet("issue", "fleetview@oaap-demo")
check("duplicate label refuses", code != 0)
code, _ = run_fleet("issue", "bad label!")
check("label with forbidden characters refuses", code != 0)
code, _ = run_fleet("issue")
check("issue without a label refuses", code != 0)

code, out = run_fleet("list")
check("list names the label, never a key value",
      code == 0 and "fleetview@oaap-demo" in out and shown[0] not in out)

code, _ = run_fleet("revoke", "fleetview@oaap-demo")
check("revoke removes the key", code == 0
      and json.load(open(m.FLEET_KEYS, encoding="utf-8")) == {})
code, _ = run_fleet("revoke", "fleetview@oaap-demo")
check("revoking a missing label refuses", code != 0)

log = [json.loads(l) for l in open(m.FLEET_LOG, encoding="utf-8")]
check("issue and revoke are audited",
      [e["event"] for e in log] == ["issue", "revoke"]
      and all(e["label"] == "fleetview@oaap-demo" for e in log)
      and all("when" in e for e in log))
check("the audit log carries no key material",
      shown[0] not in json.dumps(log))

print(f"\n{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
