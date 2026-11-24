"""The event bus's door in the twin service (oaap.events.bus 0.1, RFC-0127).

The gateway serves `/platform/events/*` on an instance's own site, asks
identity for the key scoped to `oaap.events:<that instance>`, strips the
prefix and passes here as `/bus/v1/...` with the source headers. This module
believes the caller only when identity's principal and the gateway's source
name the same instance, and takes everything else -- tenant, app, declared
types -- from files only the node writes (registry.json,
events-catalog.json).

    POST /bus/v1/        publish (CloudEvents structured or binary)
    GET  /bus/v1/feed    the granted and addressed events after ?since=<id>
    GET  /bus/v1/relate  the record of who may address whom (0.2): asked by
                         the gateway's case door as a second forward_auth,
                         never reachable through /platform/events (the
                         gateway serves only `/` and `/feed` there)
"""
import datetime
import json
import os
import random
import sys

from flask import jsonify, request

import bus_model as bm

CATALOG_NAME = "events-catalog.json"
AUDIT_LOG = "/audit/tenant-log.jsonl"
# who acts on the portal page (0.6): the tenant's own administrators
PAGE_ROLES = {"tenant_admin", "server_admin"}


def audit(action, tenant, subject, who, detail=""):
    """One line in the tenant audit log (oaap.core.tenant 1.7), the form
    appctl and identity write -- appended, never rewritten; a failure to
    write never fails the action, it is said on stderr."""
    entry = {"when": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
             "who": who or "?", "role": "-", "action": action, "tenant": tenant,
             "tenant_label": "", "subject": subject, "result": "ok"}
    if detail:
        entry["detail"] = detail
    try:
        with open(AUDIT_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as e:
        print(f"bus: audit not written: {e}", file=sys.stderr, flush=True)


def _iso(v):
    return v.isoformat() if hasattr(v, "isoformat") else v


def _load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def caller(apps_dir):
    """(instance, tenant, entry) or (None, status, sentence)."""
    user = request.headers.get("X-OAAP-User", "")
    src = request.headers.get("X-OAAP-Source-Instance", "")
    if not user.startswith("instance:") or not src or user != f"instance:{src}":
        return None, 403, ("this door is for an instance's own events key, on its own "
                           "site (oaap.events.bus 0.1 §2.5)")
    inst = (_load(os.path.join(apps_dir, "registry.json"), {}).get("instances") or {}).get(src)
    if not inst:
        return None, 403, f"'{src}' is not a registered instance"
    entry = (_load(os.path.join(apps_dir, CATALOG_NAME), {}).get("instances") or {}).get(src)
    if not entry or entry.get("tenant") != inst.get("tenant"):
        return None, 403, f"'{src}' declares no events block"
    return src, inst["tenant"], entry


def _body():
    """The CloudEvent from either mode: structured (the JSON body) or
    binary (ce-* headers, the body is `data`)."""
    ce_type = request.headers.get("ce-type")
    if ce_type:
        body = {"type": ce_type, "subject": request.headers.get("ce-subject")}
        if request.headers.get("ce-oaapto"):
            body["oaapto"] = request.headers["ce-oaapto"]
        raw = request.get_data(cache=False) or b"{}"
        try:
            body["data"] = json.loads(raw)
        except ValueError:
            return None
        return body
    try:
        return json.loads(request.get_data(cache=False) or b"null")
    except ValueError:
        return None


def register(app, get_conn, apps_dir):
    @app.post("/bus/v1/")
    @app.post("/bus/v1")
    def bus_publish():
        me, tenant, entry = caller(apps_dir)
        if me is None:
            return jsonify(error=entry), tenant
        if request.content_length and request.content_length > bm.MAX_DATA_BYTES + 16384:
            return jsonify(error=f"data is larger than {bm.MAX_DATA_BYTES // 1024} KB"), 413
        body = _body()
        status, why, acc = bm.check_publish(body, entry)
        if status != 202:
            return jsonify(error=why), status
        conn = get_conn(tenant)
        if conn is None:
            return jsonify(error="this tenant has no twin yet -- its event store lives there"), 409
        try:
            with conn, conn.cursor() as cur:
                cur.execute("SELECT count(*) AS n FROM bus_events WHERE source_instance = %s "
                            "AND time > now() - interval '1 minute'", (me,))
                if cur.fetchone()["n"] >= bm.RATE_PER_MINUTE:
                    return (jsonify(error=f"more than {bm.RATE_PER_MINUTE} events a minute"),
                            429, {"Retry-After": "60"})
                p = bm.find_type(entry, acc["type"])
                to = acc.get("to")
                if to:
                    reg = _load(os.path.join(apps_dir, "registry.json"), {}).get("instances") or {}
                    cat = _load(os.path.join(apps_dir, CATALOG_NAME), {}).get("instances") or {}
                    why = bm.addressee_error(cat.get(to), tenant, reg.get(to), acc["type"])
                    if why:
                        return jsonify(error=why), 403
                    cur.execute("SELECT 1 FROM bus_relations WHERE publisher = %s AND addressee = %s",
                                (me, to))
                    if cur.fetchone() is None:
                        return jsonify(error=(
                            f"'{to}' gave '{me}' nothing to address -- an addressed event goes "
                            "only to an instance the platform saw submit to this one "
                            "(oaap.events.bus 0.2 §2.9)")), 403
                eid = bm.ulid()
                cur.execute(
                    "INSERT INTO bus_events (id, type, source_instance, source_app, subject, "
                    "data, semantic, oaapto) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING time",
                    (eid, acc["type"], me, entry.get("app_id", ""), acc["subject"],
                     json.dumps(acc["data"]), p.get("semantic_id"), to))
                tm = cur.fetchone()["time"]
                if random.random() < 0.01:
                    cur.execute("DELETE FROM bus_events WHERE time < now() - %s * interval '1 day'",
                                (bm.RETENTION_DAYS,))
        finally:
            conn.close()
        ev = bm.envelope({"id": eid, "type": acc["type"], "source_instance": me,
                          "subject": acc["subject"], "time": tm, "data": acc["data"]},
                         tenant, entry.get("app_id", ""), p.get("semantic_id"))
        return jsonify(id=eid, type=acc["type"], time=ev["time"]), 202

    @app.post("/internal/bus/platform")
    def bus_platform_publish():
        """A platform service publishes (0.4): identity today. Behind
        app.py's '/internal/*' guard (the platform key), never through the
        gateway. Body {tenant, publisher, type, subject, data, to}. An
        addressed event to an instance that does not declare the type is
        not stored -- 200 with stored false, so the caller does not retry
        what nobody wants."""
        body = request.get_json(silent=True) or {}
        tenant = str(body.get("tenant") or "")
        status, why, acc = bm.check_platform_publish(body)
        if status != 202:
            return jsonify(error=why), status
        to = acc["to"]
        if to:
            reg = _load(os.path.join(apps_dir, "registry.json"), {}).get("instances") or {}
            cat = _load(os.path.join(apps_dir, CATALOG_NAME), {}).get("instances") or {}
            why = bm.addressee_error(cat.get(to), tenant, reg.get(to), acc["type"])
            if why:
                return jsonify(stored=False, why=why), 200
        conn = get_conn(tenant)
        if conn is None:
            return jsonify(stored=False, why="this tenant has no twin -- no event store"), 200
        p = bm.PLATFORM_TYPES[acc["type"]]
        eid = bm.ulid()
        try:
            with conn, conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO bus_events (id, type, source_instance, source_app, subject, "
                    "data, semantic, oaapto) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    (eid, acc["type"], "platform:" + acc["publisher"], "platform",
                     acc["subject"], json.dumps(acc["data"]), p["semantic_id"], to))
        finally:
            conn.close()
        return jsonify(stored=True, id=eid), 202

    # ------------------------------------------- the portal page (0.6)
    def _page_person():
        """(username, tenant) of a tenant admin the portal relays, or
        (None, (status, sentence)). Behind the '/internal/*' guard."""
        user = request.headers.get("X-OAAP-Person-User", "").strip()
        tenant = request.headers.get("X-OAAP-Person-Tenant", "").strip()
        roles = set(filter(None, request.headers.get("X-OAAP-Person-Roles", "").split(",")))
        if not user or not tenant:
            return None, (400, "missing person context")
        if not roles & PAGE_ROLES:
            return None, (403, "the page \"Ereignisse\" is for the tenant's administrators")
        return (user, tenant), None

    def _own_instance(tenant, name):
        cat = _load(os.path.join(apps_dir, CATALOG_NAME), {})
        e = (cat.get("instances") or {}).get(name)
        return cat if e and e.get("tenant") == tenant else None

    @app.get("/internal/bus/overview")
    def bus_overview():
        who, err = _page_person()
        if who is None:
            return jsonify(error=err[1]), err[0]
        _user, tenant = who
        cat = _load(os.path.join(apps_dir, CATALOG_NAME), {})
        reg = _load(os.path.join(apps_dir, "registry.json"), {})
        grants, deliveries, store = [], {}, False
        conn = get_conn(tenant)
        if conn is not None:
            store = True
            try:
                with conn, conn.cursor() as cur:
                    cur.execute("SELECT instance, type, subject_prefix, granted_by, granted_at "
                                "FROM bus_grants WHERE revoked_at IS NULL")
                    grants = [dict(r, granted_at=_iso(r["granted_at"])) for r in cur.fetchall()]
                    cur.execute("SELECT instance, delivered, failures, stopped_at, last_error, "
                                "last_ok_at FROM bus_deliveries")
                    deliveries = {r["instance"]: {k: _iso(v) for k, v in r.items()}
                                  for r in cur.fetchall()}
            finally:
                conn.close()
        out = bm.overview(cat, reg, tenant, grants, deliveries)
        out["store"] = store
        return jsonify(out)

    @app.post("/internal/bus/<action>")
    def bus_page_action(action):
        if action not in ("grant", "revoke", "resume"):
            return jsonify(error="grant, revoke or resume"), 404
        who, err = _page_person()
        if who is None:
            return jsonify(error=err[1]), err[0]
        user, tenant = who
        body = request.get_json(silent=True) or {}
        name, t = str(body.get("instance") or ""), str(body.get("type") or "")
        pre = str(body.get("subject_prefix") or "").strip()
        cat = _own_instance(tenant, name)
        if cat is None:
            return jsonify(error=f"'{name}' is not an instance of this tenant with an events block"), 404
        if action == "grant":
            why = bm.grant_error(cat, tenant, name, t)
            if why:
                return jsonify(error=why), 409
            if len(pre) > bm.SUBJECT_MAX or any(ord(c) < 32 for c in pre):
                return jsonify(error="subject prefix: at most 200 printable characters"), 400
        elif action == "revoke" and not t:
            return jsonify(error="type: required"), 400
        conn = get_conn(tenant)
        if conn is None:
            return jsonify(error="this tenant has no event store yet"), 409
        try:
            with conn, conn.cursor() as cur:
                if action == "grant":
                    cur.execute("INSERT INTO bus_grants (instance, type, subject_prefix, granted_by) "
                                "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING RETURNING id",
                                (name, t, pre or None, user))
                elif action == "revoke":
                    cur.execute("UPDATE bus_grants SET revoked_at = now(), revoked_by = %s "
                                "WHERE instance = %s AND type = %s AND revoked_at IS NULL "
                                "RETURNING id", (user, name, t))
                else:
                    cur.execute("UPDATE bus_deliveries SET stopped_at = NULL, failures = 0, "
                                "next_at = NULL WHERE instance = %s RETURNING instance", (name,))
                done = cur.fetchone() is not None
        finally:
            conn.close()
        if not done:
            return jsonify(error={"grant": f"'{name}' already holds a grant for {t}",
                                  "revoke": f"'{name}' holds no grant for {t}",
                                  "resume": f"'{name}' has no push yet"}[action]), 409
        audit(f"events.{action}", tenant, name if action == "resume" else f"{name} {t}", user,
              f"subject_prefix={pre}" if action == "grant" and pre else "")
        return jsonify(ok=True), 200

    @app.get("/bus/v1/feed")
    def bus_feed():
        me, tenant, entry = caller(apps_dir)
        if me is None:
            return jsonify(error=entry), tenant
        try:
            limit = int(request.args.get("limit", bm.FEED_LIMIT_DEFAULT))
        except ValueError:
            return jsonify(error="limit: a whole number"), 400
        limit = max(1, min(limit, bm.FEED_LIMIT_MAX))
        since = request.args.get("since", "")
        conn = get_conn(tenant)
        if conn is None:
            return jsonify(events=[], next=since or None)
        try:
            with conn, conn.cursor() as cur:
                after = 0
                if since:
                    cur.execute("SELECT seq FROM bus_events WHERE id = %s", (since,))
                    row = cur.fetchone()
                    if row is None:
                        return jsonify(error=f"since: {since} is not (or no longer) in the "
                                             "store -- read from the oldest without since"), 400
                    after = row["seq"]
                cur.execute("SELECT type, subject_prefix FROM bus_grants "
                            "WHERE instance = %s AND revoked_at IS NULL", (me,))
                where = bm.pending_conditions(
                    cur.fetchall(), [sb["type"] for sb in entry.get("subscribes") or []], me)
                if where is None:
                    return jsonify(events=[], next=since or None)
                cur.execute(
                    "SELECT id, type, source_instance, source_app, subject, time, data, semantic, "
                    f"oaapto FROM bus_events WHERE seq > %s AND {where[0]} ORDER BY seq LIMIT %s",
                    [after] + where[1] + [limit])
                rows = cur.fetchall()
        finally:
            conn.close()
        events = [bm.envelope(r, tenant, r["source_app"]) for r in rows]
        return jsonify(events=events, next=events[-1]["id"] if events else (since or None))

    @app.get("/bus/v1/relate")
    def bus_relate():
        """The case door's second forward_auth (0.2 §2.9): identity has
        just accepted the key of instance:<from> for its case door, the
        gateway names the inbox it passes to. Recorded once per pair, then
        counted. Always 200 once the caller is the right one -- a record
        that cannot be written (no twin, a database fault) must not stop a
        case: the addressed event is refused later, visibly, instead."""
        frm, to = request.args.get("from", ""), request.args.get("to", "")
        if request.headers.get("X-OAAP-User", "") != f"instance:{frm}" or not frm or not to:
            return jsonify(error="the record is written for the submitting instance only"), 403
        reg = _load(os.path.join(apps_dir, "registry.json"), {}).get("instances") or {}
        a, b = reg.get(frm), reg.get(to)
        if not a or not b or a.get("tenant") != b.get("tenant"):
            return jsonify(error="both instances, one tenant"), 403
        conn = get_conn(a["tenant"])
        if conn is None:
            return "", 200
        try:
            with conn, conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO bus_relations (publisher, addressee, via) VALUES (%s, %s, 'cases') "
                    "ON CONFLICT (publisher, addressee) DO UPDATE SET last_at = now(), "
                    "times = bus_relations.times + 1", (to, frm))
        except Exception as e:                       # noqa: BLE001
            print(f"bus: relation {frm} -> {to} not recorded: {e}", file=sys.stderr, flush=True)
        finally:
            conn.close()
        return "", 200
