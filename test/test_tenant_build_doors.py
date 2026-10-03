#!/usr/bin/env python3
"""Das Mandanten-Aufbauprofil (RFC-0055), Stufe 2: Betreiber-API und Portal-Aktion.

Dieselben Koeder an allen Tueren. Die Regel (RFC-0055 4) lautet: eine Regel,
die nur an einer Tuer geprueft ist, ist nicht geprueft. Die drei Tueren:

  CLI      `oaap tenant build ...` durch main()
  Aktion   eine Anfrage im Spool, vom Worker (`cmd_process_deploys`) mit der
           Rolle aus dem Benutzerspeicher abgearbeitet -- die Portal-Aktion
  API      `/api/v1/operator/tenant-builds` im Portal, das genau diese
           Anfrage schreibt; Antworten kommen aus der Sicht-Datei des Hosts

Der Worker ist echt (echter Mandantenspeicher, echter Spool, echter
Benutzerspeicher im Wegwerfverzeichnis); Docker, Gateway und Anmeldedienst
sind ersetzt. Der Kern ist derselbe wie in test_tenant_build.py.

Braucht Flask. Aufruf: python3 test/test_tenant_build_doors.py
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-build-doors-test-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, os.path.join(HERE, "..", "platform"))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services"))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services", "portal"))

import appctl as a                                             # noqa: E402
import tenant_build as tb                                      # noqa: E402
import management_api as mg                                    # noqa: E402
from flask import Flask, request                               # noqa: E402

a.reload_gateway = lambda: None
a.zone_probe = lambda label: ""
a.os.geteuid = lambda: 0
fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:600]}")


default_id = a.ensure_default_tenant()
os.makedirs(os.path.dirname(a._identity_users_path()), exist_ok=True)
with open(a._identity_users_path(), "w", encoding="utf-8") as f:
    json.dump([{"username": "betreiber", "roles": ["server_admin"],
                "active": True},
               {"username": "kunde-verwalter", "roles": ["tenant_admin"],
                "active": True, "tenant": default_id},
               {"username": "mitglied", "roles": ["user"], "active": True}], f)

mg.SPOOL_DIR = a.SPOOL_DIR
mg.BUILD_VIEW = a.BUILD_VIEW
for d in ("queue", "claims", "results", "jobs"):
    os.makedirs(os.path.join(a.SPOOL_DIR, d), exist_ok=True)

os.makedirs(a.PROFILE_DIR, exist_ok=True)


def club(pid="club"):
    return {"profile": tb.PROFILE_FORMAT, "id": pid, "title": "Club",
            "params": {"label": {"kind": "label", "required": True},
                       "title": {"kind": "text", "required": True, "max": 60},
                       "color": {"kind": "color", "default": None}},
            "steps": [
                {"id": "tenant", "type": "tenant.create", "label": "{label}",
                 "title": "{title}"},
                {"id": "face", "type": "tenant.face", "title": "{title}",
                 "color_primary": "{color}"},
                {"id": "admin", "type": "manual", "text": "Make the admin.",
                 "done_when": "confirmed"}]}


def write_profile(pid, doc):
    with open(os.path.join(a.PROFILE_DIR, pid + ".json"), "w",
              encoding="utf-8") as f:
        json.dump(doc, f)


write_profile("club", club())
bad = club("evil")
bad["steps"].append({"id": "x", "type": "shell.run"})
write_profile("evil", bad)


def exists(label):
    return bool(a.tenant_by_label(label, include_former=False)[1])


# ---------------------------------------------------------------- the doors
WHO = {"name": "betreiber", "roles": {"server_admin"}, "tenant": None,
       "host": None}
app = Flask(__name__)


def portal_queue(rid, name, payload, wait):
    """What the portal's `_queue_with_id` writes."""
    with open(os.path.join(a.SPOOL_DIR, "queue", rid + ".json"), "w",
              encoding="utf-8") as f:
        json.dump({"id": rid, "instance": name,
                   "by": request.headers.get("X-OAAP-User", "?"),
                   "requested": "now", **payload}, f)


mg.init(app, lambda: WHO["name"], lambda: set(WHO["roles"]),
        lambda: ("server_admin" if "server_admin" in WHO["roles"] else
                 "tenant_admin" if "tenant_admin" in WHO["roles"] else "",
                 WHO["tenant"]),
        lambda host: WHO["host"], portal_queue)
client = app.test_client()
USERS = {"betreiber": {"name": "betreiber", "roles": {"server_admin"},
                       "tenant": None},
         "kunde-verwalter": {"name": "kunde-verwalter",
                             "roles": {"tenant_admin"}, "tenant": default_id}}
API = "/api/v1/operator"
H = {"X-OAAP-API": "1", "X-OAAP-User": "betreiber"}


def work():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        a.cmd_process_deploys(None)
    return buf.getvalue()


def job_result(rid):
    p = os.path.join(a.SPOOL_DIR, "jobs", rid, "result.json")
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except OSError:
        return {}


def via_api(path, body, headers=None, as_user="betreiber", method="post"):
    """-> (http status, result of the job or None). Runs the worker."""
    h = dict(headers or H)
    h["X-OAAP-User"] = as_user
    keep = dict(WHO)
    WHO.update(USERS[as_user])
    try:
        r = getattr(client, method)(API + path, json=body, headers=h)
    finally:
        WHO.clear()
        WHO.update(keep)
    if r.status_code != 202:
        return r.status_code, None, r
    work()
    return 202, job_result(r.json["job"]), r


def via_action(op, args, by="betreiber", extra=None):
    """The portal action: a request in the spool, as the portal would write
    it, worked off by the host. -> result of the job."""
    rid = uuid.uuid4().hex
    jdir = os.path.join(a.SPOOL_DIR, "jobs", rid)
    os.makedirs(jdir)
    with open(os.path.join(jdir, "meta.json"), "w") as f:
        json.dump({"id": rid, "op": op, "by": by, "tenant": None}, f)
    with open(os.path.join(a.SPOOL_DIR, "queue", rid + ".json"), "w",
              encoding="utf-8") as f:
        json.dump({"id": rid, "instance": "", "by": by, "requested": "now",
                   "action": "tenant-build", "op": op, "args": args,
                   **(extra or {})}, f)
    work()
    return job_result(rid)


def cli(*argv):
    old = sys.argv
    sys.argv = ["oaap-app", "tenant", "build"] + list(argv)
    buf, code = io.StringIO(), 0
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            a.main()
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    finally:
        sys.argv = old
    return buf.getvalue(), code


def refused(door, res):
    """Did this door refuse, and did the answer say a refusal?"""
    return res is not None and (res.get("ok") is False if door != "cli"
                                else res[1] != 0)


P = lambda label, title="Titel", **k: {"label": label, "title": title, **k}  # noqa: E731

print("=== API: who may ask, and how ===")
r = client.get(API + "/tenant-builds", headers=H)
ok("server_admin may list builds (empty at first)",
   r.status_code == 200 and r.json == {"builds": []}, r.json)
r = client.get(API + "/tenant-profiles", headers=H)
ok("the profiles come from the view the host wrote",
   r.status_code == 200, r.status_code)
a.tenant_build_view_write()
r = client.get(API + "/tenant-profiles", headers=H)
ids = {p["id"]: p for p in r.json["profiles"]}
ok("the view lists the good profile with its parameters and the refused one "
   "with its problem",
   "label" in ids["club"]["params"] and ids["evil"]["problem"], ids)
for who, roles, tenant in (("kunde-verwalter", {"tenant_admin"}, default_id),
                           ("mitglied", {"user"}, None)):
    WHO.update(name=who, roles=roles, tenant=tenant)
    codes = [client.get(API + "/tenant-builds", headers=H).status_code,
             client.get(API + "/tenant-profiles", headers=H).status_code,
             client.post(API + "/tenant-builds", headers=H,
                         json={"profile": "club", "params": P("vnope")}).status_code,
             client.post(API + "/tenant-builds/b-20260101t000000-x/continue",
                         headers=H, json={}).status_code]
    ok(f"{who} gets 403 on every operator route", codes == [403] * 4, codes)
WHO.update(name="betreiber", roles={"server_admin"}, tenant=None)
r = client.post(API + "/tenant-builds", json={"profile": "club",
                                              "params": P("vnosess")})
ok("a session without X-OAAP-API is refused (a key would not need it)",
   r.status_code == 403 and not os.listdir(os.path.join(a.SPOOL_DIR, "queue")),
   r.status_code)
WHO["host"] = "some-tenant-id"
ok("at a tenant's place the operator routes do not exist (404)",
   client.get(API + "/tenant-builds", headers=H).status_code == 404
   and client.post(API + "/tenant-builds", headers=H, json={
       "profile": "club", "params": P("vplace")}).status_code == 404)
WHO["host"] = None
for label, body in (("no profile", {"params": P("vx")}),
                    ("profile with a slash", {"profile": "../x", "params": P("vx")}),
                    ("params not an object", {"profile": "club", "params": "x"}),
                    ("a param that is a list", {"profile": "club",
                                                "params": {"label": ["a"]}}),
                    ("a boolean param", {"profile": "club",
                                         "params": {"label": True}})):
    r = client.post(API + "/tenant-builds", headers=H, json=body)
    ok(f"a malformed start is 400, nothing queued: {label}",
       r.status_code == 400 and not os.listdir(
           os.path.join(a.SPOOL_DIR, "queue")), r.status_code)
r = client.post(API + "/tenant-builds/nonsense/continue", headers=H, json={})
ok("a build id that is no build id is 404", r.status_code == 404)
r = client.post(API + "/tenant-builds/b-20260101t000000-x/continue",
                headers=H, json={})
ok("a build the view does not know is 404, nothing queued",
   r.status_code == 404 and not os.listdir(
       os.path.join(a.SPOOL_DIR, "queue")))
r = client.post(API + "/tenant-builds/b-20260101t000000-x/teleport",
                headers=H, json={})
ok("an unknown operation is 404", r.status_code == 404)

print("=== the same baits at all three doors ===")
# 1 a label that breaks the tenant rules
out, code = cli("start", "club", "--param", "label=Bad Label!", "--param",
                "title=x")
st, res, _ = via_api("/tenant-builds", {"profile": "club",
                                        "params": P("Bad Label!")})
act = via_action("start", {"profile": "club", "params": P("Bad Label!")})
ok("bad label: refused at CLI, API and action; nothing made",
   code != 0 and res["ok"] is False and act["ok"] is False
   and not exists("bad label!"), (code, res, act))
# 2 a profile with a step type outside the list
out, code = cli("start", "evil", "--param", "label=vevil", "--param", "title=x")
st, res, _ = via_api("/tenant-builds", {"profile": "evil",
                                        "params": P("vevil")})
act = via_action("start", {"profile": "evil", "params": P("vevil")})
ok("unknown step type: refused at all three, NOTHING made (judged whole)",
   code != 0 and res["ok"] is False and act["ok"] is False
   and not exists("vevil") and "unknown step type" in res["message"],
   (code, res, act))
# 3 a bad colour
out, code = cli("start", "club", "--param", "label=vcol", "--param", "title=x",
                "--param", "color=red")
st, res, _ = via_api("/tenant-builds", {"profile": "club",
                                        "params": P("vcol", color="red")})
act = via_action("start", {"profile": "club",
                           "params": P("vcol", color="red")})
ok("bad colour: refused at all three BEFORE step one makes a tenant",
   code != 0 and res["ok"] is False and act["ok"] is False
   and not exists("vcol"), (code, res, act))
# 4 a role that is not server_admin: only the doors that HAVE a role
st, res, r = via_api("/tenant-builds", {"profile": "club",
                                        "params": P("vrole")},
                     as_user="kunde-verwalter")
ok("tenant_admin cannot start a build (the API gate already says no; "
   "nothing queued)", st == 403 and not exists("vrole"), st)
act = via_action("start", {"profile": "club", "params": P("vrole")},
                 by="kunde-verwalter")
ok("...and a request written straight into the spool by a tenant_admin is "
   "refused by the HOST, from the actor's own record",
   act["ok"] is False and "server_admin" in act["message"]
   and not exists("vrole"), act)
act = via_action("start", {"profile": "club", "params": P("vrole")},
                 by="kunde-verwalter",
                 extra={"role": "server_admin", "act_role": "server_admin"})
ok("a request that CLAIMS the role is refused all the same (the spool is "
   "data, not trust)", act["ok"] is False and not exists("vrole"), act)
act = via_action("start", {"profile": "club", "params": P("vrole")},
                 by="geist")
ok("a request naming nobody who exists is refused",
   act["ok"] is False and not exists("vrole"), act)
act = via_action("start", {"profile": "club", "params": P("vrole")},
                 by="mitglied")
ok("a plain member is refused", act["ok"] is False and not exists("vrole"), act)

print("=== a build through the API, end to end ===")
st, res, r = via_api("/tenant-builds", {"profile": "club",
                                        "params": P("vapi", "Verein Api",
                                                    color="#334455")})
ok("start: the worker made the tenant and stopped at the waiting step",
   st == 202 and res["ok"] and "waiting" in res["message"]
   and exists("vapi"), res)
bid = res["build"]
j = client.get(f"/api/v1/tenant/jobs/{r.json['job']}", headers=H).json
ok("the job status names the build", j.get("build") == bid and j["status"] == "done", j)
b = client.get(f"{API}/tenant-builds/{bid}", headers=H).json
ok("the view shows the build with its steps, from the host",
   b["state"] == "waiting" and [s["state"] for s in b["steps"]] == [
       "done", "done", "waiting"], b)
lst = client.get(API + "/tenant-builds", headers=H).json["builds"]
ok("the list names it", any(x["id"] == bid and x["state"] == "waiting"
                            for x in lst), lst)
ok("no secret and no params beyond the profile's in the list rows",
   set(lst[0]) == {"id", "profile", "label", "state", "created", "by"}, lst[0])
st, res, _ = via_api("/tenant-builds", {"profile": "club",
                                        "params": P("vapi", "Zwei")})
ok("a second build for the same label is refused while this one is open",
   res["ok"] is False and "not finished" in res["message"], res)
st, res, _ = via_api(f"/tenant-builds/{bid}/continue", {})
ok("continue alone keeps waiting", res["ok"] and "waiting" in res["message"], res)
st, res, r = via_api(f"/tenant-builds/{bid}/confirm", {})
ok("confirm without a step is 400", st == 400, st)
st, res, _ = via_api(f"/tenant-builds/{bid}/confirm", {"step": "admin"})
ok("confirm ends the wait: the build is done", res["ok"]
   and "is done" in res["message"], res)
b = client.get(f"{API}/tenant-builds/{bid}", headers=H).json
ok("the view follows to 'done'", b["state"] == "done", b["state"])

print("=== rollback and the purge flag through the API ===")
st, res, _ = via_api("/tenant-builds", {"profile": "club",
                                        "params": P("vback", "Zurueck")})
bid2 = res["build"]
st, res, r = via_api(f"/tenant-builds/{bid2}/rollback",
                     {"purge_instances": "yes please"})
ok("rollback through the API removes the empty tenant the build made",
   res["ok"] and not exists("vback"), res)
st, res, _ = via_api(f"/tenant-builds/{bid2}/rollback", {})
ok("a build already rolled back is refused", res["ok"] is False, res)
sent = []
orig = mg.enqueue
mg.enqueue = lambda *aa, **kk: (sent.append((aa, kk)) or orig(*aa, **kk))
via_api(f"/tenant-builds/{bid}/rollback", {"purge_instances": "true"})
via_api(f"/tenant-builds/{bid}/rollback", {"purge_instances": True})
mg.enqueue = orig
flags = [s[0][3].get("purge_instances") for s in sent]
ok("purge_instances is True only for the literal JSON true (a string is not "
   "consent to delete data)", flags == [False, True], flags)

print("=== the digest and the second door of the same rule ===")
st, res, _ = via_api("/tenant-builds", {"profile": "club",
                                        "params": P("vdig", "Dig")})
bid3 = res["build"]
doc = club()
doc["title"] = "edited"
write_profile("club", doc)
st, res, _ = via_api(f"/tenant-builds/{bid3}/continue", {})
act = via_action("continue", {"build": bid3})
out, code = cli("continue", bid3)
ok("a changed profile file is refused at all three doors on continue",
   res["ok"] is False and act["ok"] is False and code != 0
   and "digest" in res["message"], (res, act, code))
write_profile("club", club())
st, res, _ = via_api(f"/tenant-builds/{bid3}/continue", {})
ok("with the file back as it was, continue goes on", res["ok"], res)

print("=== the audit trail ===")
with open(a.TENANT_LOG, encoding="utf-8") as f:
    lines = [json.loads(x) for x in f if x.strip()]
denied = [x for x in lines if x["action"] == "tenant.build.denied"]
ok("refusals of the worker are written to the node's log, with who asked",
   denied and all(x["tenant"] == default_id for x in denied)
   and any(x["who"] == "kunde-verwalter" for x in denied), denied[:2])
steps = [x for x in lines if x["action"] == "tenant.build.step"]
ok("a build through the API left its step lines like the CLI's",
   any(x["who"] == "betreiber" for x in steps), steps[:2])

print("")
print("FAIL" if fails else "PASS", fails, "failure(s)")
sys.exit(1 if fails else 0)
