#!/usr/bin/env python3
"""Promotion from a test instance installed from Git (0.1.253).

Anlass (2026-11-11, Jörg): vorgaenge, angelegt durch einen Aufbau, hat
`vorgaenge-test` aus der Git-Quelle des Stores. Der Store lehnt das Update
der Produktiv-Instanz ab ("über die Test-Instanz"), die Test-Instanz bot
keine Übernahme -- Übernahme gab es nur aus einem hochgeladenen Paket.

1. promotion_review liest das Manifest aus GENAU dem festgehaltenen Commit,
   auch wenn der Zweig weitergezogen ist; Version und Rahmen werden gegen
   die Produktiv-Instanz geprüft wie bei einem Paket;
2. eine Git-Instanz ohne festgehaltenen Commit wird abgelehnt, mit dem Weg
   (ein Store-Update der Test-Instanz hält ihn fest);
3. promote_artifact installiert diesen Commit in die Produktiv-Instanz,
   hält Commit, Quelle und Herkunft fest und prüft den Rahmen nicht ein
   zweites Mal (envelope_checked);
4. die Portal-Karte bietet die Übernahme auch für eine Git-Instanz mit
   Commit an.

Run: python3 test/test_promote_git.py   (needs git)
"""
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-promote-git-test-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, os.path.join(HERE, "..", "platform"))

import appctl as m  # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:700]}")


if not shutil.which("git"):
    ok("git is installed (needed)", False)
    sys.exit(1)

repo = tempfile.mkdtemp(prefix="oaap-pg-repo-")
env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@x",
           GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@x")


def g(*a):
    return subprocess.run(["git", *a], cwd=repo, env=env, capture_output=True,
                          text=True, check=True).stdout.strip()


def manifest(version, public=False):
    routes = "  - path: /\n    roles: [user]\n"
    if public:
        routes += "  - path: /pub\n    roles: [public]\n"
    d = os.path.join(repo, "apps", "probe")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "oaap-app.yaml"), "w", encoding="utf-8") as f:
        f.write('oaap_manifest: "0.1"\napp:\n  id: probe\n  name: Probe\n'
                f"  version: {version}\n  type: native\nservices:\n  web:\n"
                "    build: .\n    port: 80\nroutes:\n" + routes
                + "health:\n  path: /healthz\n")
    with open(os.path.join(d, "Dockerfile"), "w") as f:
        f.write("FROM python:3.13-slim\n")


g("init", "-q", "-b", "main")
manifest("0.2.0", public=True)
g("add", "-A")
g("commit", "-q", "-m", "0.2.0")
tested = g("rev-parse", "HEAD")
manifest("0.3.0")
g("commit", "-q", "-am", "0.3.0 -- the branch moved on")
url = "file://" + repo.replace(os.sep, "/")

tid = m.ensure_default_tenant()
reg = m.load_registry()
reg["instances"]["probe-test"] = {
    "app_id": "probe", "version": "0.2.0", "channel": "test", "tenant": tid,
    "name": "probe-test", "routes": [{"path": "/", "roles": ["user"]}],
    "source": {"kind": "git", "url": url, "path": "apps/probe", "ref": "",
               "commit": tested, "store_source": "oaap.platform"}}
reg["instances"]["probe"] = {
    "app_id": "probe", "version": "0.1.0", "channel": "production", "tenant": tid,
    "name": "probe", "routes": [{"path": "/", "roles": ["user"]}],
    "source": {"kind": "git", "url": url, "path": "apps/probe"}}
reg["instances"]["old-test"] = dict(reg["instances"]["probe-test"], name="old-test",
                                    source={"kind": "git", "url": url,
                                            "path": "apps/probe"})
m.save_registry(reg)

# 1. the review reads the recorded commit
path, mf, notes = m.promotion_review(m.load_registry(), "probe-test", "probe")
ok("1 the manifest is the one of the recorded commit (0.2.0), not the branch (0.3.0)",
   mf["app"]["version"] == "0.2.0", mf["app"])
ok("1 the widening against production is named", any("/pub" in n for n in notes), notes)

# 2. no recorded commit
try:
    m.promotion_review(m.load_registry(), "old-test", "probe")
    ok("2 Git without a recorded commit is refused", False)
except m.PromotionRefused as e:
    ok("2 Git without a recorded commit is refused, with the way out",
       "store update of the test instance records it" in str(e), e)

# 3. the promotion installs exactly that commit
seen = {}


def fake_install(pkg, args, source):
    with open(os.path.join(pkg, "oaap-app.yaml"), encoding="utf-8") as f:
        seen["manifest"] = f.read()
    seen["args"], seen["source"] = args, source


m._install_from_dir = fake_install
try:
    m.promote_artifact("probe-test", "probe")
    ok("3 a widening without confirmation is refused", False)
except m.PromotionRefused as e:
    ok("3 a widening without confirmation is refused", "/pub" in str(e), e)
version, sha, notes, key = m.promote_artifact("probe-test", "probe", confirmed=True)
ok("3 confirmed: installs version 0.2.0, says the commit",
   version == "0.2.0" and sha == tested and key == "probe", (version, sha, key))
ok("3 the installed bytes are those of the commit",
   "version: 0.2.0" in seen.get("manifest", ""), seen.get("manifest"))
src = seen.get("source") or {}
ok("3 the production record keeps commit, URL, path, store source and origin",
   src.get("kind") == "git" and src.get("commit") == tested and src.get("url") == url
   and src.get("path") == "apps/probe" and src.get("store_source") == "oaap.platform"
   and src.get("promoted_from") == "probe-test", src)
a = seen.get("args")
ok("3 into production, the envelope is not weighed twice",
   a is not None and a.channel == "production" and a.envelope_checked is True
   and a.key == "probe", vars(a) if a else None)

# 4. the portal offers it
APP_PY = os.path.join(HERE, "..", "platform", "services", "portal", "app.py")
psrc = open(APP_PY, encoding="utf-8").read()
ok("4 the promotion card is offered for a Git instance with a recorded commit",
   'git_commit = (str(src.get("commit") or "") if src.get("kind") == "git" else "")' in psrc
   and "aus Git, Commit" in psrc)

print()
print("ALL PASS (0 failing)" if not fails else f"FAIL ({fails} failing)")
sys.exit(1 if fails else 0)
