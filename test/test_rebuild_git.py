#!/usr/bin/env python3
"""Stage 2b of the rebuild (RFC-0071 §15, oaap.apps.runtime 2.31, 0.1.229):

1. what a rebuild builds from -- the retained package, the recorded commit
   of a Git install, a pull for foreign images only; refused for a Git
   install from before the commit was recorded, a path, a missing package;
2. self-heal needs both: bytes in service AND the promise care.rebuild: self;
3. manifest 0.10: care.rebuild is one of two words, care an object;
4. the checkout is exactly the recorded commit, against a real repository
   that has moved on since (the branch is never the fallback); a commit the
   repository no longer has is refused; a credential in a URL is never said;
5. the install records the commit, the record keeps the promise verbatim.

Run: python3 test/test_rebuild_git.py   (needs git)
"""
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-rebuild-git-test-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, os.path.join(HERE, "..", "platform"))

import appctl as m  # noqa: E402

hc = m.host_care
fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:700]}")


BUILT = [{"image": "oaap-app/x:1.0.0"}]
C = "0123456789abcdef" * 2 + "01234567"

# 1. what a rebuild builds from
k, why = hc.rebuild_source({"services": BUILT, "source": {"kind": "artifact", "stored": "1.zip"}})
ok("1 retained package -> artifact", k == "artifact" and not why)
k, why = hc.rebuild_source({"services": BUILT, "source": {"kind": "artifact", "stored": "1.zip"}},
                           package_present=False)
ok("1 the package file is gone -> refused, said so", not k and "missing" in why, why)
k, why = hc.rebuild_source({"services": BUILT, "source": {"kind": "git", "url": "u", "commit": C}})
ok("1 Git with a recorded commit -> git", k == "git", why)
k, why = hc.rebuild_source({"services": BUILT, "source": {"kind": "git", "url": "u"}})
ok("1 Git from before the commit was recorded -> refused, a store update records it",
   not k and "store update" in why, why)
k, why = hc.rebuild_source({"services": BUILT, "source": {"kind": "git", "url": "u", "commit": "abc123"}})
ok("1 a short or malformed commit is not a commit", not k, why)
k, why = hc.rebuild_source({"services": BUILT, "source": {"kind": "local", "url": "/srv/x"}})
ok("1 a path -> refused, names §8.1", not k and "§8.1" in why, why)
k, why = hc.rebuild_source({"services": [{"image": "ollama/ollama:latest"}],
                            "source": {"kind": "git", "url": "u"}})
ok("1 foreign images only -> pull, no source needed", k == "pull", why)
k, _ = hc.rebuild_source({"image": "oaap-app/old:1", "source": {"kind": "git", "url": "u"}})
ok("1 a record from before services[] still counts as built", not k)

# 2. self-heal: both, not one
GIT = {"services": BUILT, "source": {"kind": "git", "url": "u", "commit": C}}
h, why = hc.self_heal(GIT)
ok("2 bytes but no promise -> no self-heal, the promise is named",
   not h and "care.rebuild: self" in why, why)
h, why = hc.self_heal(dict(GIT, care_rebuild="release"))
ok("2 'release' -> no self-heal", not h)
h, why = hc.self_heal(dict(GIT, care_rebuild="self"))
ok("2 bytes and the promise -> self-heal", h and not why, why)
h, why = hc.self_heal({"services": BUILT, "care_rebuild": "self",
                       "source": {"kind": "git", "url": "u"}})
ok("2 the promise without bytes -> no self-heal, the missing commit is the reason",
   not h and "commit" in why, why)
h, _ = hc.self_heal({"services": [{"image": "ollama/ollama:latest"}], "care_rebuild": "self"})
ok("2 foreign images with the promise -> self-heal (a pull)", h)

# 3. manifest 0.10
BASE = {"oaap_manifest": "0.10", "app": {"id": "probe", "name": "P", "version": "1.0.0",
                                         "type": "native"},
        "services": {"web": {"build": ".", "port": 8000}},
        "routes": [{"path": "/", "roles": ["user"]}], "health": {"path": "/health"}}


def errs(**kw):
    """The manifest's errors; validate_manifest dies with them."""
    said = []
    real = m.die
    m.die = lambda msg: (said.append(msg), (_ for _ in ()).throw(SystemExit(1)))
    try:
        m.validate_manifest(dict(BASE, **kw))
    except SystemExit:
        pass
    finally:
        m.die = real
    return [ln.strip(" -") for x in said for ln in x.splitlines()[1:]]


ok("3 care.rebuild: self is valid", not errs(care={"rebuild": "self"}), errs(care={"rebuild": "self"}))
ok("3 care.rebuild: release is valid", not errs(care={"rebuild": "release"}))
ok("3 no care at all is valid", not errs())
ok("3 any other word is an error", any("care.rebuild" in e for e in errs(care={"rebuild": "yes"})))
ok("3 care must be an object", any("care:" in e for e in errs(care="self")))
ok("3 the platform reads manifest 0.10 without the 'newer' note", m.MANIFEST_MINOR == 10)

# 4. the checkout against a real repository
if not shutil.which("git"):
    ok("4 git is installed (needed for this part)", False)
else:
    repo = tempfile.mkdtemp(prefix="oaap-rb-repo-")
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@x",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@x")

    def g(*a, cwd=repo):
        return subprocess.run(["git", *a], cwd=cwd, env=env, capture_output=True,
                              text=True, check=True).stdout.strip()
    g("init", "-q", "-b", "main")
    os.makedirs(os.path.join(repo, "apps", "probe"))
    with open(os.path.join(repo, "apps", "probe", "Dockerfile"), "w") as f:
        f.write("FROM python:3.13-slim\n# first\n")
    g("add", "-A")
    g("commit", "-q", "-m", "first")
    first = g("rev-parse", "HEAD")
    with open(os.path.join(repo, "apps", "probe", "Dockerfile"), "w") as f:
        f.write("FROM python:3.13-slim\n# second -- new code on the branch\n")
    g("commit", "-q", "-am", "second")
    url = "file://" + repo.replace(os.sep, "/")
    tmp = tempfile.mkdtemp(prefix="oaap-rb-co-")
    src = {"kind": "git", "url": url, "path": "apps/probe", "commit": first}
    try:
        pkg = m._git_at_commit(src, tmp)
        text = open(os.path.join(pkg, "Dockerfile")).read()
        ok("4 the checkout is the recorded commit, not the branch that moved on",
           "# first" in text and "second" not in text, text)
        ok("4 ... and the bytes come without .git (its config holds the URL)",
           not os.path.exists(os.path.join(tmp, ".git")))
    except m.RebuildRefused as e:
        ok("4 the checkout is the recorded commit", False, e)
    tmp2 = tempfile.mkdtemp(prefix="oaap-rb-co-")
    try:
        m._git_at_commit(dict(src, commit="f" * 40), tmp2)
        ok("4 a commit the repository does not have is refused", False)
    except m.RebuildRefused as e:
        ok("4 a commit the repository does not have is refused, never the branch",
           "no longer in the repository" in str(e), e)
    tmp3 = tempfile.mkdtemp(prefix="oaap-rb-co-")
    try:
        m._git_at_commit(dict(src, url="https://joerg:geheim@127.0.0.1:9/x.git"), tmp3)
        ok("4 an unreachable source is refused", False)
    except m.RebuildRefused as e:
        ok("4 an unreachable source is refused; the credential is never said",
           "cannot be fetched" in str(e) and "geheim" not in str(e), e)
    ok("4 the redaction itself", m._redact_url("https://a:b@h/x") == "https://***@h/x")
    # _rebuild_package routes a Git instance with a commit to the checkout
    tmp4 = tempfile.mkdtemp(prefix="oaap-rb-co-")
    pkg = m._rebuild_package("probe", {"services": BUILT, "source": src}, tmp4)
    ok("4 _rebuild_package builds a Git instance from its commit",
       os.path.isfile(os.path.join(pkg, "Dockerfile"))
       and "# first" in open(os.path.join(pkg, "Dockerfile")).read())
    try:
        m._rebuild_package("probe", {"services": BUILT, "source": {"kind": "git", "url": url}},
                           tempfile.mkdtemp())
        ok("4 ... and refuses one without", False)
    except m.RebuildRefused as e:
        ok("4 ... and refuses one without, before fetching anything",
           "before the commit was recorded" in str(e), e)
    for d in (repo, tmp, tmp2, tmp3, tmp4):
        shutil.rmtree(d, ignore_errors=True)

# 5. the install records the commit; the record keeps the promise
src = open(os.path.join(HERE, "..", "platform", "appctl.py"), encoding="utf-8").read()
inst_fn = src.split("def cmd_install(args):", 1)[1].split("\ndef ", 1)[0]
clone = inst_fn.split('"git", "clone"', 1)[1].split('elif args.package.lower()', 1)[0]
ok("5 the Git install reads the commit it checked out into source.commit",
   '"rev-parse", "HEAD"' in clone and 'source["commit"]' in clone, clone[:400])
ok("5 the instance record keeps care.rebuild verbatim",
   '"care_rebuild": str((m.get("care") or {}).get("rebuild") or "")' in src)

print()
print("FAILED" if fails else "ALL PASS", f"({fails} failing)")
sys.exit(1 if fails else 0)
