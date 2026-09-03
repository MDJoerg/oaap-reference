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
import json
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

print("\n-- confirm, then raise the version: the loop of 2026-08-24")
# Real case on oaapx01: refuse -> the administrator confirms -> the
# client raises the version (our own deployment sheet told it to) ->
# the confirmation no longer covers the manifest -> refuse. Three
# rounds before a human broke it by hand. The client never sees the
# portal, so the way out has to be in the refusal sentence itself.
wide1 = json.loads(json.dumps(widened))
wide1["app"]["version"] = "0.3.0"
try:
    m.announce_artifact("demo-test", yaml.safe_dump(wide1), sha, 10, dig)
    check("widening refused", False, "was accepted")
except m.ArtifactRejected as e:
    check("the refusal says to send THIS package again unchanged",
          "unchanged" in str(e) and "Do not raise the version" in str(e), e)

# the administrator confirms in the portal
grants = m.load_grants()
grants["pending:demo-test"]["payload"]["confirmed"] = True
m.save_grants(grants)

# ... and the client dutifully raises the version instead of resending
wide2 = json.loads(json.dumps(widened))
wide2["app"]["version"] = "0.3.1"
try:
    m.announce_artifact("demo-test", yaml.safe_dump(wide2), sha, 10, dig)
    check("the raised version is refused", False, "was accepted")
except m.ArtifactRejected as e:
    txt = str(e)
    check("the refusal names the confirmed version", "0.3.0" in txt, txt)
    check("and says to announce exactly that one again",
          "byte for byte unchanged" in txt, txt)
    check("and kills the reflex that causes the loop",
          "not installed" in txt and "INSTALLED" in txt, txt)

# the way out works: announce the confirmed manifest again, unchanged
grants = m.load_grants()
grants["pending:demo-test"]["payload"] = {
    "manifest_sha": hashlib.sha256(yaml.safe_dump(wide1).encode()).hexdigest(),
    "reasons": ["routes become reachable without login: /"],
    "version": "0.3.0", "confirmed": True}
m.save_grants(grants)
v = m.announce_artifact("demo-test", yaml.safe_dump(wide1), sha, 10, dig,
                        confirmed=True)
check("re-announcing the confirmed package installs it", v == "0.3.0", v)

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

print("\n-- instance creation grant (RFC-0019, Studio section)")
# The one privileged step no deploy token can cover: before the
# instance exists there is nothing to hold a token for. The permission
# is therefore SPENT for one creation, never held.
cdig = hashlib.sha256(b"anlege-erlaubnis").hexdigest()
new_mf = yaml.safe_load(MANIFEST)
new_mf["app"]["id"] = "frisch"
new_mf["app"]["version"] = "0.1.0"
new_text = yaml.safe_dump(new_mf)
udig = hashlib.sha256(b"upload-fuer-frisch").hexdigest()

m.save_profiles([])
m.grant_create("create", "frisch-test", cdig, {"channel": "test"})
try:
    m.announce_artifact("frisch-test", new_text, sha, 10, udig,
                        create_digest=cdig)
    check("creation needs the dev profile", False, "was accepted")
except m.ArtifactRejected as e:
    check("creation needs the dev profile", "dev" in str(e), e)

m.save_profiles(["dev"])
try:
    m.announce_artifact("frisch-test", new_text, sha, 10, udig,
                        create_digest=hashlib.sha256(b"erfunden").hexdigest())
    check("an invented grant is refused", False, "was accepted")
except m.ArtifactRejected as e:
    check("an invented grant is refused", "creation grant" in str(e), e)

try:
    m.announce_artifact("demo-test", yaml.safe_dump(bumped), sha, 10, udig,
                        create_digest=cdig)
    check("a creation grant does not touch an existing instance",
          False, "was accepted")
except m.ArtifactRejected as e:
    check("a creation grant does not touch an existing instance",
          "already exists" in str(e), e)

# A first package has no envelope to widen — the manifest IS what the
# administrator agreed to. The same manifest against an existing
# instance was refused above.
wide_mf = yaml.safe_load(new_text)
wide_mf["routes"] = [{"path": "/", "roles": ["public"]}]
v = m.announce_artifact("frisch-test", yaml.safe_dump(wide_mf), sha, 10, udig,
                        create_digest=cdig)
check("the first package needs no envelope confirmation", v == "0.1.0", v)
check("announcing does not spend the permission yet",
      m.grant_check("create", "frisch-test", cdig) is not None)
up = m.grant_spend("upload", "frisch-test", udig)
check("the upload grant knows it creates the instance",
      bool(up) and up["create"] and up["create_digest"] == cdig, up)
check("the permission is spent exactly once",
      m.grant_spend("create", "frisch-test", cdig) is not None
      and m.grant_spend("create", "frisch-test", cdig) is None)
m.grant_create("create", "spaeter-test", cdig, {"channel": "test"})
check("an open permission is visible to the operator",
      [g["instance"] for g in m.grants_of_kind("create")] == ["spaeter-test"],
      m.grants_of_kind("create"))
check("...and the listing carries no secret",
      all(set(g) == {"instance", "expires"} for g in m.grants_of_kind("create")))

m.grant_create("create", "spaeter-test", cdig, {"channel": "test"}, ttl=-1)
check("an expired permission is gone",
      m.grant_check("create", "spaeter-test", cdig) is None)

print("\n-- promotion to production (RFC-0020)")
# Die Zusage lautet: was produktiv geht, ist GENAU das, was getestet
# wurde. Deshalb pruefen wir hier, was eine Uebernahme verweigert —
# jede dieser Weigerungen ist ein Weg, auf dem etwas anderes als das
# Getestete produktiv gehen koennte.
prom_mf = yaml.safe_load(MANIFEST)
prom_mf["app"]["id"] = "shop"
prom_mf["app"]["version"] = "1.2.0"
prom_zip = zip_with({"oaap-app.yaml": yaml.safe_dump(prom_mf)},
                    os.path.join(DATA, "shop.zip"))
shop_dir = m.artifact_dir("shop-test")
os.makedirs(shop_dir, exist_ok=True)
import shutil as _sh
_sh.copy(prom_zip, os.path.join(shop_dir, "1.2.0-abc.zip"))

reg = m.load_registry()
reg["instances"]["shop-test"] = {
    "app_id": "shop", "app_name": "Shop", "version": "1.2.0", "channel": "test",
    "routes": [{"path": "/", "roles": ["user"]}], "storage": [],
    "source": {"kind": "artifact", "stored": "1.2.0-abc.zip", "path": ""}}
reg["instances"]["shop"] = {
    "app_id": "shop", "app_name": "Shop", "version": "1.1.0",
    "channel": "production", "routes": [{"path": "/", "roles": ["user"]}],
    "storage": [], "source": {"kind": "artifact", "stored": "1.1.0-old.zip"}}
reg["instances"]["fremd"] = {
    "app_id": "anderes", "app_name": "Anderes", "version": "0.1.0",
    "channel": "production", "routes": [], "storage": [], "source": {}}
reg["instances"]["git-test"] = {
    "app_id": "shop", "app_name": "Shop", "version": "1.3.0", "channel": "test",
    "routes": [], "storage": [], "source": {"kind": "git", "url": "https://x/y"}}
m.save_registry(reg)


def refuses(label, source, target, needle):
    try:
        m.promotion_review(m.load_registry(), source, target)
        check(label, False, "was accepted")
    except m.PromotionRefused as e:
        check(label, needle in str(e), e)


refuses("production cannot be promoted from", "shop", "shop", "not a test instance")
refuses("a git-installed test instance cannot be promoted",
        "git-test", "shop", "same BYTES")
refuses("promoting into another app is refused", "shop-test", "fremd",
        "never turns one app into another")
refuses("promoting into a test instance is refused", "shop-test", "git-test",
        "not a production instance")
refuses("an unknown source is refused", "gibtsnicht", "shop", "no instance named")

# Gleiche und kleinere Version: der Weg zurueck ist der Rueckschritt,
# nicht eine Uebernahme, die sich als Fortschritt ausgibt.
reg = m.load_registry()
reg["instances"]["shop"]["version"] = "1.2.0"
m.save_registry(reg)
refuses("the same version is not a promotion", "shop-test", "shop", "rollback")
reg["instances"]["shop"]["version"] = "1.3.0"
m.save_registry(reg)
refuses("a lower version is not a promotion", "shop-test", "shop", "higher version")
reg["instances"]["shop"]["version"] = "1.1.0"
m.save_registry(reg)

path, mf, notes = m.promotion_review(m.load_registry(), "shop-test", "shop")
check("a clean promotion is allowed", path.endswith("1.2.0-abc.zip") and not notes,
      (path, notes))
check("and it names the tested package, not a fresh build",
      os.path.isfile(path) and mf["app"]["version"] == "1.2.0")

# Der Rahmen wird gegen die PRODUKTIV-Instanz geprueft, nicht gegen den
# Teststand: ein Paket kann neben seinem Teststand unauffaellig sein und
# trotzdem erweitern, was produktiv erlaubt ist.
wide = yaml.safe_load(yaml.safe_dump(prom_mf))
wide["routes"] = [{"path": "/", "roles": ["public"]}]
wide_zip = zip_with({"oaap-app.yaml": yaml.safe_dump(wide)},
                    os.path.join(DATA, "shop-wide.zip"))
_sh.copy(wide_zip, os.path.join(shop_dir, "1.4.0-wide.zip"))
reg = m.load_registry()
reg["instances"]["shop-test"]["source"]["stored"] = "1.4.0-wide.zip"
reg["instances"]["shop-test"]["routes"] = [{"path": "/", "roles": ["public"]}]
m.save_registry(reg)
_p, _mf, notes = m.promotion_review(m.load_registry(), "shop-test", "shop")
check("a widening against production is reported", bool(notes), notes)
try:
    m.promote_artifact("shop-test", "shop", confirmed=False)
    check("and refuses without an explicit confirmation", False, "ran anyway")
except m.PromotionRefused as e:
    check("and refuses without an explicit confirmation",
          "confirm it explicitly" in str(e), e)

check("a new production instance needs a usable name",
      m.promotion_review(m.load_registry(), "shop-test", "shop-neu")[2] == [],
      "a free name has nothing to widen")
refuses("a malformed new name is refused", "shop-test", "Shop Neu",
        "lowercase letters")

check("version ordering: 1.10.0 is newer than 1.9.0",
      m._version_gt("1.10.0", "1.9.0") and not m._version_gt("1.9.0", "1.10.0"))
check("version ordering: unchanged is not newer",
      not m._version_gt("1.2.0", "1.2.0"))

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

print("\n-- an uploaded package creates the instance in its OWN tenant")
# Found on oaapx01 on 2026-09-03, minutes after the store fix: a
# tenant_admin uploaded a ZIP, the portal said "Test-Instanz angelegt"
# and the page it linked to answered "Instanz nicht gefunden".
#
# The instance was real -- it just belonged to nobody's tenant. The
# worker composes the KEY from the acting tenant (`cls-gliss-viewer`)
# and then handed only that key to install_artifact. The two facts a
# permit carries -- whose instance this is, and what the human typed --
# were dropped on the way, so the record fell back to the DEFAULT
# tenant and took the key for its name. Invisible to the admin who had
# just created it, because an instance of another tenant does not exist.
#
# Driven through the real worker with the build stubbed: the bug is in
# what the worker PASSES, so that is what this reads.
import argparse as _ap  # noqa: E402
import contextlib as _cl  # noqa: E402
import io as _io  # noqa: E402

m.save_profiles(["dev"])
m.ensure_default_tenant()
with _cl.redirect_stdout(_io.StringIO()):
    m.cmd_tenant(_ap.Namespace(
        action="create", name="kunde", target=None, title="Kunde",
        account="", account_name="", grace_days=30, yes=True, count=50))
KUNDE = m.tenant_by_label("kunde")[0]
ident_dir = os.path.join(DATA, "data", "identity")
os.makedirs(ident_dir, exist_ok=True)
with open(os.path.join(ident_dir, "users.json"), "w", encoding="utf-8") as f:
    json.dump([{"username": "kunde-chef", "roles": ["tenant_admin"],
                "tenant": KUNDE, "groups": [], "active": True}], f)

seen = {}
_real_install_artifact = m.install_artifact


def _capture(name, zip_path, grant, channel="test", path="", origin="",
             permit=None):
    seen.update(key=name, permit=permit or {})
    return "0.1.0", "ab" * 32


m.install_artifact = _capture
QUEUE = os.path.join(m.SPOOL_DIR, "queue")
os.makedirs(QUEUE, exist_ok=True)
os.makedirs(os.path.join(m.SPOOL_DIR, "uploads"), exist_ok=True)
with open(os.path.join(m.SPOOL_DIR, "uploads", "up1.zip"), "wb") as f:
    f.write(b"nicht echt -- der Bau ist gestubbt")
with open(os.path.join(QUEUE, "up1.json"), "w", encoding="utf-8") as f:
    json.dump({"id": "up1", "instance": "viewer", "action": "create",
               "from": "artifact", "by": "kunde-chef"}, f)
with _cl.redirect_stdout(_io.StringIO()):
    m.cmd_process_deploys(None)
with open(os.path.join(m.SPOOL_DIR, "results", "up1.json"), encoding="utf-8") as f:
    res = json.load(f)
m.install_artifact = _real_install_artifact

check("the upload is accepted", res.get("ok"), res)
check("the node files it under the tenant's key",
      seen.get("key") == "kunde-viewer", seen)
check("but the instance belongs to the tenant its creator acts in",
      seen["permit"].get("tenant") == KUNDE, seen)
check("and is called what the human typed, not what the node files it under",
      seen["permit"].get("name") == "viewer", seen)

print(f"\n{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
