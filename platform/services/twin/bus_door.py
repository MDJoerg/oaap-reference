"""The event bus's door in the twin service (oaap.events.bus 0.1, RFC-0127).

The gateway serves `/platform/events/*` on an instance's own site, asks
identity for the key scoped to `oaap.events:<that instance>`, strips the
prefix and passes here as `/bus/v1/...` with the source headers. This module
believes the caller only when identity's principal and the gateway's source
name the same instance, and takes everything else -- tenant, app, declared
types -- from files only the node writes (registry.json,
events-catalog.json).

    POST /bus/v1/        publish (CloudEvents structured or binary)
    GET  /bus/v1/feed    the granted events after ?since=<id>
"""
import json
import os
import random

from flask import jsonify, request

import bus_model as bm

CATALOG_NAME = "events-catalog.json"


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
                eid = bm.ulid()
                cur.execute(
                    "INSERT INTO bus_events (id, type, source_instance, source_app, subject, "
                    "data, semantic) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING time",
                    (eid, acc["type"], me, entry.get("app_id", ""), acc["subject"],
                     json.dumps(acc["data"]), p.get("semantic_id")))
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
                grants = cur.fetchall()
                if not grants:
                    return jsonify(events=[], next=since or None)
                conds, args = [], []
                for g in grants:
                    if g["subject_prefix"]:
                        conds.append("(type = %s AND starts_with(subject, %s))")
                        args += [g["type"], g["subject_prefix"]]
                    else:
                        conds.append("type = %s")
                        args.append(g["type"])
                cur.execute(
                    "SELECT id, type, source_instance, source_app, subject, time, data, semantic "
                    "FROM bus_events WHERE seq > %s AND source_instance <> %s AND oaapto IS NULL "
                    f"AND ({' OR '.join(conds)}) ORDER BY seq LIMIT %s",
                    [after, me] + args + [limit])
                rows = cur.fetchall()
        finally:
            conn.close()
        events = [bm.envelope(r, tenant, r["source_app"]) for r in rows]
        return jsonify(events=events, next=events[-1]["id"] if events else (since or None))
