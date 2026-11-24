"""The push into the app (oaap.events.bus 0.3, RFC-0127 D6, stage 1.4).

Runs as its own compose service `bus` (the twin image, another command), like
`hooks` and the relay. For every instance that declares `events.endpoint` it
posts what reaches it -- the granted and the addressed events, the feed's own
rule (bus_model.pending_conditions) -- to the app, through the gateway's
internal listener (`gateway:8099/e/<instance>`, which answers the platform
network only and passes to the declared path inside the instance's own
network; never the internet).

- **One event per POST**, the CloudEvents HTTP structured binding
  (`application/cloudevents+json`), signed `X-OAAP-Signature: t=..,v1=..`
  with the instance's OAAP_EVENTS_SECRET over `<t>.<body>`.
- **In store order, at least once:** the instance's cursor (`bus_deliveries.
  last_seq`) moves only after a 2xx; the next event waits for it. An app is
  idempotent by `id`.
- **Backoff, then stop:** a failure waits 2^n seconds (at most an hour);
  after PUSH_FAILURES_MAX in a row the push stops (`stopped_at`), says why,
  and waits for `oaap events resume <instance>`; the feed still serves.
- **Where it starts:** an instance seen for the first time starts at the
  store's newest event -- the past is the feed's (§2.8), not a flood.
"""
import http.client
import json
import os
import time

import psycopg2
import psycopg2.extras

import bus_model as bm

APPS = "/platform-apps"
SECRETS_FILE = os.path.join(APPS, "twin-secrets.json")
CATALOG_FILE = os.path.join(APPS, "events-catalog.json")
EVENT_SECRETS_FILE = os.path.join(APPS, "events-secrets.json")
STORE_HOST = os.environ.get("STORE_HOST", "store")
STORE_PORT = int(os.environ.get("STORE_PORT", "5432"))
GATEWAY = os.environ.get("OAAP_BUS_GATEWAY", "gateway:8099")
IDLE_S = 1
RETENTION_EVERY_S = 3600
USER_AGENT = "oaap-bus/1"


def log(msg):
    print(f"bus: {msg}", flush=True)


def _json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def post(instance, body, headers, gateway=GATEWAY):
    """(status, short reason) of one POST to the instance's endpoint."""
    host, _, port = gateway.partition(":")
    c = http.client.HTTPConnection(host, int(port or 80), timeout=bm.PUSH_TIMEOUT_S)
    try:
        c.request("POST", f"/e/{instance}", body=body, headers=headers)
        r = c.getresponse()
        text = r.read(300).decode("utf-8", "replace").strip().replace("\n", " ")
        return r.status, text[:200]
    except (OSError, http.client.HTTPException) as e:
        return 0, f"{type(e).__name__}: {e}"[:200]
    finally:
        c.close()


def state_of(cur, instance):
    cur.execute("SELECT * FROM bus_deliveries WHERE instance = %s", (instance,))
    row = cur.fetchone()
    if row is None:
        cur.execute("INSERT INTO bus_deliveries (instance, last_seq) "
                    "SELECT %s, COALESCE(max(seq), 0) FROM bus_events RETURNING *", (instance,))
        row = cur.fetchone()
    return row


def push_instance(cur, tenant, instance, entry, secret, send=post, now=None):
    """Deliver what is due for one instance. Returns a short word for the log:
    'nothing', 'waiting', 'stopped', 'delivered <n>', 'failed <reason>'."""
    st = state_of(cur, instance)
    if st["stopped_at"] is not None:
        return "stopped"
    t = time.time() if now is None else now
    if st["next_at"] is not None and st["next_at"].timestamp() > t:
        return "waiting"
    cur.execute("SELECT type, subject_prefix FROM bus_grants "
                "WHERE instance = %s AND revoked_at IS NULL", (instance,))
    where = bm.pending_conditions(cur.fetchall(),
                                  [sb["type"] for sb in entry.get("subscribes") or []], instance)
    if where is None:
        return "nothing"
    cur.execute("SELECT seq, id, type, source_instance, source_app, subject, time, data, semantic, "
                f"oaapto FROM bus_events WHERE seq > %s AND {where[0]} ORDER BY seq LIMIT %s",
                [st["last_seq"]] + where[1] + [bm.PUSH_PER_PASS])
    rows = cur.fetchall()
    if not rows:
        return "nothing"
    n = 0
    for r in rows:
        body = json.dumps(bm.envelope(r, tenant, r["source_app"]), separators=(",", ":")).encode()
        ts = int(time.time())
        status, why = send(instance, body, {
            "Content-Type": "application/cloudevents+json",
            "X-OAAP-Signature": bm.sign(secret, ts, body),
            "X-OAAP-Event-Id": r["id"], "User-Agent": USER_AGENT})
        if 200 <= status < 300:
            cur.execute("UPDATE bus_deliveries SET last_seq = %s, failures = 0, next_at = NULL, "
                        "last_error = NULL, delivered = delivered + 1, last_ok_at = now() "
                        "WHERE instance = %s", (r["seq"], instance))
            n += 1
            continue
        fails = st["failures"] + 1
        reason = f"HTTP {status} {why}".strip() if status else why
        if fails >= bm.PUSH_FAILURES_MAX:
            cur.execute("UPDATE bus_deliveries SET failures = %s, stopped_at = now(), "
                        "last_error = %s, next_at = NULL WHERE instance = %s",
                        (fails, reason, instance))
            return f"stopped after {fails} failures: {reason}"
        cur.execute("UPDATE bus_deliveries SET failures = %s, last_error = %s, "
                    "next_at = now() + %s * interval '1 second' WHERE instance = %s",
                    (fails, reason, bm.backoff(fails), instance))
        return (f"delivered {n}, then " if n else "") + f"failed ({fails}): {reason}"
    return f"delivered {n}"


def connect(schema, creds):
    return psycopg2.connect(host=STORE_HOST, port=STORE_PORT, dbname="postgres",
                            user=creds["role"], password=creds["password"],
                            options=f"-c search_path={schema}",
                            cursor_factory=psycopg2.extras.RealDictCursor, connect_timeout=5)


def main():
    log(f"started, through {GATEWAY}")
    quiet, last_retention = {}, 0.0
    while True:
        cat = (_json(CATALOG_FILE, {}) or {}).get("instances") or {}
        secrets = _json(EVENT_SECRETS_FILE, {}) or {}
        db = _json(SECRETS_FILE, {}) or {}
        by_tenant = {}
        for name, e in cat.items():
            if e.get("endpoint"):
                by_tenant.setdefault(e.get("tenant"), []).append((name, e))
        retention = time.time() - last_retention > RETENTION_EVERY_S
        for schema, creds in sorted(db.items()):
            if not schema.startswith("twin_") or not isinstance(creds, dict) or "role" not in creds:
                continue
            tid = schema[len("twin_"):]
            if tid not in by_tenant and not retention:
                continue
            try:
                conn = connect(schema, creds)
            except psycopg2.Error as e:
                if quiet.get(schema) != "connect":
                    log(f"{schema}: {str(e).strip()[:120]}")
                    quiet[schema] = "connect"
                continue
            try:
                for name, e in by_tenant.get(tid, []):
                    if not secrets.get(name):
                        if quiet.get(name) != "secret":
                            log(f"{name}: no OAAP_EVENTS_SECRET on the node yet -- waiting")
                            quiet[name] = "secret"
                        continue
                    with conn, conn.cursor() as cur:
                        what = push_instance(cur, tid, name, e, secrets[name])
                    if what not in ("nothing", "waiting", "stopped"):
                        log(f"{name}: {what}")
                    quiet.pop(name, None)
                if retention:
                    with conn, conn.cursor() as cur:
                        cur.execute("DELETE FROM bus_events WHERE time < now() - %s * interval '1 day'",
                                    (bm.RETENTION_DAYS,))
                quiet.pop(schema, None)
            except psycopg2.errors.UndefinedTable:
                if quiet.get(schema) != "table":
                    log(f"{schema}: no bus tables yet (the twin's schema step brings them)")
                    quiet[schema] = "table"
            except psycopg2.Error as e:
                log(f"{schema}: {str(e).strip()[:160]}")
            finally:
                conn.close()
        if retention:
            last_retention = time.time()
        time.sleep(IDLE_S)


if __name__ == "__main__":
    main()
