#!/usr/bin/env python3
"""Artifact deployment: the guards and the rules (RFC-0019).

Covers what makes an uploaded package acceptable at all: safe
extraction (§5), the envelope rule (§3), single-use grants, and the
binding between the announcement and the upload (§2) — without which
phase 1 would be theatre.

Needs no docker and no node: everything here refuses BEFORE anything is
built, which is the point of the three-phase exchange.

Run: python3 test/test_artifact_deploy.py
"""
import hashlib
import os
import sys
import tempfile
import time
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-test-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, os.path.join(HERE, "..", "platform"))

import appctl as m  # noqa: E402
import yaml  # noqa: E402

ok = fail = 0


def check(label, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  PASS  {label}")
    else:
        fail += 1
        print(f"  FAIL  {label} {detail}")


def zip_with(entries, path):
    with zipfile.ZipFile(path, "w") as z:
        for name, data in entries.items():
            z.writestr(name, data)
    return path


MANIFEST = """oaap_manifest: "0.1"
app:
  id: demo
  name: Demo
  version: 0.1.0
  type: native
services:
  web:
    build: .
    port: 80
routes:
  - path: /
    roles: [user]
health:
  path: /healthz
"""

print("\n-- safe extraction (RFC-0019 §5)")
tmp = tempfile.mkdtemp()

p = zip_with({"oaap-app.yaml": MANIFEST}, os.path.join(tmp, "plain.zip"))
d = tempfile.mkdtemp()
try:
    m.extract_artifact(p, d)
    check("plain archive unpacks", os.path.isfile(os.path.join(d, "oaap-app.yaml")))
except Exception as e:
    check("plain archive unpacks", False, e)

p = zip_with({"../escape.txt": "x"}, os.path.join(tmp, "slip.zip"))
try:
    m.extract_artifact(p, tempfile.mkdtemp())
    check("path traversal refused", False, "was accepted")
except m.ArtifactRejected as e:
    check("path traversal refused", "escapes" in str(e), e)

p = os.path.join(tmp, "abs.zip")
with zipfile.ZipFile(p, "w") as z:
    z.writestr("/etc/passwd", "x")
try:
    m.extract_artifact(p, tempfile.mkdtemp())
    check("absolute path refused", False, "was accepted")
except m.ArtifactRejected as e:
    check("absolute path refused", "absolute" in str(e), e)

p = os.path.join(tmp, "link.zip")
with zipfile.ZipFile(p, "w") as z:
    zi = zipfile.ZipInfo("evil")
    zi.external_attr = (0o120777 << 16)      # S_IFLNK
    z.writestr(zi, "/etc/shadow")
try:
    m.extract_artifact(p, tempfile.mkdtemp())
    check("symlink refused", False, "was accepted")
except m.ArtifactRejected as e:
    check("symlink refused", "link" in str(e), e)

p = os.path.join(tmp, "bomb.zip")
with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as z:
    z.writestr("big", b"\0" * (m.ARTIFACT_MAX_UNPACKED + 1024))
try:
    m.extract_artifact(p, tempfile.mkdtemp())
    check("decompression bomb refused", False, "was accepted")
except m.ArtifactRejected as e:
    check("decompression bomb refused", "expands" in str(e), e)

print("\n-- package root")
d = tempfile.mkdtemp()
m.extract_artifact(zip_with({"proj/oaap-app.yaml": MANIFEST},
                            os.path.join(tmp, "nested.zip")), d)
try:
    check("single top-level folder found",
          m.package_root(d).endswith("proj"))
except Exception as e:
    check("single top-level folder found", False, e)

d = tempfile.mkdtemp()
m.extract_artifact(zip_with({"a/x": "1", "b/y": "2"},
                            os.path.join(tmp, "two.zip")), d)
try:
    m.package_root(d)
    check("missing manifest reported", False, "was accepted")
except m.ArtifactRejected as e:
    check("missing manifest reported", "oaap-app.yaml" in str(e), e)

print("\n-- envelope rule (RFC-0019 §3)")
base = yaml.safe_load(MANIFEST)
inst = {"app_id": "demo", "version": "0.1.0",
        "routes": base["routes"], "declared_endpoints": [], "storage": []}

hard, confirm = m.envelope_review(inst, base)
check("same version is a hard refusal",
      any("different version" in h for h in hard), hard)

bumped = yaml.safe_load(MANIFEST)
bumped["app"]["version"] = "0.2.0"
hard, confirm = m.envelope_review(inst, bumped)
check("clean bump passes", not hard and not confirm, (hard, confirm))

other = yaml.safe_load(MANIFEST)
other["app"]["id"] = "somethingelse"
other["app"]["version"] = "0.2.0"
hard, _ = m.envelope_review(inst, other)
check("changed app id is a hard refusal", any("belongs to one app" in h for h in hard), hard)

widened = yaml.safe_load(MANIFEST)
widened["app"]["version"] = "0.2.0"
widened["routes"] = [{"path": "/", "roles": ["public"]}]
hard, confirm = m.envelope_review(inst, widened)
check("new public route needs confirmation",
      not hard and any("without login" in c for c in confirm), (hard, confirm))

ep = yaml.safe_load(MANIFEST)
ep["app"]["version"] = "0.2.0"
ep["endpoints"] = [{"name": "media", "protocol": "udp", "container_port": 8280}]
hard, confirm = m.envelope_review(inst, ep)
check("new declared endpoint needs confirmation",
      any("past the gateway" in c for c in confirm), confirm)

st = yaml.safe_load(MANIFEST)
st["app"]["version"] = "0.2.0"
st["storage"] = [{"name": "data", "mount": "/data"}]
hard, confirm = m.envelope_review(inst, st)
check("new storage mount needs confirmation",
      any("storage" in c for c in confirm), confirm)

print("\n-- grants: single use, bound, expiring")
os.makedirs(m.APPS_DIR, exist_ok=True)
dig = hashlib.sha256(b"secret").hexdigest()
m.grant_create("upload", "demo-test", dig, {"bytes": 5})
check("grant is spendable once", m.grant_spend("upload", "demo-test", dig) == {"bytes": 5})
check("grant cannot be spent twice", m.grant_spend("upload", "demo-test", dig) is None)

m.grant_create("upload", "demo-test", dig, {"bytes": 5})
check("grant is bound to its instance",
      m.grant_spend("upload", "other-test", dig) is None)
check("grant is bound to its kind",
      m.grant_spend("create", "demo-test", dig) is None)
check("still spendable for the right pair",
      m.grant_spend("upload", "demo-test", dig) == {"bytes": 5})

m.grant_create("upload", "demo-test", dig, {"bytes": 5}, ttl=-1)
check("expired grant is gone", m.grant_spend("upload", "demo-test", dig) is None)

m.grant_create("upload", "demo-test", dig, {"bytes": 1})
m.grants_drop_for("demo-test")
check("grants drop with the instance",
      m.grant_spend("upload", "demo-test", dig) is None)

print("\n-- announce refuses before anything is transferred")
reg = {"instances": {"demo-test": dict(inst, channel="test",
                                       app_name="Demo", source={"kind": "artifact"})}}
m.save_registry(reg)

sha = hashlib.sha256(b"x").hexdigest()
try:
    m.announce_artifact("demo-test", MANIFEST, sha, 10, dig)
    check("same version refused at announce", False, "was accepted")
except m.ArtifactRejected as e:
    check("same version refused at announce", "different version" in str(e), e)

try:
    m.announce_artifact("demo-test", yaml.safe_dump(bumped), sha,
                        m.ARTIFACT_MAX_BYTES + 1, dig)
    check("oversized announcement refused", False, "was accepted")
except m.ArtifactRejected as e:
    check("oversized announcement refused", "limit" in str(e), e)

try:
    m.announce_artifact("demo-test", "not: [valid", sha, 10, dig)
    check("broken YAML refused", False, "was accepted")
except m.ArtifactRejected as e:
    check("broken YAML refused", "YAML" in str(e), e)

try:
    m.announce_artifact("demo-test", yaml.safe_dump(widened), sha, 10, dig)
    check("widening refused without confirmation", False, "was accepted")
except m.ArtifactRejected as e:
    check("widening refused without confirmation", "confirmation" in str(e), e)

pending = m.load_grants().get("pending:demo-test")
check("widening is recorded as pending for the portal",
      bool(pending) and pending["payload"]["reasons"], pending)

v = m.announce_artifact("demo-test", yaml.safe_dump(bumped), sha, 10, dig)
check("clean announcement issues a grant", v == "0.2.0", v)
grant = m.grant_spend("upload", "demo-test", dig)
check("grant carries the announced facts",
      grant and grant["artifact_sha256"] == sha
      and grant["manifest_sha"] == hashlib.sha256(yaml.safe_dump(bumped).encode()).hexdigest(),
      grant)

print("\n-- the announcement binds the upload")
zpath = zip_with({"oaap-app.yaml": yaml.safe_dump(bumped)},
                 os.path.join(tmp, "good.zip"))
other_zip = zip_with({"oaap-app.yaml": yaml.safe_dump(widened)},
                     os.path.join(tmp, "swapped.zip"))
g = {"bytes": os.path.getsize(other_zip),
     "artifact_sha256": hashlib.sha256(open(other_zip, "rb").read()).hexdigest(),
     "manifest_sha": hashlib.sha256(yaml.safe_dump(bumped).encode()).hexdigest()}
try:
    m.install_artifact("demo-test", other_zip, grant=g)
    check("swapped manifest refused", False, "was accepted")
except m.ArtifactRejected as e:
    check("swapped manifest refused", "differs from the announced" in str(e), e)

g2 = {"bytes": 1, "artifact_sha256": g["artifact_sha256"],
      "manifest_sha": g["manifest_sha"]}
try:
    m.install_artifact("demo-test", zpath, grant=g2)
    check("size mismatch refused", False, "was accepted")
except m.ArtifactRejected as e:
    check("size mismatch refused", "announced" in str(e), e)

g3 = {"bytes": os.path.getsize(zpath), "artifact_sha256": "0" * 64,
      "manifest_sha": g["manifest_sha"]}
try:
    m.install_artifact("demo-test", zpath, grant=g3)
    check("checksum mismatch refused", False, "was accepted")
except m.ArtifactRejected as e:
    check("checksum mismatch refused", "checksum" in str(e), e)

print("\n-- retention keeps the current plus three")
d = m.artifact_dir("demo-test")
os.makedirs(d, exist_ok=True)
for i in range(6):
    with open(os.path.join(d, f"0.{i}.0-aaaaaaaaaaaa.zip"), "w") as f:
        f.write("x")
    time.sleep(0.01)
m.artifact_prune("demo-test")
kept = m.artifact_list("demo-test")
check("four artifacts kept", len(kept) == 4, kept)
check("the newest survive", kept[0] == "0.5.0-aaaaaaaaaaaa.zip", kept)

print(f"\n{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
