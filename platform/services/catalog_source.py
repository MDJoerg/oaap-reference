"""A package catalog as a store source (RFC-0050 stage 2).

The catalog is an OAAP app that keeps released ZIP packages. The node
reads what it wrote straight from the instance's storage directory on
the host -- no HTTP, no token, nothing exposed:

  * the portal container cannot reach app containers (RFC-0016), and on
    some nodes no container reaches its own host at all;
  * a list served over a port would have to be guarded by a secret the
    portal also holds;
  * the operator already decides which instance is the catalog (a store
    source is a node decision), so "the files of THIS instance" is the
    same trust as "this URL", minus the network.

Everything in that directory is written by an app, so it is data:

  * a package path in the list must be relative, made of plain names, and
    must resolve to a REGULAR file INSIDE the directory -- a symlink to
    `/etc/shadow` is refused, not followed;
  * size and SHA-256 are taken from the FILE and compared with the list
    before one byte is unpacked, and the comparison is made on a COPY the
    node owns (`stage`), so the app cannot swap the file between the
    check and the install;
  * what an entry says about itself (id, version) is checked again
    against the manifest inside the ZIP by the install that follows.

Pure standard library: the host imports it, tests run it on a temp dir.
"""
import hashlib
import hmac
import json
import os
import re
import shutil
import stat
import tempfile
import time

LIST_FILE = "store.json"
MAX_LIST_BYTES = 2 * 1024 * 1024
MAX_ENTRIES = 500
SCHEME = "catalog:"

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
_SHA = re.compile(r"^[0-9a-f]{64}$")
_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


class CatalogError(Exception):
    """A refusal a person can read: the catalog said something the node
    will not act on."""


def is_catalog_url(url):
    return str(url or "").startswith(SCHEME)


def instance_of(url):
    """`catalog:<instance key>` -> the key, or ''."""
    key = str(url or "")[len(SCHEME):] if is_catalog_url(url) else ""
    return key if re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,127}", key) else ""


def _safe_rel(rel):
    """A relative path of plain names, or None."""
    if not isinstance(rel, str) or not rel or rel.startswith("/") \
            or "\\" in rel or "\x00" in rel:
        return None
    parts = rel.split("/")
    if len(parts) > 4 or not all(_NAME.match(p) and p not in (".", "..")
                                 for p in parts):
        return None
    return parts


def resolve_file(base, rel):
    """Absolute path of `rel` inside `base`, or raise.

    lstat on every component: a symlink anywhere on the way is refused,
    which is stricter than realpath-inside-base and needs no race-prone
    second look. The result is a regular file.
    """
    parts = _safe_rel(rel)
    if parts is None:
        raise CatalogError(f"package path '{str(rel)[:80]}' is not a plain "
                           "relative path")
    cur = os.path.realpath(base)
    for i, p in enumerate(parts):
        cur = os.path.join(cur, p)
        try:
            st = os.lstat(cur)
        except OSError:
            raise CatalogError(f"package file '{rel}' does not exist")
        if stat.S_ISLNK(st.st_mode):
            raise CatalogError(f"'{rel}' goes through a symbolic link")
        last = i == len(parts) - 1
        if last and not stat.S_ISREG(st.st_mode):
            raise CatalogError(f"'{rel}' is not a regular file")
        if not last and not stat.S_ISDIR(st.st_mode):
            raise CatalogError(f"'{rel}' goes through something that is not "
                               "a directory")
    return cur


def open_regular(base, rel):
    """The file `rel` inside `base`, opened for reading -- without ever
    following a link, and without a window between "looked" and "opened".

    Where the platform allows it (Linux, the node), every component is
    opened relative to the one before it with O_NOFOLLOW, so a directory
    swapped for a symlink a moment ago is refused by the kernel, not by
    a check that has already finished. Elsewhere (a developer's machine)
    the lstat walk of `resolve_file` is all there is.
    """
    parts = _safe_rel(rel)
    if parts is None:
        raise CatalogError(f"package path '{str(rel)[:80]}' is not a plain "
                           "relative path")
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not nofollow or os.open not in os.supports_dir_fd:
        return open(resolve_file(base, rel), "rb")
    fd = os.open(os.path.realpath(base), os.O_RDONLY | os.O_DIRECTORY)
    try:
        for p in parts[:-1]:
            nxt = os.open(p, os.O_RDONLY | os.O_DIRECTORY | nofollow, dir_fd=fd)
            os.close(fd)
            fd = nxt
        last = os.open(parts[-1], os.O_RDONLY | nofollow, dir_fd=fd)
    except OSError:
        raise CatalogError(f"package file '{rel}' does not exist or goes "
                           "through a symbolic link")
    finally:
        os.close(fd)
    if not stat.S_ISREG(os.fstat(last).st_mode):
        os.close(last)
        raise CatalogError(f"'{rel}' is not a regular file")
    return os.fdopen(last, "rb")


def _entry(a):
    """One list entry, cleaned, or None (an entry that cannot be trusted
    is left out, not repaired)."""
    if not isinstance(a, dict):
        return None
    app_id, version = a.get("id"), a.get("version")
    pkg = a.get("package")
    if not isinstance(app_id, str) or not _ID.match(app_id) \
            or not isinstance(version, str) or not 1 <= len(version) <= 64 \
            or not isinstance(pkg, dict):
        return None
    sha, size, rel = pkg.get("sha256"), pkg.get("size"), pkg.get("zip")
    if not isinstance(sha, str) or not _SHA.match(sha) \
            or not isinstance(size, int) or isinstance(size, bool) or size <= 0 \
            or _safe_rel(rel) is None:
        return None
    out = {k: a[k] for k in ("name", "summary", "description", "released",
                             "type", "license", "categories", "audience",
                             "app_class", "maturity", "status", "tags",
                             "roles", "profiles")
           if k in a}
    out.update({"id": app_id, "version": version,
                "package": {"zip": rel, "sha256": sha, "size": size}})
    return out


def read_list(base):
    """The validated list of the catalog whose storage is `base`.

    Returns {"name", "apps": [entry, ...]}. A missing list is an empty
    catalog (nothing released yet), a damaged one raises: an operator
    must hear "the catalog's list is broken", not see an empty store.
    """
    path = os.path.join(base, LIST_FILE)
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return {"name": "", "apps": []}
    except OSError as e:
        raise CatalogError(f"cannot read the catalog's list: {e}")
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise CatalogError("the catalog's list is not a regular file")
    if st.st_size > MAX_LIST_BYTES:
        raise CatalogError("the catalog's list is too large")
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError) as e:
        raise CatalogError(f"the catalog's list is not valid JSON: {e}")
    if not isinstance(doc, dict) or not isinstance(doc.get("apps"), list):
        raise CatalogError("the catalog's list has no 'apps' list")
    apps, seen = [], set()
    for a in doc["apps"][:MAX_ENTRIES]:
        e = _entry(a)
        if e is None or e["id"] in seen:
            continue                 # one entry per app id: the first wins
        seen.add(e["id"])
        apps.append(e)
    name = doc.get("name") if isinstance(doc.get("name"), str) else ""
    return {"name": name[:120], "apps": apps}


def snapshot(doc):
    """The list as the PORTAL may see it: no file paths, a marker that
    says the package is installed by the host. The portal asks for an
    install by app id; this is only what it shows."""
    out = []
    for e in doc["apps"]:
        e = dict(e)
        e["package"] = {"catalog": True, "sha256": e["package"]["sha256"],
                        "size": e["package"]["size"]}
        out.append(e)
    return {"schema": "0.2", "name": doc.get("name", ""), "apps": out}


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def stage(base, entry, stage_dir, max_bytes, now=None):
    """Copy the entry's ZIP into `stage_dir` and verify THE COPY.

    Returns the path of the verified copy, owned by the node. The size
    is checked first (from the file, and again while copying: a file
    that grows between the two is cut off and refused), then the SHA-256
    of the copy against the list. Nothing is unpacked here.
    """
    pkg = entry["package"]
    if pkg["size"] > max_bytes:
        raise CatalogError(f"the package is {pkg['size']} bytes, the limit "
                           f"is {max_bytes // (1024 * 1024)} MB")
    os.makedirs(stage_dir, mode=0o700, exist_ok=True)
    prune(stage_dir, now=now)
    f = open_regular(base, pkg["zip"])
    try:
        if os.fstat(f.fileno()).st_size != pkg["size"]:
            raise CatalogError("the package file does not have the size the "
                               "list announces")
        fd, dest = tempfile.mkstemp(prefix="pkg-", suffix=".zip", dir=stage_dir)
    except BaseException:
        f.close()
        raise
    try:
        copied = 0
        h = hashlib.sha256()
        with os.fdopen(fd, "wb") as out, f:
            for block in iter(lambda: f.read(1024 * 1024), b""):
                copied += len(block)
                if copied > pkg["size"]:
                    raise CatalogError("the package file grew while it was "
                                       "being read")
                h.update(block)
                out.write(block)
        if copied != pkg["size"] \
                or not hmac.compare_digest(h.hexdigest(), pkg["sha256"]):
            raise CatalogError("the package does not match the checksum in "
                               "the catalog's list -- not installed")
    except BaseException:
        try:
            os.remove(dest)
        except OSError:
            pass
        raise
    os.chmod(dest, 0o600)
    return dest


def prune(stage_dir, keep_seconds=3600, now=None):
    """Remove staged copies older than an hour: whoever asked for one has
    long since installed it or given up."""
    now = time.time() if now is None else now
    try:
        names = os.listdir(stage_dir)
    except OSError:
        return 0
    n = 0
    for fn in names:
        p = os.path.join(stage_dir, fn)
        try:
            if fn.startswith("pkg-") and now - os.path.getmtime(p) > keep_seconds:
                os.remove(p)
                n += 1
        except OSError:
            pass
    return n


def clear(stage_dir):
    shutil.rmtree(stage_dir, ignore_errors=True)
