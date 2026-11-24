"""oaap.data.twin 0.3 -- the outbox relay (RFC-0032 build order step 2).

Reads every tenant's `events` table past its own watermark and publishes
each row to the node's MQTT broker (`oaap.events.broker`), retained, on
the twin's own topic tree (RFC-0032 §1.1):

    oaap/<tenant-id>/<type-key>/<object-id>[/<group-key>]

For a row that names a group it also appends that group's attribute
snapshot to the tenant's `states` table (RFC-0032 §1.4) -- in the SAME
transaction that advances the watermark, and only AFTER the broker has
confirmed the publish. A crash between the two re-publishes one row on
restart, which is harmless by design (§1.5): a retained message is
simply replaced, and `states` is keyed by its event id.

Runs as its own compose service `relay` (the `twin` image, a different
command), gated by the node profile `broker` -- Jörg's decision of
2026-09-12: no broker, no relay. The outbox then simply grows, nothing
is lost, and the portal's health page says so (§1.5).

Authenticates at the broker as the platform principal `oaap.relay` with
the node secret BROKER_RELAY_KEY (Jörg, 2026-09-12), not with an RFC-0027
key: a key is confined to its own tenant's tree, and this process
publishes for every tenant on the node. identity lets that principal
PUBLISH only -- never subscribe, never read -- and only into
`oaap/<known tenant-id>/...` (services/identity/app.py, RELAY_USER).

MQTT v5, not 3.1.1, on purpose: under 3.1.1 a broker acknowledges a
publish it refused on ACL grounds exactly like one it accepted, so a
misconfigured principal would advance the watermark over messages that
never went anywhere. v5 carries the refusal back as a reason code, and
a refused publish stops this tenant's relay at that row, loudly.

Deliberately not done, named rather than hidden:

- `type.created` rows carry no object and have no place in a tree of
  objects (§1.1): passed over, watermark advanced, nothing published.
- A backlog (events recorded before this relay first ran, or while the
  broker was away) is published in order, but each `states` row holds
  the group's snapshot at the moment the relay READ it -- §1.4's own
  wording -- not the value at the time of the event. The twin's
  append-only tables keep that history; `states` is not a copy of it.
- It polls every RELAY_POLL_SECONDS instead of LISTEN/NOTIFY -- right at
  this scale, revisited only if it becomes a cost. **It became one**
  (RFC-0127 §1.6, measured 2026-11-24: about 60 rows a second for one
  tenant, 41 each for two; a burst arrived minutes late). Since 0.1.365
  (RFC-0127 step 1.6, oaap.data.twin 0.16.1 §2.13):
  * a batch is published without waiting for each PUBACK -- all of it
    goes out, then the relay waits for all confirmations (paho keeps at
    most the broker's receive maximum in flight); the watermark and the
    `states` rows move in ONE transaction to the last row confirmed in
    order, so a refusal still stops the tenant at exactly that row;
  * no sleep while a backlog remains: a pass in which any tenant filled
    its batch is followed at once by the next. Every tenant still gets at
    most BATCH rows per pass, so a quiet tenant's event waits one pass at
    most, never behind another tenant's whole backlog.
"""
import json
import os
import re
import signal
import sys
import time

PLATFORM_APPS_DIR = "/platform-apps"
SECRETS_FILE = os.path.join(PLATFORM_APPS_DIR, "twin-secrets.json")
STORE_HOST = os.environ.get("STORE_HOST", "store")
STORE_PORT = int(os.environ.get("STORE_PORT", "5432"))
BROKER_HOST = os.environ.get("BROKER_HOST", "broker")
BROKER_PORT = int(os.environ.get("BROKER_PORT", "1883"))
RELAY_KEY = os.environ.get("BROKER_RELAY_KEY", "")

# Must match services/identity/app.py's RELAY_USER exactly. The dot makes
# it a name no RFC-0027 key id (hex digits only) can ever be -- the same
# syntactic impossibility TWIN_KEY_SCOPE ('oaap.twin') already relies on.
RELAY_USER = "oaap.relay"

POLL_SECONDS = float(os.environ.get("RELAY_POLL_SECONDS", "2"))
BATCH = 200
# How often an idle relay says "still here" into relay_watermark. The
# portal calls a relay silent after oaap.core.portal's own threshold
# (relay_view.RELAY_STALE_SECONDS, several heartbeats), never after one.
HEARTBEAT_SECONDS = 60
PUBLISH_TIMEOUT = 10
# 0.1.365: a whole batch waits at most this long for its confirmations.
BATCH_TIMEOUT = 30
# A broker that has not answered this long after start counts as a
# problem; before that it is merely still connecting.
CONNECT_GRACE_SECONDS = 15

# One topic segment: never MQTT's own separator or wildcards. The twin's
# own validation already guarantees that for type keys, group keys and
# UUIDs (app.py KEY_RE / MODEL_GROUP_KEY_RE); checked again here because
# this process trusts no row it did not write itself.
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")


class PublishFailed(Exception):
    """The broker is unreachable, or it refused the publish."""


def log(msg):
    print(f"relay: {msg}", flush=True)


# ------------------------------------------------------------ pure rules
def tenant_schemas(secrets_map):
    """[(tenant_id, schema, creds)] for every twin schema this node has --
    the same file, and the same 'twin_<tenant-id>' naming, the twin
    service itself reads (app.py get_conn)."""
    out = []
    for schema, creds in sorted((secrets_map or {}).items()):
        if (schema.startswith("twin_") and isinstance(creds, dict)
                and creds.get("role") and creds.get("password")):
            out.append((schema[len("twin_"):], schema, creds))
    return out


def topic_for(tenant_id, type_key, object_id, group_key):
    """The topic of one event row, or None when it has no place in the
    tree (no object -- `type.created` -- or a segment that would not be
    a single, literal topic level)."""
    if not object_id or not type_key:
        return None
    parts = ["oaap", tenant_id, type_key, str(object_id)]
    if group_key:
        parts.append(group_key)
    if not all(_SEGMENT_RE.fullmatch(p or "") for p in parts[1:]):
        return None
    return "/".join(parts)


def _iso(v):
    return v.isoformat() if hasattr(v, "isoformat") else v


def message_for(row):
    """The retained message: the thin `events` row, no values (RFC-0032
    §1.3). A subscriber learns THAT something changed and asks the twin's
    own API for WHAT. `id` and `recorded_at` ride along so a subscriber
    can order and de-duplicate without a second lookup."""
    oid = row.get("object_id")
    return json.dumps({
        "id": row["id"],
        "kind": row["kind"],
        "object_id": f"urn:oaap:obj:{oid}" if oid else None,
        "group_key": row.get("group_key") or "",
        "origin": row.get("origin") or "",
        "recorded_at": _iso(row.get("recorded_at")),
    }, separators=(",", ":")).encode("utf-8")


def status_message(conn, row):
    """A status change (oaap.data.twin 0.15 §2.27) goes out on
    `.../<object>/status` WITH from, to and reason -- a subscriber needs
    them, and a status is not personal data (RFC-0120 D10)."""
    with conn:
        with conn.cursor() as cur:
            cur.execute("SELECT from_status, to_status, reason FROM statuses "
                        "WHERE event_id = %s", (row["id"],))
            st = cur.fetchone() or {}
    msg = json.loads(message_for(row))
    msg["status"] = {"from": st.get("from_status"), "to": st.get("to_status"),
                     "reason": st.get("reason")}
    return json.dumps(msg, separators=(",", ":")).encode("utf-8")


def wants_state(row, topic):
    """Only a published row that names an object AND a group gets a
    `states` row -- the grain is the group (RFC-0032 D3)."""
    return bool(topic and row.get("object_id") and row.get("group_key"))


def snapshot_payload(attr_rows):
    """The group's current attributes as one JSON object -- the same
    shape the twin's own object read returns per group (app.py
    _load_object), so a reader needs to learn nothing new."""
    return {r["attr_key"]: {"value": r["value"],
                            "valid_from": _iso(r.get("valid_from")),
                            "valid_to": _iso(r.get("valid_to"))}
            for r in attr_rows}


# ------------------------------------------------------------- the broker
def make_client(link):
    import paho.mqtt.client as mqtt

    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="oaap-relay",
                    protocol=mqtt.MQTTv5)
    c.username_pw_set(RELAY_USER, RELAY_KEY)
    acks = {}

    def on_connect(_c, _u, _flags, reason_code, _props):
        if reason_code.is_failure:
            link["error"] = f"der Broker lehnt die Anmeldung des Relais ab ({reason_code})"
            log(link["error"])
        else:
            link["error"] = None
            log("connected to the broker")

    def on_disconnect(_c, _u, _flags, reason_code, _props):
        if reason_code.is_failure:
            link["error"] = f"Verbindung zum Broker verloren ({reason_code})"
            log(link["error"])

    def on_publish(_c, _u, mid, reason_code, _props):
        acks[mid] = reason_code

    c.on_connect, c.on_disconnect, c.on_publish = on_connect, on_disconnect, on_publish
    c.reconnect_delay_set(1, 30)
    c.connect_async(BROKER_HOST, BROKER_PORT, keepalive=30)
    c.loop_start()
    return c, acks


def publish_batch(client, acks, items, timeout=BATCH_TIMEOUT):
    """Publish [(topic, payload)] retained, QoS 1, without waiting between
    them; then wait for every confirmation. Returns (n, error): the number
    of leading items the broker confirmed IN ORDER, and why it stopped
    there (None when all went through). What follows a failure may have
    been published too -- harmless, it is published again (§1.5)."""
    infos = []
    for topic, payload in items:
        try:
            info = client.publish(topic, payload, qos=1, retain=True)
        except (RuntimeError, ValueError) as e:
            infos.append((topic, None, f"Broker nicht erreichbar ({e})"))
            break
        if info.rc != 0:
            infos.append((topic, None, f"Broker nicht erreichbar (rc={info.rc})"))
            break
        infos.append((topic, info, None))
    deadline = time.time() + timeout
    for i, (topic, info, err) in enumerate(infos):
        if err:
            return i, err
        try:
            info.wait_for_publish(timeout=max(0.1, deadline - time.time()))
        except (RuntimeError, ValueError) as e:
            return i, f"Broker nicht erreichbar ({e})"
        if not info.is_published():
            return i, (f"der Broker hat die Veröffentlichung nicht binnen "
                       f"{timeout} s bestätigt")
        rc = acks.pop(info.mid, None)
        if rc is not None and rc.is_failure:
            return i, f"der Broker verweigert '{topic}' ({rc})"
    return len(infos), None


def publish(client, acks, topic, payload):
    try:
        info = client.publish(topic, payload, qos=1, retain=True)
        if info.rc != 0:
            raise PublishFailed(f"Broker nicht erreichbar (rc={info.rc})")
        info.wait_for_publish(timeout=PUBLISH_TIMEOUT)
    except (RuntimeError, ValueError) as e:
        raise PublishFailed(f"Broker nicht erreichbar ({e})") from e
    if not info.is_published():
        raise PublishFailed(f"der Broker hat die Veröffentlichung nicht binnen "
                            f"{PUBLISH_TIMEOUT} s bestätigt")
    rc = acks.pop(info.mid, None)
    if rc is not None and rc.is_failure:
        raise PublishFailed(f"der Broker verweigert '{topic}' ({rc})")


# --------------------------------------------------------------- the store
def load_secrets():
    try:
        with open(SECRETS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def connect(schema, creds):
    import psycopg2
    import psycopg2.extras

    return psycopg2.connect(
        host=STORE_HOST, port=STORE_PORT, dbname="postgres",
        user=creds["role"], password=creds["password"],
        options=f"-c search_path={schema}", connect_timeout=5,
        cursor_factory=psycopg2.extras.RealDictCursor)


def _record(conn, error):
    with conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE relay_watermark SET checked_at = now(), "
                        "last_error = %s WHERE id = 1", (error,))


def relay_tenant(conn, tenant_id, client, acks, problem):
    """Publish this tenant's rows past its watermark, at most BATCH, in
    order. Returns (problem, full): the problem that stopped it (or the
    one it was handed), None when all is well; and whether the batch was
    full -- a backlog remains, the next pass comes at once (0.1.365)."""
    with conn:
        with conn.cursor() as cur:
            cur.execute("SELECT last_event_id FROM relay_watermark WHERE id = 1")
            wm = cur.fetchone()
    if wm is None:
        return ("Relais-Tabellen fehlen in diesem Mandanten -- "
                "'sudo oaap data store migrate-twin'"), False
    if problem:
        return problem, False
    # One read transaction for the rows AND their snapshots: the snapshot
    # is the group's value when the relay READ it (RFC-0032 §1.4).
    plan = []
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT e.id, e.recorded_at, e.kind, e.object_id, e.group_key, "
                "e.origin, o.type_key FROM events e "
                "LEFT JOIN objects o ON o.id = e.object_id "
                "WHERE e.id > %s ORDER BY e.id LIMIT %s",
                (wm["last_event_id"], BATCH))
            rows = cur.fetchall()
            for row in rows:
                topic = topic_for(tenant_id, row["type_key"], row["object_id"],
                                  row["group_key"])
                payload = None
                if wants_state(row, topic):
                    cur.execute(
                        "SELECT attr_key, value, valid_from, valid_to "
                        "FROM current_attributes WHERE object_id = %s "
                        "AND group_key = %s ORDER BY attr_key",
                        (row["object_id"], row["group_key"]))
                    payload = snapshot_payload(cur.fetchall())
                plan.append([row, topic, payload, None])
    for item in plan:
        row, topic = item[0], item[1]
        if topic and row["kind"] == "status.changed":
            item[1], item[3] = topic + "/status", status_message(conn, row)
    sendable = [i for i, (row, topic, _p, _m) in enumerate(plan) if topic]
    n_ok, err = publish_batch(client, acks, [
        (plan[i][1], plan[i][3] or message_for(plan[i][0])) for i in sendable])
    # Everything before the first unconfirmed publish is done -- rows without
    # a topic in between included (they publish nothing, §1.1).
    stop = sendable[n_ok] if n_ok < len(sendable) else len(plan)
    done = plan[:stop]
    if done:
        with conn:
            with conn.cursor() as cur:
                for row, _topic, payload, _m in done:
                    if payload is not None:
                        cur.execute(
                            "INSERT INTO states (event_id, object_id, group_key, "
                            "payload) VALUES (%s, %s, %s, %s::jsonb) "
                            "ON CONFLICT (event_id) DO NOTHING",
                            (row["id"], row["object_id"], row["group_key"],
                             json.dumps(payload)))
                last = done[-1][0]["id"]
                cur.execute(
                    "UPDATE relay_watermark SET last_event_id = %s, "
                    "updated_at = now(), checked_at = now(), last_error = NULL "
                    "WHERE id = 1 AND last_event_id < %s", (last, last))
    if err:
        return err, False
    return None, len(rows) >= BATCH


def main():
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    started = time.time()
    link = {"error": None}
    client = acks = None
    if RELAY_KEY:
        client, acks = make_client(link)
    else:
        log("BROKER_RELAY_KEY is not configured -- not connecting (fail closed); "
            "'sudo oaap update' generates it")
    # 'logged': the last exception text per schema. An update recreates
    # this container BEFORE migrate.sh adds a new table to existing tenant
    # schemas, so for a few seconds every poll raised the same
    # UndefinedTable -- seen live on oaap-test, 2026-09-12, as a wall of
    # identical lines. Say it once, and again only when it changes.
    conns, beats, written, logged = {}, {}, {}, {}
    while True:
        now = time.time()
        if not RELAY_KEY:
            problem = ("BROKER_RELAY_KEY ist auf diesem Knoten nicht gesetzt -- "
                       "'sudo oaap update'")
        elif not client.is_connected() and now - started > CONNECT_GRACE_SECONDS:
            problem = link["error"] or "Broker nicht erreichbar"
        else:
            problem = None
        backlog = False
        for tenant_id, schema, creds in tenant_schemas(load_secrets()):
            try:
                if schema not in conns:
                    conns[schema] = connect(schema, creds)
                conn = conns[schema]
                result, full = relay_tenant(conn, tenant_id, client, acks, problem)
                backlog = backlog or full
                # Write the verdict when it CHANGES, and otherwise once a
                # heartbeat -- not on every poll, which would be a row
                # update every few seconds per tenant for nothing.
                if (result != written.get(schema, "unset")
                        or now - beats.get(schema, 0) >= HEARTBEAT_SECONDS):
                    _record(conn, result)
                    written[schema], beats[schema] = result, now
                    if result:
                        log(f"{tenant_id}: {result}")
                logged.pop(schema, None)
            except Exception as e:  # one tenant's trouble never stops the rest
                msg = (f"{type(e).__name__}: {e}".strip().splitlines() or ["?"])[0]
                if logged.get(schema) != msg:
                    log(f"{tenant_id}: {msg}")
                    logged[schema] = msg
                old = conns.pop(schema, None)
                if old is not None:
                    try:
                        old.close()
                    except Exception:
                        pass
        # 0.1.365: no sleep while a backlog remains (RFC-0127 §1.6)
        if not backlog:
            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
