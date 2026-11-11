#!/usr/bin/env python3
"""Bulk runs: "Alle aktualisieren", "Alle getesteten übernehmen", "Alle neu
bauen" (0.1.253, Jörg 2026-11-11: "mit Protokoll und Stopp bei Fehlern").

1. the update plan: test instances first; a production instance with a test
   instance is left out with a sentence (it follows by promotion); up to date
   is not a step; a tenant_admin plans only their tenant;
2. the promotion plan: production whose test instance runs higher; a test
   instance without package or commit is a sentence, not a step;
3. the rebuild plan: only where the card says a rebuild helps, and only what
   can be rebuilt;
4. start writes the job and spawns the unit; one run at a time; stop;
5. the run: every step is an ordinary spool request in the person's name; it
   stops at the first failure, says why, leaves the rest waiting; a widening
   is never confirmed by a run;
6. the portal pages render plan, protocol and list.

Run: python3 test/test_bulk_runs.py   (no Docker, no node)
"""
import ast
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-bulk-test-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, os.path.join(HERE, "..", "platform"))
sys.path.insert(0, HERE)

import appctl as m  # noqa: E402
from template_src import resolve  # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:700]}")


tid = m.ensure_default_tenant()
other = "t-other"
C = "ab" * 20
reg = m.load_registry()
reg["instances"] = {
    "alpha-test": {"app_id": "alpha", "version": "0.1.0", "channel": "test", "tenant": tid,
                   "source": {"kind": "git", "url": "u", "commit": C,
                              "store_source": "oaap.platform"}},
    "alpha": {"app_id": "alpha", "version": "0.1.0", "channel": "production", "tenant": tid,
              "source": {"kind": "git", "url": "u", "store_source": "oaap.platform"}},
    "beta": {"app_id": "beta", "version": "0.1.0", "channel": "production", "tenant": tid,
             "source": {"kind": "git", "url": "u"}},
    "gamma": {"app_id": "gamma", "version": "0.2.0", "channel": "production", "tenant": tid,
              "source": {"kind": "git", "url": "u"}},
    "delta": {"app_id": "delta", "version": "0.1.0", "channel": "production", "tenant": other,
              "source": {"kind": "git", "url": "u"}},
}
m.save_registry(reg)
m.resolve_tenant = lambda t: t
LISTS = [({"id": "oaap.platform", "trust": "platform"}, 0,
          {"alpha": {"id": "alpha", "version": "0.2.0"},
           "beta": {"id": "beta", "version": "0.1.1"},
           "gamma": {"id": "gamma", "version": "0.2.0"},
           "delta": {"id": "delta", "version": "0.3.0"}})]

# 1. update plan
steps, notes = m.bulk_plan("update", "server_admin", None, lists=LISTS)
names = [s["name"] for s in steps]
ok("1 update: the test instance first, then production without a test instance",
   names == ["alpha-test", "beta", "delta"], names)
ok("1 production with a test instance is left out, with the way",
   any("alpha" in n and "Alle getesteten übernehmen" in n for n in notes), notes)
ok("1 up to date is no step", "gamma" not in names)
ok("1 the step carries versions and the source",
   steps[0]["from"] == "0.1.0" and steps[0]["to"] == "0.2.0"
   and steps[0]["source_id"] == "oaap.platform", steps[0])
steps, _ = m.bulk_plan("update", "tenant_admin", other, lists=LISTS)
ok("1 a tenant_admin plans only their tenant", [s["name"] for s in steps] == ["delta"], steps)

# 2. promotion plan
reg = m.load_registry()
reg["instances"]["alpha-test"]["version"] = "0.2.0"
reg["instances"]["beta-test"] = {"app_id": "beta", "version": "0.2.0", "channel": "test",
                                 "tenant": tid, "source": {"kind": "git", "url": "u"}}
m.save_registry(reg)
steps, notes = m.bulk_plan("promote", "server_admin", None)
ok("2 promotion: production whose test instance runs higher",
   [(s["name"], s["source"], s["to"]) for s in steps] == [("alpha", "alpha-test", "0.2.0")],
   steps)
ok("2 a test instance without package or commit is a sentence",
   any("beta" in n and "Store-Update der Test-Instanz" in n for n in notes), notes)

# 3. rebuild plan
facts = {"images": {"instances": {
    "alpha-test": {"services": [{"open": 1, "behind": False, "upgrades": True}]},
    "gamma": {"services": [{"open": 1, "behind": False, "upgrades": False}]},
    "beta": {"services": [{"open": 1, "behind": True}]}}}}
reg = m.load_registry()
for k in ("alpha-test", "gamma", "beta"):
    reg["instances"][k]["services"] = [{"image": f"oaap-app/{k}:1"}]
m.save_registry(reg)
steps, notes = m.bulk_plan("rebuild", "server_admin", None, facts=facts)
ok("3 rebuild: only where a rebuild helps (test first); a Git install without a "
   "commit is a sentence",
   [s["name"] for s in steps] == ["alpha-test"] and any(n.startswith("beta:") for n in notes),
   (steps, notes))

# 4. start / one at a time / stop
m._bulk_store_lists = lambda: [({"id": "oaap.platform", "trust": "platform"}, 0,
                                {"alpha": {"id": "alpha", "version": "0.3.0"},
                                 "beta": {"id": "beta", "version": "0.2.1"},
                                 "delta": {"id": "delta", "version": "0.3.0"}})]
spawned = []
m.BULK_SPAWN = lambda jid: spawned.append(jid)
okk, msg, d = m.bulk_job({"op": "start", "kind": "update"}, "server_admin", None, "anna")
jid = (d or {}).get("job", "")
job = m.bulk_load(jid)
ok("4 start: job written, unit spawned, steps waiting",
   okk and spawned == [jid] and job and job["state"] == "planned"
   and all(s["state"] == "pending" for s in job["steps"]) and job["by"] == "anna", (msg, job))
okk2, msg2, _ = m.bulk_job({"op": "start", "kind": "rebuild"}, "server_admin", None, "anna")
ok("4 one run at a time", not okk2 and "already" in msg2, msg2)
okk3, msg3, _ = m.bulk_job({"op": "start", "kind": "update"}, "user", None, "x")
ok("4 only admins", not okk3, msg3)
okk4, msg4, _ = m.bulk_job({"op": "stop", "job": jid}, "tenant_admin", other, "bert")
ok("4 a tenant_admin cannot stop somebody else's run", not okk4, msg4)
mode = oct(os.stat(m._bulk_path(jid)).st_mode & 0o777)
ok("4 the job file is readable for the portal (0644)",
   os.name == "nt" or mode == "0o644", mode)

# 5. the run
sent = []
answers = [{"ok": True, "message": "installed from store source 'oaap.platform'"},
           {"ok": False, "message": "refused: ...", "needs": "confirm_envelope",
            "notes": ["routes become reachable without login: /pub"]}]


def fake_submit(req, wait):
    sent.append(req)
    return answers[len(sent) - 1] if len(sent) <= len(answers) else {"ok": True}


m.BULK_SUBMIT = fake_submit
out = m.bulk_run(jid, say=lambda *_: None)
st = [s["state"] for s in out["steps"]]
ok("5 the run stops at the first failure, the rest waits",
   out["state"] == "stopped" and st == ["ok", "failed", "pending"], (out["state"], st))
ok("5 the stop says which step and why -- a widening goes to the store, one by one",
   "beta" in out["reason"] and "einzeln im Store bestätigen" in out["reason"]
   and "/pub" in out["reason"], out["reason"])
r0 = sent[0]
ok("5 each step is a store update in the person's name, never confirming",
   r0["action"] == "install" and r0["update"] == "alpha-test" and r0["by"] == "anna"
   and r0["app_id"] == "alpha" and "confirm_envelope" not in r0
   and "confirm_source" not in r0 and r0["via"] == f"bulk {jid}", r0)
ok("5 a promotion step never confirms a widening",
   m._bulk_request({"kind": "promote", "by": "a", "id": "x"},
                   {"name": "alpha", "source": "alpha-test"})["confirmed"] is False)
ok("5 a rebuild step is the card's rebuild",
   m._bulk_request({"kind": "rebuild", "by": "a", "id": "x"},
                   {"instance": "gamma"}) | {"requested": ""} ==
   {"by": "a", "requested": "", "via": "bulk x", "instance": "gamma",
    "action": "rebuild", "op": "start"})
okk5, msg5, _ = m.bulk_job({"op": "start", "kind": "promote"}, "server_admin", None, "anna")
jid2 = m.bulk_running()["id"] if m.bulk_running() else ""
open(m._bulk_path(jid2)[:-5] + ".stop", "w").close()
out2 = m.bulk_run(jid2, say=lambda *_: None)
ok("5 a stop requested before a step: nothing more runs",
   out2["state"] == "stopped" and out2["reason"] == "angehalten von Hand"
   and all(s["state"] == "pending" for s in out2["steps"]), out2)
src = open(m.__file__, encoding="utf-8").read()
ok("5 the worker knows the action", 'elif action == "bulk":' in src)

# 6. portal pages
APP_PY = os.path.join(HERE, "..", "platform", "services", "portal", "app.py")
tree = ast.parse(open(APP_PY, encoding="utf-8").read())
T = {}
for n in tree.body:
    if isinstance(n, ast.Assign):
        for t in n.targets:
            if getattr(t, "id", "") in ("BULK_PLAN_BODY", "BULK_RUN_BODY", "BULK_LIST_BODY",
                                        "BULK_STATE", "BULK_TITLE"):
                T[t.id] = resolve(tree, n.value) if t.id.endswith("BODY") else ast.literal_eval(n.value)
from jinja2 import Environment  # noqa: E402
env = Environment(autoescape=True)
h = env.from_string(T["BULK_PLAN_BODY"]).render(
    kind="update", title="Alle aktualisieren", intro="x", err="",
    steps=[{"instance": "alpha-test", "name": "alpha-test", "channel": "test",
            "from": "0.1.0", "to": "0.2.0"}], notes=["alpha: über die Test-Instanz"])
ok("6 the plan page lists the steps and has one start button",
   "alpha-test" in h and "0.1.0 → 0.2.0" in h and 'action="/store/alle/update"' in h
   and "starten" in h and "Nicht dabei" in h, h[:600])
job = m.bulk_load(jid)
h = env.from_string(T["BULK_RUN_BODY"]).render(job=job, done=1, states=T["BULK_STATE"],
                                                title="Alle aktualisieren — " + jid)
ok("6 the protocol: state, step results, the reason, no refresh once stopped",
   "angehalten" in h and "erledigt" in h and "Angehalten bei" in h
   and 'http-equiv="refresh"' not in h, h[:800])
job["state"] = "running"
h = env.from_string(T["BULK_RUN_BODY"]).render(job=job, done=1, states=T["BULK_STATE"], title="t")
ok("6 while running: it refreshes itself and offers to stop",
   'http-equiv="refresh"' in h and "/stop" in h)

print()
print("ALL PASS (0 failing)" if not fails else f"FAIL ({fails} failing)")
sys.exit(1 if fails else 0)
