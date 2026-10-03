#!/usr/bin/env python3
"""Das Mandanten-Aufbauprofil (RFC-0055), Stufe 1: Kern und Kommandozeile.

Zwei Schichten, beide ohne Docker und ohne Knoten:

1. `services/tenant_build.py` allein, mit einem erfundenen Knoten: Profil
   pruefen (jede Regel mit Koeder), Werte, Reihenfolge, Fortsetzen,
   Abbruch mitten im Schritt, Rueckbau nur des Selbstgemachten.
2. Der echte `appctl.py` im Wegwerf-Datenverzeichnis, durch main() --
   derselbe Weg wie an der Maschine: `oaap tenant build start|continue|
   confirm|rollback`, mit dem echten Mandantenspeicher.

Nicht hier (Stufe 2): Portal-Aktion und API. Die Regel "der Test muss alle
Tueren treffen" gilt dort; heute gibt es genau eine.

Aufruf: python3 test/test_tenant_build.py
"""
import contextlib
import io
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-tenant-build-test-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, os.path.join(HERE, "..", "platform"))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services"))

import appctl as m                                             # noqa: E402
import tenant_build as tb                                      # noqa: E402

m.reload_gateway = lambda: None
m.zone_probe = lambda label: ""
fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:600]}")


def good_profile(**over):
    doc = {
        "profile": tb.PROFILE_FORMAT, "id": "probe", "title": "Probe",
        "params": {"label": {"kind": "label", "required": True},
                   "title": {"kind": "text", "required": True, "max": 40},
                   "color": {"kind": "color", "default": None}},
        "steps": [
            {"id": "tenant", "type": "tenant.create", "label": "{label}",
             "title": "{title}"},
            {"id": "address", "type": "address.ensure"},
            {"id": "face", "type": "tenant.face", "title": "{title}",
             "color_primary": "{color}"},
            {"id": "admin", "type": "manual", "text": "Create the first admin.",
             "done_when": "confirmed"},
            {"id": "backup", "type": "backup.check"},
        ],
    }
    doc.update(over)
    return doc


def problems(doc):
    return tb.profile_problems(doc)


# ================= 1. the pure half, with an invented node ==============

print("=== the profile is judged whole, before anything runs ===")
ok("a good profile has no problems", problems(good_profile()) == [])
d = good_profile()
d["steps"].append({"id": "x", "type": "shell.run", "cmd": "rm -rf /"})
ok("an unknown step type is refused", any("unknown step type" in p
                                          for p in problems(d)))
d = good_profile()
d["steps"][0]["cmd"] = "id"
ok("an argument the step type never promised is refused",
   any("not an argument" in p for p in problems(d)))
d = good_profile()
d["steps"][2]["title"] = "{nope}"
ok("a template naming no parameter is refused",
   any("not a declared parameter" in p for p in problems(d)))
d = good_profile()
d["steps"][1]["id"] = "tenant"
ok("a duplicate step id is refused", any("used twice" in p for p in problems(d)))
d = good_profile()
d["steps"][3]["done_when"] = "whenever"
ok("manual.done_when outside the list of reads is refused",
   any("done_when" in p for p in problems(d)))
d = good_profile()
d["params"].pop("label")
ok("a profile without a label parameter is refused",
   any("parameter 'label'" in p for p in problems(d)))
d = good_profile(profile="oaap.tenant-profile/2")
ok("a wrong format version is refused", any("must say" in p for p in problems(d)))
d = good_profile()
d["steps"][0] = {"id": "tenant", "type": "tenant.create"}
ok("a missing required argument is refused", any("needs 'label'" in p
                                                  for p in problems(d)))
d = good_profile()
d["steps"][1]["timeout"] = 5
ok("an argument of ANOTHER step type is refused", any("not an argument" in p
                                                      for p in problems(d)))

print("=== values are values ===")
prof = good_profile()
vals, pr = tb.param_values(prof, {"label": "vtest", "title": "Verein"})
ok("good parameters pass; an unset optional one is None",
   not pr and vals["color"] is None, pr)
_, pr = tb.param_values(prof, {"label": "vtest"})
ok("a missing required parameter is a problem",
   any("'title' is required" in p for p in pr))
_, pr = tb.param_values(prof, {"label": "vtest", "title": "x", "colour": "#fff"})
ok("a parameter nobody declared is a problem (a typo is not dropped)",
   any("'colour'" in p for p in pr))
_, pr = tb.param_values(prof, {"label": "vtest", "title": "x", "color": "red"})
ok("a colour must be #rrggbb", any("colour" in p for p in pr))
_, pr = tb.param_values(prof, {"label": "vtest", "title": "a" * 41})
ok("text longer than its limit is refused", any("longer than 40" in p for p in pr))
_, pr = tb.param_values(prof, {"label": "vtest", "title": "a\nb"})
ok("control characters in text are refused", any("control" in p for p in pr))
args = tb.render_step(prof["steps"][2], {"label": "v", "title": "T", "color": None})
ok("an unset '{color}' alone drops the key", "color_primary" not in args
   and args["title"] == "T", args)
step = {"id": "s", "type": "tenant.face", "title": "Club {label}"}
try:
    tb.render_step(step, {"label": None})
    refused = False
except tb.Refusal:
    refused = True
ok("an unset value inside a longer text is a refusal", refused)
args = tb.render_step({"id": "s", "type": "tenant.face", "title": "{title}"},
                      {"title": "$(id); `x` {label}"})
ok("a value is never interpreted (it comes out as it went in)",
   args["title"] == "$(id); `x` {label}", args)


class FakeNode:
    """An invented node: things exist in a set, the verbs add and remove."""

    def __init__(self):
        self.things, self.calls, self.audit_log = set(), [], []
        self.fail = {}                 # step id -> sentence, once
        self.refuse_undo = set()
        self.die_after_do = None       # step id: raise SystemExit after doing

    def check(self, typ, a, ctx):
        self.calls.append(("check", typ))
        if typ == "manual":
            return ((tb.PRESENT, "confirmed")
                    if ctx["step"].get("confirmed") else (tb.ABSENT, "waiting"))
        key = f"{typ}:{a.get('label', a.get('name', ''))}"
        return (tb.PRESENT, "there") if key in self.things else (tb.ABSENT, "no")

    def do(self, typ, a, ctx):
        self.calls.append(("do", typ))
        sid = ctx["step"]["id"]
        if sid in self.fail:
            return False, self.fail.pop(sid), []
        key = f"{typ}:{a.get('label', a.get('name', ''))}"
        self.things.add(key)
        if self.die_after_do == sid:
            self.die_after_do = None
            raise SystemExit(2)
        return True, f"did {typ}", [key]

    def undo(self, typ, a, made, ctx):
        self.calls.append(("undo", made[0]))
        if made[0] in self.refuse_undo:
            return False, "it holds something"
        self.things.discard(made[0])
        return True, "undone"

    def audit(self, event, step, result, detail=""):
        self.audit_log.append((event, step, result))


def mk_profile(steps):
    return {"profile": tb.PROFILE_FORMAT, "id": "fake", "title": "",
            "params": {"label": {"kind": "label", "required": True}},
            "steps": steps}


def fake_steps():
    return [{"id": "a", "type": "tenant.create", "label": "{label}"},
            {"id": "b", "type": "app.install", "source": "s", "name": "one"},
            {"id": "c", "type": "app.install", "source": "s", "name": "two"}]


def fresh(node=None):
    root = tempfile.mkdtemp(prefix="oaap-bld-")
    prof = mk_profile(fake_steps())
    vals, _ = tb.param_values(prof, {"label": "pure"})
    return root, prof, tb.new_state(prof, "d" * 64, vals, "tester"), node or FakeNode()


print("=== the order, and a step is done when its CHECK says so ===")
root, prof, st, node = fresh()
st = tb.run(st, prof, node, root)
ok("all steps run in order and the build is done",
   st["state"] == "done" and [r["state"] for r in st["steps"]] == ["done"] * 3, st)
ok("the do of each step is followed by a check",
   node.calls[:3] == [("check", "tenant.create"), ("do", "tenant.create"),
                      ("check", "tenant.create")], node.calls[:4])
ok("what a step made is written in the step",
   st["steps"][0]["made"] == ["tenant.create:pure"], st["steps"][0])
ok("every step left an audit line", len(node.audit_log) >= 4, node.audit_log)

root, prof, st, node = fresh()


class Liar(FakeNode):
    def do(self, typ, a, ctx):
        self.calls.append(("do", typ))
        return True, "said ok, did nothing", []


st = tb.run(st, prof, Liar(), root)
ok("a verb that said ok and left nothing is a FAILED step",
   st["state"] == "failed" and st["steps"][0]["state"] == "failed"
   and "not on the node" in st["steps"][0]["note"], st["steps"][0])

print("=== failure stops; continue does not repeat; resume after a break ===")
root, prof, st, node = fresh()
node.fail["b"] = "the package is not there"
st = tb.run(st, prof, node, root)
ok("a failing step stops the build there",
   st["state"] == "failed" and st["steps"][1]["state"] == "failed"
   and st["steps"][2]["state"] == "pending", st["steps"])
before = [c for c in node.calls if c[0] == "do"]
st = tb.run(st, prof, node, root)
did = [c for c in node.calls if c[0] == "do"][len(before):]
ok("continue finishes the rest and does NOT do step one again",
   st["state"] == "done" and ("do", "tenant.create") not in did, did)

root, prof, st, node = fresh()
node.die_after_do = "b"
try:
    tb.run(st, prof, node, root)
    died = False
except SystemExit:
    died = True
on_disk = tb.load_state(root, st["id"])
ok("a process killed in the middle leaves a state that says so",
   died and on_disk["steps"][1]["state"] == "running"
   and on_disk["state"] == "running", on_disk["steps"][1])
ok("what it made before dying is on disk (a rollback can find it)",
   on_disk["steps"][1]["made"] == [] and "app.install:one" in node.things)
node.claim = lambda typ, a, ctx: [f"{typ}:{a['name']}"] if typ == "app.install" else []
st = tb.run(on_disk, prof, node, root)
ok("the interrupted step's result is claimed: a rollback can now find it",
   st["steps"][1]["made"] == ["app.install:one"], st["steps"][1])
ok("continue after the break finds step two already in place, makes nothing twice",
   st["state"] == "done" and "already in place" in st["steps"][1]["note"]
   and [c for c in node.calls if c == ("do", "app.install")].count(
       ("do", "app.install")) == 2, st["steps"][1])

root, prof, st, node = fresh()
st = tb.run(st, prof, node, root)
node.things.discard("app.install:one")
st["state"] = "running"
st = tb.run(st, prof, node, root)
ok("a finished step that is no longer in place STOPS the build, it is not "
   "silently done over",
   st["state"] == "failed" and st["steps"][1]["state"] == "failed"
   and "no longer in place" in st["steps"][1]["note"], st["steps"][1])

print("=== a manual step waits, and only a read or a human ends it ===")
root = tempfile.mkdtemp(prefix="oaap-bld-")
prof = mk_profile([{"id": "a", "type": "tenant.create", "label": "{label}"},
                   {"id": "m", "type": "manual", "text": "Make the admin.",
                    "done_when": "confirmed"},
                   {"id": "z", "type": "app.install", "source": "s", "name": "x"}])
vals, _ = tb.param_values(prof, {"label": "pure"})
st = tb.new_state(prof, "d" * 64, vals, "tester")
node = FakeNode()
st = tb.run(st, prof, node, root)
ok("the build waits at the manual step and does not go on",
   st["state"] == "waiting" and st["steps"][1]["state"] == "waiting"
   and st["steps"][2]["state"] == "pending", st["steps"])
try:
    tb.confirm(st, "a", root)
    refused = False
except tb.Refusal:
    refused = True
ok("only a manual step can be confirmed", refused)
st = tb.run(st, prof, node, root)
ok("continue alone does not end the wait", st["state"] == "waiting")
tb.confirm(st, "m", root)
st = tb.run(st, prof, node, root)
ok("after the confirmation the build finishes", st["state"] == "done", st)

print("=== rollback undoes only what this build made, backwards ===")
root, prof, st, node = fresh()
node.things.add("app.install:preexisting")
st = tb.run(st, prof, node, root)
node.calls.clear()
st = tb.rollback(st, prof, node, root)
undone = [c[1] for c in node.calls if c[0] == "undo"]
ok("rollback walks the made list in reverse and finishes 'rolled-back'",
   st["state"] == "rolled-back"
   and undone == ["app.install:two", "app.install:one", "tenant.create:pure"],
   undone)
ok("a thing that was there before the build is untouched",
   "app.install:preexisting" in node.things)

root, prof, st, node = fresh()
st = tb.run(st, prof, node, root)
node.refuse_undo.add("tenant.create:pure")
st = tb.rollback(st, prof, node, root)
ok("a refused undo stops the rollback there and says so",
   st["state"] == "failed" and "rollback stopped" in st["steps"][0]["note"]
   and st["steps"][0]["made"] == ["tenant.create:pure"], st["steps"][0])
ok("the steps that were undone are not undone twice, the refused one stays",
   st["steps"][1]["made"] == [] and st["steps"][2]["made"] == [])
node.refuse_undo.clear()
st = tb.rollback(st, prof, node, root)
ok("a later rollback finishes what is left", st["state"] == "rolled-back", st)

print("=== one build per label; a stale lock does not lock for ever ===")
root, prof, st, node = fresh()
tb.save_state(root, st)
ok("an unfinished build of the label is found",
   tb.open_build_for(root, "pure")["id"] == st["id"])
st["state"] = "done"
tb.save_state(root, st)
ok("a finished one is not", tb.open_build_for(root, "pure") is None)
try:
    with tb.RunLock(root, st["id"]):
        with tb.RunLock(root, st["id"]):
            second = True
except tb.Refusal:
    second = False
ok("a second driver of the same build is refused", second is False)
lock = os.path.join(root, st["id"] + ".run")
with open(lock, "w") as f:
    f.write("old")
os.utime(lock, (1, 1))
with tb.RunLock(root, st["id"]):
    took = True
ok("a lock file from long ago is stale and is taken over", took)
if os.name == "posix":
    import subprocess
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    with open(lock, "w") as f:
        f.write(f"{dead.pid} {tb.iso_now()}")
    with tb.RunLock(root, st["id"]):
        took = True
    ok("a lock left by a KILLED process (its pid is gone) is taken over at "
       "once, not after an hour", took)
    with open(lock, "w") as f:
        f.write(f"{os.getpid()} {tb.iso_now()}")
    try:
        with tb.RunLock(root, st["id"]):
            took = True
    except tb.Refusal:
        took = False
    ok("a lock of a LIVE process is still respected", took is False)
    os.remove(lock)
ok("the lock is gone afterwards", not os.path.exists(lock))
try:
    tb.state_path(root, "../../etc/passwd")
    bad = False
except tb.Refusal:
    bad = True
ok("a build id is never a path", bad)


# ============ 2. the real appctl, through main() =========================

print("=== the real node, through main() (the CLI door) ===")
m.os.geteuid = lambda: 0
default_id = m.ensure_default_tenant()
os.makedirs(m.PROFILE_DIR, exist_ok=True)


def write_profile(name, doc):
    with open(os.path.join(m.PROFILE_DIR, name + ".json"), "w",
              encoding="utf-8") as f:
        json.dump(doc, f)


def cli(*argv):
    old = sys.argv
    sys.argv = ["oaap-app", "tenant", "build"] + list(argv)
    buf, code = io.StringIO(), 0
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            m.main()
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    finally:
        sys.argv = old
    return buf.getvalue(), code


def tenant_exists(label):
    return bool(m.tenant_by_label(label, include_former=False)[1])


def real_profile(pid="club"):
    return {
        "profile": tb.PROFILE_FORMAT, "id": pid, "title": "Club",
        "params": {"label": {"kind": "label", "required": True},
                   "title": {"kind": "text", "required": True, "max": 60},
                   "color": {"kind": "color", "default": None}},
        "steps": [
            {"id": "tenant", "type": "tenant.create", "label": "{label}",
             "title": "{title}"},
            {"id": "address", "type": "address.ensure"},
            {"id": "face", "type": "tenant.face", "title": "{title}",
             "color_primary": "{color}"},
            {"id": "admin", "type": "manual", "text": "Create the admin.",
             "done_when": "confirmed"},
            {"id": "backup", "type": "backup.check"},
        ]}


write_profile("club", real_profile())
m.write_users = None
out, code = cli("profiles")
ok("`build profiles` lists the profile", "club" in out and code == 0, out)

out, code = cli("start", "club", "--param", "label=vbuild", "--param",
                "title=Verein Bau", "--dry-run")
ok("a dry run says what it would do and changes nothing",
   code == 0 and "Nothing was changed" in out and "tenant.create" in out
   and not tenant_exists("vbuild"), out)

out, code = cli("start", "club", "--param", "label=vbuild", "--param",
                "title=Verein Bau", "--param", "color=#112233")
ok("start runs up to the waiting step", code == 0 and "WAITING" in out
   and tenant_exists("vbuild"), out)
tid, t = m.tenant_by_label("vbuild")
ok("the tenant has the name and the colour from the profile",
   t.get("name") == "Verein Bau"
   and (t.get("theme") or {}).get("color_primary") == "#112233", t)
builds = tb.list_states(m.BUILD_DIR)
ok("the state file is there with mode 0600 (where the OS has modes)",
   len(builds) == 1 and (os.name == "nt" or oct(os.stat(os.path.join(
       m.BUILD_DIR, builds[0]["id"] + ".json")).st_mode & 0o777) == "0o600"))
bid = builds[0]["id"]
ok("the state has no secret in it", "secret" not in json.dumps(builds[0]).lower())
log = m.read_tenant_log(tid) if hasattr(m, "read_tenant_log") else None
with open(m.TENANT_LOG, encoding="utf-8") as f:
    lines = [json.loads(x) for x in f if x.strip()]
mine = [x for x in lines if x["action"].startswith("tenant.build")]
ok("the build wrote audit lines into the node log AND the new tenant's log",
   any(x["tenant"] == default_id for x in mine)
   and any(x["tenant"] == tid for x in mine), mine[:3])

out, code = cli("start", "club", "--param", "label=vbuild", "--param",
                "title=Zwei")
ok("a second build for the same label is refused while the first is open",
   code != 0 and "not finished" in out, out)

out, code = cli("continue", bid)
ok("continue alone keeps waiting for the human", "WAITING" in out, out)
out, code = cli("confirm", bid, "--step", "admin")
ok("after the confirmation the build is DONE", "DONE" in out and code == 0, out)
out, code = cli("show", bid, "--json")
ok("`show --json` gives the state document",
   json.loads(out)["state"] == "done", out)

print("=== refusals at the CLI door (baits) ===")
out, code = cli("start", "club", "--param", "label=Bad Label!", "--param", "title=x")
ok("a label that breaks the tenant rules is refused before anything is made",
   code != 0 and not tenant_exists("bad label!"), out)
out, code = cli("start", "club", "--param", "label=vcol", "--param", "title=x",
                "--param", "color=red")
ok("a bad colour is refused BEFORE step one makes a tenant",
   code != 0 and not tenant_exists("vcol"), out)
out, code = cli("start", "nosuch", "--param", "label=vnone", "--param", "title=x")
ok("an unknown profile is refused", code != 0, out)
out, code = cli("start", "../etc/passwd", "--param", "label=vnone", "--param",
                "title=x")
ok("a profile name is never a path", code != 0, out)
bad = real_profile("bad")
bad["steps"].append({"id": "evil", "type": "shell.run"})
write_profile("bad", bad)
out, code = cli("start", "bad", "--param", "label=vevil", "--param", "title=x")
ok("a profile with an unknown step type creates NOTHING (judged whole)",
   code != 0 and not tenant_exists("vevil") and "unknown step type" in out, out)
try:
    m.tenant_build_start("club", {"label": "vrole", "title": "x"}, "someone",
                         "tenant_admin")
    refused = False
except tb.Refusal:
    refused = True
ok("the core refuses a role that is not server_admin or root (the one door "
   "all three doors will use)", refused and not tenant_exists("vrole"))
out, code = cli("start", "club", "--param", "label=vext", "--param", "title=x",
                "--param", "extra=1")
ok("an undeclared --param is refused", code != 0 and not tenant_exists("vext"),
   out)
out, code = cli("start", "club", "--param", "nolabel")
ok("--param without key=value is refused", code != 0, out)

print("=== a tenant that was there before is never adopted, never removed ===")
with contextlib.redirect_stdout(io.StringIO()):
    m.cmd_tenant(__import__("argparse").Namespace(
        action="create", name="vold", target=None, title="Alt", account="",
        account_name="", grace_days=30, yes=True, count=50))
out, code = cli("start", "club", "--param", "label=vold", "--param", "title=Neu")
ok("a label already taken is refused at the start, nothing built",
   code != 0 and "already taken" in out
   and not [s for s in tb.list_states(m.BUILD_DIR) if s["label"] == "vold"], out)
drv = m._BuildDrivers("vold")
st_fake = {"steps": []}
verdict, note = drv.check("tenant.create", {"label": "vold"},
                          {"started": "2999-01-01T00:00:00+00:00",
                           "state": st_fake})
ok("the step itself also refuses a tenant it did not make (a race between "
   "the start and the step)", verdict == tb.CONFLICT and "not touched" in note,
   (verdict, note))
verdict, _ = drv.check("tenant.create", {"label": "vold"},
                       {"started": "1999-01-01T00:00:00+00:00",
                        "state": st_fake})
ok("...but a tenant created after the build started is its own",
   verdict == tb.PRESENT)

print("=== rollback of a build that made everything ===")
out, code = cli("start", "club", "--param", "label=vroll", "--param",
                "title=Zurueck")
rb = [s for s in tb.list_states(m.BUILD_DIR) if s["label"] == "vroll"][0]["id"]
ok("the build waits at the manual step", "WAITING" in out and
   tenant_exists("vroll"), out)
out, code = cli("rollback", rb)
ok("a rollback without --yes only says what it would do",
   tenant_exists("vroll") and "Nothing was changed" in out, out)
out, code = cli("rollback", rb, "--yes")
ok("with --yes the empty tenant this build made is gone and the state says "
   "ROLLED-BACK", not tenant_exists("vroll") and "ROLLED-BACK" in out, out)
out, code = cli("start", "club", "--param", "label=vroll", "--param",
                "title=Nochmal")
ok("after a rollback the label can be built again",
   tenant_exists("vroll") and "WAITING" in out, out)

print("=== the profile changed under a running build ===")
out, code = cli("start", "club", "--param", "label=vdig", "--param", "title=Dig")
dig = [s for s in tb.list_states(m.BUILD_DIR) if s["label"] == "vdig"][0]["id"]
changed = real_profile()
changed["title"] = "Club (edited)"
write_profile("club", changed)
out, code = cli("continue", dig)
ok("continue refuses when the profile file differs from the one the build "
   "started with", code != 0 and "digest" in out, out)

print("=== something already gone is not a failed undo ===")
drv = m._BuildDrivers("vnogone")
ok("a tenant that is already gone undoes cleanly",
   drv.undo("tenant.create", {}, ["tenant:x"], {})[0])
ok("a provider of a tenant that is gone undoes cleanly",
   drv.undo("idp.provision", {}, ["provider:x"], {})[0])
ok("an instance of a tenant that is gone undoes cleanly",
   drv.undo("app.install", {}, ["instance:vnogone-web"], {})[0])
print("=== rollback keeps an instance's data unless told otherwise ===")
seen = []
orig_run, orig_inst = m._run_verb, m.tenant_instances
m._run_verb = lambda argv: (seen.append(list(argv)) or (0, "ok"))
m.tenant_instances = lambda reg, tid: {"vroll-web": {}}
d0 = m._BuildDrivers("vroll")
d0.undo("app.install", {}, ["instance:vroll-web"], {})
d1 = m._BuildDrivers("vroll", purge=True)
d1.undo("app.install", {}, ["instance:vroll-web"], {})
m._run_verb, m.tenant_instances = orig_run, orig_inst
ok("without the flag the instance is removed WITHOUT --purge",
   seen[0] == ["remove", "vroll-web"], seen)
ok("with --purge-instances it is removed with --purge",
   seen[1] == ["remove", "vroll-web", "--purge"], seen)
out, code = cli("start", "club", "--param", "label=vhint", "--param", "title=x")
ok("the waiting hint names the right flag for confirm",
   "confirm" in out and "--step" in out, out)

print("")
print("FAIL" if fails else "PASS", fails, "failure(s)")
sys.exit(1 if fails else 0)
