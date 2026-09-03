#!/usr/bin/env python3
"""Store sources: objects, migration, reconcile, resolution (RFC-0012).

Covers findings B2 (a source is an object), B3 (resolution by trust
class instead of configured order, plus the confirmation an unverified
source costs) and B4 (a shipped source survives a move).

Runs the real appctl code against a throwaway data directory; store
lists are served from a local http server, so the test needs no network
and cannot drift against a fixture file.

Run: python3 test/test_store_sources.py
"""
import http.server
import json
import io
import contextlib
import os
import shutil
import sys
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-test-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, os.path.join(HERE, "..", "platform"))

import appctl  # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {detail}")


def run(fn, *a, **kw):
    """Call an appctl command, capture output, catch die()."""
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            fn(*a, **kw)
        return True, buf.getvalue()
    except SystemExit:
        return False, buf.getvalue()


class Args:
    def __init__(self, **kw):
        self.action = ""
        self.target = None
        self.value = None
        self.name = None
        self.id = None
        self.origin = ""
        self.trust = None
        self.__dict__.update(kw)


def write_sources(doc):
    os.makedirs(appctl.APPS_DIR, exist_ok=True)
    with open(appctl.STORE_SOURCES, "w", encoding="utf-8") as f:
        json.dump(doc, f)


def read_sources():
    with open(appctl.STORE_SOURCES, encoding="utf-8") as f:
        return json.load(f)


# --------------------------------------------------------------- serve lists
LISTS = {
    "/platform.json": {"store": "0.1", "name": "Plattform", "apps": [
        {"id": "studio", "name": "OAAP Studio", "version": "0.1.0",
         "package": {"git": "https://example.invalid/oaap-apps",
                     "path": "apps/studio"}},
        {"id": "uptime-kuma", "name": "Uptime Kuma (unsere Fassung)",
         "version": "1.23.0",
         "package": {"git": "https://example.invalid/oaap-apps",
                     "path": "apps/uptime-kuma"}}]},
    "/community.json": {"store": "0.1", "name": "Community", "apps": [
        {"id": "uptime-kuma", "name": "Uptime Kuma", "version": "1.23.0",
         "package": {"git": "https://example.invalid/oaap-store",
                     "path": "apps/uptime-kuma"}},
        {"id": "n8n", "name": "n8n", "version": "1.0.0",
         "package": {"git": "https://example.invalid/oaap-store",
                     "path": "apps/n8n"}}]},
    "/foreign.json": {"store": "0.1", "name": "Fremde Liste", "apps": [
        {"id": "studio", "name": "Studio (Übernahmeversuch)", "version": "9.9.9",
         "package": {"git": "https://evil.invalid/takeover"}}]},
}


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        doc = LISTS.get(self.path)
        body = json.dumps(doc or {}).encode()
        self.send_response(200 if doc else 404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_address[1]}"

print("=== migration of an existing node (B4) ===")
# Exactly what a node installed before RFC-0012 carries: the community
# list, as {url, name}, and nothing else.
write_sources({"sources": [
    {"url": "https://raw.githubusercontent.com/MDJoerg/oaap-store/main/oaap-store.json",
     "name": ""}]})
sources, removed, migrated = appctl.load_sources()
ok("old {url,name} entry is migrated in memory", migrated)
s = sources[0]
ok("recognised as the shipped community list", s["id"] == "oaap.community",
   f"id={s['id']}")
ok("trust derived once from the URL prefix", s["trust"] == "verified")
ok("marked as shipped, with the shipped URL recorded",
   s.get("shipped") and s.get("shipped_url") == s["url"])
ok("enabled by default", s["enabled"] is True)

lines = appctl.reconcile_shipped_sources()
after = read_sources()["sources"]
ids = [x["id"] for x in after]
ok("reconcile adds the platform list that was missing",
   "oaap.platform" in ids, str(ids))
ok("reconcile says so in the update transcript",
   any("oaap.platform" in ln for ln in lines), str(lines))
ok("reconcile reports the change of resolution rule",
   any("resolved by trust" in ln for ln in lines), str(lines))
ok("migration reached disk", all(x.get("id") for x in after))
ok("reconcile is idempotent", appctl.reconcile_shipped_sources() == [])

print("\n=== a foreign list from a repository we also ship from ===")
# Found on oaap-test, 2026-08-09: the URL prefix was consulted for every
# entry on every read, so a source the operator added from the same
# repository (here: the community list pinned to a commit) was silently
# marked as one the installation shipped.
pinned = ("https://raw.githubusercontent.com/MDJoerg/oaap-store/"
          "26681d051d61eba96da4d4d616559e4d877b07b0/oaap-store.json")
okr, out = run(appctl.cmd_store, Args(action="add-source", target=pinned,
                                      name="Angeheftet"))
ok("a pinned URL from the same repository can be added", okr, out)
sources, removed, migrated = appctl.load_sources()
mine = next(s for s in sources if s["url"] == pinned)
ok("it is NOT marked as shipped", not mine.get("shipped"), str(mine))
ok("it keeps the unverified class", mine["trust"] == "unverified", mine["trust"])
ok("it keeps its own derived id", mine["id"] != "oaap.community", mine["id"])
ok("and reading it back reports no migration", not migrated)
okr, out = run(appctl.cmd_store, Args(action="remove-source", target=mine["id"]))
ok("removing it is not remembered as a shipped removal",
   okr and "remembered" not in out, out)
ok("so removed_shipped stays empty",
   not read_sources().get("removed_shipped"),
   str(read_sources().get("removed_shipped")))
ok("and the shipped sources are untouched by all this",
   {"oaap.community", "oaap.platform"}
   <= {x["id"] for x in read_sources()["sources"]})

print("\n=== a shipped list moves (B4) ===")
moved = "https://lists.oaap.org/community/oaap-store.json"
real = appctl.SHIPPED_SOURCES[1]["url"]
appctl.SHIPPED_SOURCES[1]["url"] = moved
lines = appctl.reconcile_shipped_sources()
cur = {x["id"]: x for x in read_sources()["sources"]}
ok("untouched shipped URL follows the move",
   cur["oaap.community"]["url"] == moved)
ok("the move is reported", any("moved to" in ln for ln in lines), str(lines))

# now the operator edits it, and it moves again
cur["oaap.community"]["url"] = "https://my-mirror.invalid/list.json"
appctl.save_sources(list(cur.values()), [])
appctl.SHIPPED_SOURCES[1]["url"] = "https://lists.oaap.org/v2/community.json"
lines = appctl.reconcile_shipped_sources()
cur = {x["id"]: x for x in read_sources()["sources"]}
ok("an edited URL is NOT overwritten",
   cur["oaap.community"]["url"] == "https://my-mirror.invalid/list.json")
ok("and the difference is reported",
   any("Left unchanged" in ln for ln in lines), str(lines))
appctl.SHIPPED_SOURCES[1]["url"] = real

print("\n=== removal is remembered (B4) ===")
write_sources({"sources": []})
appctl.reconcile_shipped_sources()
okr, out = run(appctl.cmd_store, Args(action="remove-source", target="oaap.community"))
ok("removing a shipped source is remembered", okr and "remembered" in out, out)
appctl.reconcile_shipped_sources()
ids = [x["id"] for x in read_sources()["sources"]]
ok("update does not bring it back", "oaap.community" not in ids, str(ids))
ok("but the other shipped source is still there", "oaap.platform" in ids)
okr, out = run(appctl.cmd_store, Args(
    action="add-source",
    target="https://raw.githubusercontent.com/MDJoerg/oaap-store/main/oaap-store.json",
    id="oaap.community", trust="verified"))
appctl.reconcile_shipped_sources()
ok("re-adding it by hand clears the tombstone",
   "oaap.community" in [x["id"] for x in read_sources()["sources"]])

print("\n=== trust class rules (RFC-0012 decision 4) ===")
okr, out = run(appctl.cmd_store, Args(action="add-source",
                                      target="https://foo.invalid/list.json",
                                      trust="platform"))
ok("an operator cannot add a source as 'platform'", not okr, out)
okr, out = run(appctl.cmd_store, Args(action="add-source",
                                      target="http://foo.invalid/list.json"))
ok("plain http is refused", not okr, out)
okr, out = run(appctl.cmd_store, Args(action="add-source",
                                      target="https://foo.invalid/list.json"))
ok("a foreign source is added as unverified", okr and "muss bestätigt" in out, out)
new_id = [x for x in read_sources()["sources"]
          if x["url"] == "https://foo.invalid/list.json"][0]["id"]
ok("and gets a readable derived id", new_id.startswith("foo.invalid-"), new_id)
okr, out = run(appctl.cmd_store, Args(action="trust", target=new_id,
                                      value="platform"))
ok("it cannot be raised to 'platform' later", not okr, out)
okr, out = run(appctl.cmd_store, Args(action="trust", target=new_id,
                                      value="verified"))
ok("but it can be raised to 'verified'", okr, out)
okr, out = run(appctl.cmd_store, Args(action="trust", target="oaap.platform",
                                      value="verified"))
ok("a shipped platform source cannot be re-classified", not okr, out)
okr, out = run(appctl.cmd_store, Args(action="disable", target=new_id))
ok("a source can be disabled", okr and
   [x for x in read_sources()["sources"] if x["id"] == new_id][0]["enabled"] is False)

print("\n=== resolution by trust, not by order (B3) ===")
# The takeover setup: the foreign list is configured FIRST and claims
# 'studio' — exactly the case the old first-hit rule lost.
appctl.save_sources([
    {"id": "foreign", "name": "Fremde Liste", "url": BASE + "/foreign.json",
     "trust": "unverified", "enabled": True},
    {"id": "oaap.community", "name": "Community", "url": BASE + "/community.json",
     "trust": "verified", "enabled": True},
    {"id": "oaap.platform", "name": "Plattform", "url": BASE + "/platform.json",
     "trust": "platform", "enabled": True},
], [])

pkg, version, src = appctl._store_lookup("studio")
ok("the platform list wins over a foreign list configured first",
   src["id"] == "oaap.platform" and "evil" not in pkg["url"],
   f"{src['id']} -> {pkg['url']}")
pkg, version, src = appctl._store_lookup("uptime-kuma")
ok("platform beats verified for the same app id", src["id"] == "oaap.platform")
pkg, version, src = appctl._store_lookup("n8n")
ok("an app only the community list has still resolves",
   src["id"] == "oaap.community")
pkg, version, src = appctl._store_lookup("does-not-exist")
ok("an unknown app resolves to nothing", src is None)

pkg, version, src = appctl._store_lookup("uptime-kuma", prefer="oaap.community")
ok("an instance keeps the source it was installed from",
   src["id"] == "oaap.community")
pkg, version, src = appctl._store_lookup("studio", prefer="oaap.community")
ok("but a preferred source that does not list the app is ignored",
   src["id"] == "oaap.platform")
pkg, version, src = appctl._store_lookup("studio", source_id="foreign")
ok("an explicitly named source is honoured", src["id"] == "foreign")
pkg, version, src = appctl._store_lookup("studio", source_id="not-configured")
ok("a source that is not configured resolves to nothing", src is None)

appctl.save_sources([
    {"id": "oaap.platform", "name": "Plattform", "url": BASE + "/platform.json",
     "trust": "platform", "enabled": False},
    {"id": "oaap.community", "name": "Community", "url": BASE + "/community.json",
     "trust": "verified", "enabled": True},
], [])
pkg, version, src = appctl._store_lookup("uptime-kuma")
ok("a disabled source is not resolved from", src["id"] == "oaap.community")

print("\n=== confirmation for unverified sources (B3) ===")
appctl.save_sources([
    {"id": "foreign", "name": "Fremde Liste", "url": BASE + "/foreign.json",
     "trust": "unverified", "enabled": True},
], [])
pkg, version, src = appctl._store_lookup("studio")
ok("an unverified source does resolve (it is not a block)",
   src["id"] == "foreign")
ok("and is flagged as needing a confirmation", src["trust"] == "unverified")

print("\n=== the host-side worker: confirmation and log record (B3) ===")
# Drive the real deploy worker with the real request path; only the two
# things that touch the outside world are stubbed.
installed = {}
appctl.cmd_install = lambda ns: installed.update(
    package=ns.package, path=ns.path, store_source=ns.store_source)
appctl._resolve_revision = lambda src: "deadbee"

QUEUE = os.path.join(appctl.SPOOL_DIR, "queue")


def queue(req):
    os.makedirs(QUEUE, exist_ok=True)
    with open(os.path.join(QUEUE, f"{req['id']}.json"), "w", encoding="utf-8") as f:
        json.dump(req, f)
    run(appctl.cmd_process_deploys, None)
    with open(os.path.join(appctl.SPOOL_DIR, "results", f"{req['id']}.json"),
              encoding="utf-8") as f:
        return json.load(f)


def last_log():
    with open(appctl.DEPLOY_LOG, encoding="utf-8") as f:
        return json.loads(f.read().strip().splitlines()[-1])


res = queue({"id": "r1", "instance": "studio", "action": "install",
             "by": "joerg"})
ok("install from an unverified source without confirmation is refused",
   not res["ok"] and "confirmed explicitly" in res["message"], str(res))
ok("and nothing was installed", not installed)
ok("the refusal names the source in the log",
   last_log().get("source") == "foreign", str(last_log()))
ok("but names nobody as having confirmed it",
   "confirmed_by" not in last_log(), str(last_log()))

res = queue({"id": "r2", "instance": "studio", "action": "install",
             "source_id": "foreign", "confirm_source": "foreign",
             "by": "joerg"})
ok("with the confirmation it installs", res["ok"], str(res))
ok("from the package the SOURCE named, not the request",
   installed.get("package") == "https://evil.invalid/takeover", str(installed))
ok("and the instance remembers the source it came from",
   installed.get("store_source") == "foreign")
rec = last_log()
ok("the log records who confirmed which source, for which app",
   rec.get("source") == "foreign" and rec.get("source_trust") == "unverified"
   and rec.get("confirmed_by") == "joerg" and rec.get("instance") == "studio",
   str(rec))

# An audit trail records the decision, not its outcome: a confirmed
# install that fails afterwards is exactly what one wants to find later.
_stub_install = appctl.cmd_install


def _fails(ns):
    print("ERROR: build failed")
    raise SystemExit(1)


appctl.cmd_install = _fails
res = queue({"id": "r2b", "instance": "studio", "action": "install",
             "source_id": "foreign", "confirm_source": "foreign",
             "by": "lars"})
rec = last_log()
ok("a confirmed install that fails is still recorded as confirmed",
   not res["ok"] and rec.get("confirmed_by") == "lars", f"{res} / {rec}")
appctl.cmd_install = _stub_install

res = queue({"id": "r3", "instance": "studio", "action": "install",
             "source_id": "not-configured", "confirm_source": "not-configured",
             "by": "joerg"})
ok("a request cannot introduce a source of its own",
   not res["ok"] and "not listed in any configured" in res["message"], str(res))

print("\n=== a tenant admin installs from the store (RFC-0025 §8.1) ===")
# Found on the real node oaapx01 on 2026-09-03: `cls-admin` clicked
# "install" on studio and got "app is not listed in any configured store
# source" — from a source the portal had just LISTED the app from.
#
# Since 0.1.58 the worker turns the requested name into the node-wide
# key before it resolves anything: outside the default tenant that is
# `<slug>-<app>`. The store was then asked for an app called
# `cls-studio`, which no list has ever heard of. Nothing was wrong with
# the sources; the question was.
import argparse as _ap  # noqa: E402

appctl.save_sources([
    {"id": "oaap.platform", "name": "Plattform", "url": BASE + "/platform.json",
     "trust": "platform", "enabled": True},
], [])
with contextlib.redirect_stdout(io.StringIO()):
    appctl.cmd_tenant(_ap.Namespace(
        action="create", name="cls", target=None, title="Kunde CLS",
        account="", account_name="", grace_days=30, yes=True, count=50))
CLS = appctl.tenant_by_label("cls")[0]
ident = os.path.join(DATA, "data", "identity")
os.makedirs(ident, exist_ok=True)
with open(os.path.join(ident, "users.json"), "w", encoding="utf-8") as f:
    json.dump([{"username": "cls-admin", "roles": ["tenant_admin"],
                "tenant": CLS, "groups": [], "active": True}], f)
ok("the tenant has a slug, so its keys differ from its names",
   appctl.tenant_slug(CLS) == "cls")

installed.clear()
res = queue({"id": "t1", "instance": "studio", "action": "install",
             "by": "cls-admin"})
ok("a tenant admin can install an app the store lists", res["ok"], str(res))
ok("resolved by app id, never by the key the node files it under",
   installed.get("path") == "apps/studio", str(installed))

print("\n=== sources from the portal go through the same rules (§7) ===")
# The portal's /apps-registry mount is read-only, so a change is queued
# for the host worker — which re-checks everything the CLI checks,
# because the spool is data, not trust.
appctl.save_sources([
    {"id": "oaap.platform", "name": "Plattform", "url": BASE + "/platform.json",
     "trust": "platform", "enabled": True, "shipped": True,
     "shipped_url": BASE + "/platform.json"},
], [])


def portal(op, **fields):
    rid = f"s{len(os.listdir(QUEUE)) if os.path.isdir(QUEUE) else 0}{op}{len(fields)}"
    return queue({"id": rid, "instance": "", "action": "source", "op": op,
                  "by": "joerg", **fields})


res = portal("add", url="http://unsicher.invalid/list.json")
ok("the portal cannot add a plain-http source",
   not res["ok"] and "https://" in res["message"], str(res))
res = portal("add", url="https://neu.invalid/list.json", name="Neue Liste",
             trust="platform")
ok("the portal cannot claim the 'platform' class",
   not res["ok"] and "reserved" in res["message"], str(res))
res = portal("add", url="https://neu.invalid/list.json", name="Neue Liste",
             trust="unverified", origin="Firma X")
ok("a foreign list can be added as unverified", res["ok"], str(res))
added = [s for s in appctl.load_sources()[0] if s["name"] == "Neue Liste"]
ok("and it lands in the file, enabled, with its origin",
   len(added) == 1 and added[0]["enabled"] and added[0]["origin"] == "Firma X",
   str(added))
new_id = added[0]["id"]
res = portal("trust", source_id="oaap.platform", trust="verified")
ok("a shipped platform source cannot be re-classified from the portal",
   not res["ok"] and "not settable" in res["message"], str(res))
res = portal("trust", source_id=new_id, trust="verified")
ok("but a foreign one can be raised to 'verified'", res["ok"], str(res))
ok("and the message says what that costs",
   "without a confirmation" in res["message"], str(res))
res = portal("disable", source_id=new_id)
ok("a source can be switched off from the portal", res["ok"], str(res))
ok("and the store stops reading from it",
   not any(s["id"] == new_id
           for s in appctl.load_sources()[0] if s.get("enabled")))
res = portal("rename", source_id=new_id, name="")
ok("an empty name is refused", not res["ok"], str(res))
res = portal("remove", source_id="gibt-es-nicht")
ok("an unknown source is refused", not res["ok"]
   and "unknown store source" in res["message"], str(res))
res = portal("remove", source_id="oaap.platform")
ok("removing a shipped source works and is remembered",
   res["ok"] and "remembered" in res["message"], str(res))
appctl.reconcile_shipped_sources()
ok("an update does not bring it back",
   "oaap.platform" not in [s["id"] for s in appctl.load_sources()[0]])
res = portal("frisieren", source_id=new_id)
ok("an unknown operation is refused", not res["ok"], str(res))

# --- "not readable" is not "not configured" (2026-09-02) --------------
# The same class of bug `tenant check` had with the user store (0.1.51):
# the source list is 0600 and owned by root, so a non-root reader saw
# an OSError, called it "no sources", and printed "Add one with: sudo
# oaap store add-source" on a node that had two. A reader that cannot
# see its subject has to say so, not pass.
import builtins  # noqa: E402

real_open = builtins.open


def refuse_sources(path, *a, **kw):
    if str(path) == appctl.STORE_SOURCES:
        raise PermissionError(13, "Permission denied")
    return real_open(path, *a, **kw)


builtins.open = refuse_sources
try:
    raised = False
    try:
        appctl.load_sources()
    except appctl.SourcesUnreadable:
        raised = True
    ok("an unreadable source list raises instead of reading as empty", raised)
    survived, out = run(appctl.cmd_store, Args(action="list"))
    ok("'store list' fails loudly and names the way out",
       not survived and "sudo oaap store list" in out, out)
    ok("and never claims there are none",
       "No store sources configured" not in out, out)
finally:
    builtins.open = real_open

ok("a source list that is simply absent still reads as empty",
   (os.remove(appctl.STORE_SOURCES) if os.path.exists(appctl.STORE_SOURCES)
    else None) is None and appctl.load_sources()[0] == [])

srv.shutdown()
shutil.rmtree(DATA, ignore_errors=True)
print(f"\n{'ALL PASS' if not fails else str(fails) + ' FAILURES'}")
sys.exit(1 if fails else 0)
