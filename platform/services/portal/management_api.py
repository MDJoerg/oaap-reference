"""oaap.core.management 0.1 -- the tenant's own hand on the platform.

The cohort commands as a tenant-scoped JSON API (RFC-0046 stage 2). The
portal does only three things here: decide who may ask and in which
tenant, hand a request to the host-side worker through the spool it
already uses, and give the one-time handout back as a ZIP. Everything
that changes anything runs on the host (`appctl.cohort_job`), which
re-checks every rule: the spool is data, not trust.

The pure parts (reading an archive safely, building the handout ZIP,
the shape of a job) are plain functions so they can be tested without a
running portal.
"""
import io
import json
import os
import re
import stat
import time
import uuid
import zipfile
from datetime import datetime, timezone

from flask import Blueprint, Response, jsonify, request

PREFIX = "/api/v1/tenant"
OPERATOR = "/api/v1/operator"
SPOOL_DIR = "/deploy-spool"
VIEW = "/apps-registry/cohort-view.json"
BUILD_VIEW = "/apps-registry/build-view.json"

MAX_UPLOAD = 256 * 1024 * 1024
MAX_UNPACKED = 512 * 1024 * 1024
MAX_ENTRIES = 5000
MIN_PASSWORD = 8
JOB_KEEP_SECONDS = 24 * 3600

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
SEAT_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
JOB_RE = re.compile(r"^[0-9a-f]{32}$")
BUILD_RE = re.compile(r"^b-[0-9]{8}t[0-9]{6}-[a-z0-9-]{1,63}$")
PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

bp = Blueprint("management", __name__)
CTX = {}          # filled by init(): the portal's own helpers


# ------------------------------------------------------------ pure parts

def _wrapper(names):
    """The one folder a ZIP made by 'send folder to ZIP' (Windows, macOS) puts
    around everything -> 'folder/', or '' when there is none or it is not
    the only one. Exactly one level, and only when `cohort.yaml` is directly
    inside it: a template is never searched for."""
    if "cohort.yaml" in names:
        return ""
    tops = {n.split("/", 1)[0] for n in names if n and not n.startswith("__MACOSX/")}
    if len(tops) != 1:
        return ""
    top = tops.pop()
    return top + "/" if f"{top}/cohort.yaml" in names else ""


def archive_problem(data):
    """Why an uploaded template archive may not be used ('' when it may).

    Looked at BEFORE anything is written: a path that leaves the target,
    a link, a special entry, too many entries, too much when unpacked.
    """
    if len(data) > MAX_UPLOAD:
        return "too_large", "the archive is larger than 256 MiB"
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return "bad", "the body is not a ZIP archive"
    infos = zf.infolist()
    if len(infos) > MAX_ENTRIES:
        return "bad", f"more than {MAX_ENTRIES} entries"
    total = 0
    names = set()
    for i in infos:
        n = i.filename
        if not n or "\\" in n or "\x00" in n or n.startswith("/") \
                or re.match(r"^[A-Za-z]:", n):
            return "bad", f"entry '{n}' has a path that is not allowed"
        parts = n.rstrip("/").split("/")
        if any(p in ("..", "") for p in parts):
            return "bad", f"entry '{n}' leaves the template directory"
        mode = (i.external_attr >> 16) & 0xFFFF
        kind = stat.S_IFMT(mode)      # 0 for an archive that names no type
        if kind and kind not in (stat.S_IFREG, stat.S_IFDIR):
            return "bad", f"entry '{n}' is a link or a special file"
        if i.flag_bits & 0x1:
            return "bad", f"entry '{n}' is encrypted"
        total += i.file_size
        if total > MAX_UNPACKED:
            return "too_large", "more than 512 MiB when unpacked"
        names.add(n)
    if "cohort.yaml" not in names and f"{_wrapper(names)}cohort.yaml" not in names:
        return "bad", ("cohort.yaml is missing at the root of the archive "
                       "(zip the CONTENTS of the folder, or the folder itself "
                       "with cohort.yaml directly inside)")
    return "", ""


def extract_archive(data, dest):
    """Unpack an archive `archive_problem` accepted into `dest`."""
    zf = zipfile.ZipFile(io.BytesIO(data))
    base = os.path.realpath(dest)
    os.makedirs(base, exist_ok=True)
    strip = _wrapper({i.filename for i in zf.infolist()})
    for i in zf.infolist():
        name = i.filename
        if strip:
            if not name.startswith(strip):
                continue                       # e.g. __MACOSX/ noise beside the folder
            name = name[len(strip):]
            if not name:
                continue                       # the wrapper folder itself
        target = os.path.realpath(os.path.join(base, name))
        if target != base and not target.startswith(base + os.sep):
            raise ValueError("entry leaves the target")     # belt and braces
        if i.is_dir():
            os.makedirs(target, exist_ok=True)
            continue
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with zf.open(i) as src, open(target, "wb") as out:
            while True:
                chunk = src.read(1 << 20)
                if not chunk:
                    break
                out.write(chunk)


def handout_zip(csv_bytes, password=""):
    """The handout as a ZIP -> (bytes, encrypted?).

    With a password it is AES-256 (pyzipper); without one it is a plain
    container -- the trainer's call, said in a response header.
    """
    buf = io.BytesIO()
    if password:
        import pyzipper
        with pyzipper.AESZipFile(buf, "w", compression=pyzipper.ZIP_DEFLATED,
                                 encryption=pyzipper.WZ_AES) as z:
            z.setpassword(password.encode("utf-8"))
            z.setencryption(pyzipper.WZ_AES, nbits=256)
            z.writestr("handout.csv", csv_bytes)
    else:
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("handout.csv", csv_bytes)
    return buf.getvalue(), bool(password)


def job_status(spool, rid):
    """queued | running | done | None, from where the request lives."""
    jdir = os.path.join(spool, "jobs", rid)
    if os.path.isfile(os.path.join(jdir, "result.json")):
        return "done"
    if os.path.isfile(os.path.join(spool, "claims", rid + ".json")):
        return "running"
    if os.path.isfile(os.path.join(spool, "queue", rid + ".json")):
        return "queued"
    return None


def read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def shred(path):
    try:
        size = os.path.getsize(path)
        with open(path, "r+b") as f:
            f.write(bytes(size))
            f.flush()
            os.fsync(f.fileno())
        os.remove(path)
    except OSError:
        pass


# ------------------------------------------------------------ the doors

def err(code, text):
    return jsonify({"error": text}), code


def gate(write=False):
    """-> (tenant_id, role, None) or (None, None, error response)."""
    roles = CTX["caller_roles"]()
    if not roles & {"server_admin", "tenant_admin"}:
        return None, None, err(403, "this needs the role tenant_admin or server_admin")
    role, mine = CTX["caller_scope"]()
    bearer = request.headers.get("Authorization", "").startswith("Bearer ")
    if write and not bearer and request.headers.get("X-OAAP-API") != "1":
        return None, None, err(403, "a call with a session needs the header "
                                    "X-OAAP-API: 1 (a key does not)")
    host_tid = CTX["host_tenant"](request.host)
    if role == "tenant_admin":
        if not mine:
            return None, None, err(403, "this account has no tenant")
        if host_tid and host_tid != mine:
            return None, None, err(404, "no such place")
        return mine, role, None
    return host_tid or mine, role, None


def _json_body():
    if not request.data:
        return {}, None
    try:
        body = json.loads(request.data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None, err(400, "the body is not JSON")
    if not isinstance(body, dict):
        return None, err(400, "the body must be a JSON object")
    return body, None


def _view(tid):
    data = read_json(VIEW) or {}
    return (data.get("cohorts") or {}).get(tid) or {}


def enqueue(tid, role, op, args, rid=None, extra=None, action="cohort"):
    """Write the request into the spool; the worker takes it from there.
    Returns the job id. The portal's own pages use this too: one way in."""
    rid = rid or uuid.uuid4().hex
    jdir = os.path.join(SPOOL_DIR, "jobs", rid)
    os.makedirs(jdir, exist_ok=True)
    os.chmod(jdir, 0o700)
    meta = {"id": rid, "op": op, "by": CTX["caller_name"](), "tenant": tid,
            "role": role, "cohort": args.get("cohort", ""),
            "created": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    tmp = os.path.join(jdir, "meta.json.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(meta, f)
    os.replace(tmp, os.path.join(jdir, "meta.json"))
    payload = {"action": action, "op": op, "args": args, **(extra or {})}
    CTX["queue"](rid, "", payload, 0)
    return rid


def _submit(tid, role, op, args, rid=None, extra=None):
    rid = enqueue(tid, role, op, args, rid, extra)
    return jsonify({"job": rid, "status_url": f"{PREFIX}/jobs/{rid}"}), 202


def _may_see(meta, tid, role):
    if not meta:
        return False
    return role == "server_admin" or meta.get("tenant") == tid


# ------------------------------------------------------------ cohorts

@bp.get(PREFIX + "/cohorts")
def cohorts_list():
    tid, role, bad = gate()
    if bad:
        return bad
    rows = [{"name": c["name"], "state": c["state"], "seats": len(c["seats"]),
             "ends": (c.get("lifetime") or {}).get("ends", "")}
            for c in _view(tid).values()]
    return jsonify({"cohorts": rows})


@bp.get(PREFIX + "/cohorts/<name>")
def cohorts_show(name):
    tid, role, bad = gate()
    if bad:
        return bad
    c = _view(tid).get(name)
    if not c:
        return err(404, f"no cohort '{name}' in this tenant")
    return jsonify(c)


def start_create(data, tid, role):
    """Check a template archive, unpack it into a new job, queue the create.
    -> (job id, None) or (None, (status, text)). The page and the API share
    this: one way in."""
    if len(data) > MAX_UPLOAD:
        return None, (413, "the archive is larger than 256 MiB")
    kind, why = archive_problem(data)
    if kind == "too_large":
        return None, (413, why)
    if kind:
        return None, (400, why)
    rid = uuid.uuid4().hex
    jdir = os.path.join(SPOOL_DIR, "jobs", rid)
    try:
        extract_archive(data, os.path.join(jdir, "template"))
    except (ValueError, OSError, zipfile.BadZipFile) as e:
        return None, (400, f"the archive could not be unpacked: {e}")
    return enqueue(tid, role, "create", {}, rid=rid), None


@bp.post(PREFIX + "/cohorts")
def cohorts_create():
    tid, role, bad = gate(write=True)
    if bad:
        return bad
    if request.mimetype != "application/zip":
        return err(400, "send the template directory as a ZIP "
                        "(Content-Type: application/zip)")
    if request.content_length and request.content_length > MAX_UPLOAD:
        return err(413, "the archive is larger than 256 MiB")
    rid, bad = start_create(request.get_data(), tid, role)
    if bad:
        return err(*bad)
    return jsonify({"job": rid, "status_url": f"{PREFIX}/jobs/{rid}"}), 202


@bp.post(PREFIX + "/cohorts/<name>/<verb>")
def cohorts_verb(name, verb):
    tid, role, bad = gate(write=True)
    if bad:
        return bad
    if verb not in ("stop", "start", "extend"):
        return err(404, "no such operation")
    if not NAME_RE.match(name) or name not in _view(tid):
        return err(404, f"no cohort '{name}' in this tenant")
    body, bad = _json_body()
    if bad:
        return bad
    args = {"cohort": name}
    if verb == "extend":
        ends = str(body.get("ends") or "")
        if not DATE_RE.match(ends):
            return err(400, "'ends' must be a date, YYYY-MM-DD")
        args.update(ends=ends, dry_run=bool(body.get("dry_run")))
    if role == "server_admin" and body.get("tenant"):
        args["tenant"] = str(body["tenant"])
    return _submit(tid, role, verb, args)


@bp.post(PREFIX + "/cohorts/<name>/seats")
def seats_add(name):
    tid, role, bad = gate(write=True)
    if bad:
        return bad
    if not NAME_RE.match(name) or name not in _view(tid):
        return err(404, f"no cohort '{name}' in this tenant")
    body, bad = _json_body()
    if bad:
        return bad
    who = str(body.get("name") or "").strip()
    if len(who) > 80:
        return err(400, "the participant's name is too long")
    return _submit(tid, role, "add", {"cohort": name, "name": who})


@bp.post(PREFIX + "/cohorts/<name>/seats/<sid>/reset")
def seats_reset(name, sid):
    tid, role, bad = gate(write=True)
    if bad:
        return bad
    c = _view(tid).get(name)
    if not NAME_RE.match(name) or not c:
        return err(404, f"no cohort '{name}' in this tenant")
    if not SEAT_RE.match(sid) or sid not in [s["id"] for s in c["seats"]]:
        return err(404, f"no seat '{sid}' in this cohort")
    body, bad = _json_body()
    if bad:
        return bad
    return _submit(tid, role, "reset", {"cohort": name, "seat": sid,
                                        "keep_home": bool(body.get("keep_home"))})


def _removal(name, seat):
    tid, role, bad = gate(write=True)
    if bad:
        return bad
    c = _view(tid).get(name)
    if not NAME_RE.match(name) or not c:
        return err(404, f"no cohort '{name}' in this tenant")
    if seat and (not SEAT_RE.match(seat)
                 or seat not in [s["id"] for s in c["seats"]]):
        return err(404, f"no seat '{seat}' in this cohort")
    body, bad = _json_body()
    if bad:
        return bad
    if body.get("confirm") is None:
        return err(400, "a removal needs 'confirm': the cohort's name")
    if body.get("confirm") != name:
        return err(409, "'confirm' does not match the cohort's name")
    args = {"cohort": name, "confirm": name, "seat": seat,
            "purge": bool(body.get("purge")),
            "users": bool(body.get("users"))}
    return _submit(tid, role, "remove-seat" if seat else "remove", args)


@bp.delete(PREFIX + "/cohorts/<name>")
def cohorts_remove(name):
    return _removal(name, "")


@bp.delete(PREFIX + "/cohorts/<name>/seats/<sid>")
def seats_remove(name, sid):
    return _removal(name, sid)


# ------------------------------------------------- tenant builds (RFC-0055)
#
# The operator's door onto the same core `oaap tenant build` uses. It does
# what the cohort routes do and no more: decide who may ask, hand a request
# to the host-side worker, answer GETs from a view file the host writes. The
# worker re-checks the role from the actor's own record -- the spool is data.

def op_gate(write=False):
    """-> (tenant_id, role, None) or (None, None, error response).

    server_admin only, and only at the node's own address: a tenant's place
    is not where a node is administered."""
    tid, role, bad = gate(write)
    if bad:
        return None, None, bad
    if role != "server_admin":
        return None, None, err(403, "this needs the role server_admin")
    if CTX["host_tenant"](request.host):
        return None, None, err(404, "no such place")
    return tid, role, None


def _build_view():
    return read_json(BUILD_VIEW) or {}


def _build_row(st):
    return {k: st.get(k, "") for k in
            ("id", "profile", "label", "state", "created", "by")}


def _build_submit(tid, role, op, args):
    rid = enqueue(tid, role, op, args, action="tenant-build")
    return jsonify({"job": rid, "status_url": f"{PREFIX}/jobs/{rid}"}), 202


@bp.get(OPERATOR + "/tenant-profiles")
def build_profiles():
    tid, role, bad = op_gate()
    if bad:
        return bad
    return jsonify({"profiles": _build_view().get("profiles") or []})


@bp.get(OPERATOR + "/tenant-builds")
def build_list():
    tid, role, bad = op_gate()
    if bad:
        return bad
    return jsonify({"builds": [_build_row(b) for b in
                               _build_view().get("builds") or []]})


@bp.get(OPERATOR + "/tenant-builds/<bid>")
def build_show(bid):
    tid, role, bad = op_gate()
    if bad:
        return bad
    if not BUILD_RE.match(bid):
        return err(404, "no such build")
    for b in _build_view().get("builds") or []:
        if b.get("id") == bid:
            return jsonify(b)
    return err(404, "no such build")


@bp.post(OPERATOR + "/tenant-builds")
def build_start():
    tid, role, bad = op_gate(write=True)
    if bad:
        return bad
    body, bad = _json_body()
    if bad:
        return bad
    prof = body.get("profile")
    params = body.get("params")
    if not isinstance(prof, str) or not PROFILE_RE.match(prof):
        return err(400, "'profile' names a profile (a short lowercase word)")
    if not isinstance(params, dict) or not all(
            isinstance(k, str) and isinstance(v, (str, int))
            and not isinstance(v, bool) for k, v in params.items()):
        return err(400, "'params' is an object of text values")
    return _build_submit(tid, role, "start",
                         {"profile": prof,
                          "params": {k: str(v) for k, v in params.items()}})


@bp.post(OPERATOR + "/tenant-builds/<bid>/<verb>")
def build_verb(bid, verb):
    tid, role, bad = op_gate(write=True)
    if bad:
        return bad
    if verb not in ("continue", "confirm", "rollback"):
        return err(404, "no such operation")
    if not BUILD_RE.match(bid):
        return err(404, "no such build")
    if not any(b.get("id") == bid for b in _build_view().get("builds") or []):
        return err(404, "no such build")
    body, bad = _json_body()
    if bad:
        return bad
    args = {"build": bid}
    if verb == "confirm":
        step = body.get("step")
        if not isinstance(step, str) or not PROFILE_RE.match(step):
            return err(400, "'step' names the waiting step")
        args["step"] = step
    if verb in ("continue", "rollback"):
        args["purge_instances"] = body.get("purge_instances") is True
    return _build_submit(tid, role, verb, args)


# ------------------------------------------------------------ jobs

def _job(rid):
    tid, role, bad = gate()
    if bad:
        return None, None, None, bad
    if not JOB_RE.match(rid):
        return None, None, None, err(404, "no such job")
    meta = read_json(os.path.join(SPOOL_DIR, "jobs", rid, "meta.json"))
    if not _may_see(meta, tid, role):
        return None, None, None, err(404, "no such job")
    return tid, role, meta, None


@bp.get(PREFIX + "/jobs/<rid>")
def jobs_show(rid):
    tid, role, meta, bad = _job(rid)
    if bad:
        return bad
    status = job_status(SPOOL_DIR, rid)
    if status is None:
        return err(404, "no such job")
    out = {"id": rid, "op": meta.get("op", ""), "status": status,
           "created": meta.get("created", ""), "cohort": meta.get("cohort", "")}
    if status == "done":
        res = read_json(os.path.join(SPOOL_DIR, "jobs", rid, "result.json")) or {}
        out["cohort"] = out["cohort"] or res.get("cohort", "")
        if res.get("build"):
            out["build"] = res["build"]
        out.update(ok=bool(res.get("ok")), message=res.get("message", ""),
                   finished=res.get("finished", ""),
                   handout=os.path.isfile(
                       os.path.join(SPOOL_DIR, "jobs", rid, "handout.csv")))
    return jsonify(out)


@bp.post(PREFIX + "/jobs/<rid>/handout")
def jobs_handout(rid):
    tid, role, meta, bad = _job(rid)
    if bad:
        return bad
    # the write door rule applies to the download as well: it has an effect
    # (the file is destroyed), and a session must prove it is not a page
    # on another site
    if not request.headers.get("Authorization", "").startswith("Bearer ") \
            and request.headers.get("X-OAAP-API") != "1":
        return err(403, "a call with a session needs the header X-OAAP-API: 1")
    if role != "server_admin" and meta.get("by") != CTX["caller_name"]():
        return err(403, "only the person who started the job may fetch its handout")
    body, bad = _json_body()
    if bad:
        return bad
    password = body.get("password") or ""
    if password and (not isinstance(password, str) or len(password) < MIN_PASSWORD):
        return err(400, f"the password needs at least {MIN_PASSWORD} characters")
    if job_status(SPOOL_DIR, rid) != "done":
        return err(409, "the job is not finished")
    got = claim_handout(rid, meta.get("cohort", ""), password)
    if got is None:
        return err(410, "the handout was fetched already, or there never was one")
    blob, encrypted = got
    headers = {"Content-Disposition": f'attachment; filename="handout-{rid[:8]}.zip"',
               "Cache-Control": "no-store"}
    if not encrypted:
        headers["X-OAAP-Handout"] = "unencrypted"
    return Response(blob, mimetype="application/zip", headers=headers)


def claim_handout(rid, cohort, password):
    """Take the one-time handout of a finished job -> (zip bytes, encrypted)
    or None when it is gone. The rename is atomic: exactly one caller wins,
    then the CSV is shredded and the fetch is noted in the audit log."""
    jdir = os.path.join(SPOOL_DIR, "jobs", rid)
    src = os.path.join(jdir, "handout.csv")
    claimed = os.path.join(jdir, f"handout.claimed.{uuid.uuid4().hex[:8]}")
    try:
        os.rename(src, claimed)
    except OSError:
        return None
    try:
        with open(claimed, "rb") as f:
            raw = f.read()
        blob, encrypted = handout_zip(raw, password)
    finally:
        shred(claimed)
    CTX["queue"](uuid.uuid4().hex, "", {
        "action": "cohort", "op": "handout-note",
        "args": {"cohort": cohort, "job": rid, "encrypted": encrypted}}, 0)
    return blob, encrypted


def init(app, caller_name, caller_roles, caller_scope, host_tenant, queue):
    """Register the API on the portal.

    `host_tenant(host)` -> the tenant id the HOST names, or None;
    `queue(rid, name, payload, wait)` -> hands a request to the worker.
    """
    CTX.update(caller_name=caller_name, caller_roles=caller_roles,
               caller_scope=caller_scope, host_tenant=host_tenant, queue=queue)
    app.register_blueprint(bp)
