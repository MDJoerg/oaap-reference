"""Webhook delivery (RFC-0120 stage 3, oaap.data.twin 0.13 §2.25).

Runs as its own compose service `hooks` (the `twin` image, a different
command), like the relay: it reads every tenant's `webhooks` and posts each
change a client may read to the client's URL -- the same entries the change
feed (`/twin/v1/changes`) answers, so a sync that missed a webhook reads the
feed from the last id it got.

- **Signed:** `X-OAAP-Signature: t=<unix seconds>,v1=<hex>` where `<hex>` is
  HMAC-SHA256 with the webhook's secret over `<t>.<body>`. The receiver
  recomputes it and refuses a stale `t`; the secret was shown to the client
  once, when it registered the webhook.
- **In order, at least once:** up to BATCH events per POST, oldest first; the
  webhook's cursor moves only after a 2xx. A receiver deduplicates by event id.
- **Retries with backoff, then stops:** a failure waits 2^n seconds (at most
  an hour); after FAILURES_MAX failures in a row the webhook stops and the
  portal's *Integration* page says so; the client resumes it
  (`POST /twin/v1/webhooks/{id}/resume`) and gets everything from its cursor.
- **Only to the internet:** every address the host resolves to is checked
  before connecting, and the connection goes to that checked address (no
  second lookup a DNS answer could change); no redirect is followed. A
  host the operator names in OAAP_TWIN_HOOK_ALLOW (a test receiver in the
  LAN) is the one exception. A tenant whose outbound traffic is closed
  (RFC-0068) posts nothing; its webhooks wait.
- **The client's current rights decide:** the types are read from the client
  record and the registry at every delivery; a removed client's webhooks
  stop.

**The CloudEvents format** (RFC-0127 D7, stage 3; oaap.events.bus 0.8): a
webhook registered with `format: cloudevents` gets, in one POST of
`application/cloudevents-batch+json` (a JSON array, at most BATCH of each),
its twin changes as `oaap.twin.object.*.v1` events and every event type the
tenant admin granted the client (`bus_grants`, instance `client:<label>`),
with the same signature. Before its first POST the target is asked once, as
the CloudEvents webhook spec's abuse protection says: `OPTIONS` with
`WebHook-Request-Origin`; only an answer 2xx whose `WebHook-Allowed-Origin`
is that origin or `*` lets delivery start -- otherwise it counts as a failure
and is asked again after the backoff. The old body (`oaap-v1`) stays for the
existing webhooks.
"""
import hashlib
import hmac
import http.client
import ipaddress
import json
import os
import socket
import ssl
import time
from urllib.parse import urlsplit

import psycopg2
import psycopg2.extras

import bus_model as bm
import twin_read as tr
import twin_sync as tsy

APPS = "/platform-apps"
SECRETS_FILE = os.path.join(APPS, "twin-secrets.json")
CLIENTS_FILE = os.path.join(APPS, "twin-clients.json")
TENANTS_FILE = os.path.join(APPS, "tenants.json")
STORE_HOST = os.environ.get("STORE_HOST", "store")
STORE_PORT = int(os.environ.get("STORE_PORT", "5432"))
ALLOW = tuple(h.strip().lower() for h in os.environ.get("OAAP_TWIN_HOOK_ALLOW", "").split(",")
              if h.strip())
BATCH = 50
TIMEOUT_S = 5
FAILURES_MAX = 10
BACKOFF_MAX_S = 3600
IDLE_S = 2
USER_AGENT = "oaap-twin-hooks/1"


def log(msg):
    print(f"hooks: {msg}", flush=True)


def _json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def sign(secret, ts, body):
    mac = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}"


def verify(secret, header, body, now=None, tolerance=300):
    """What a receiver does (and the tests): True when the signature fits and
    is not older than `tolerance` seconds."""
    try:
        parts = dict(p.split("=", 1) for p in header.split(","))
        ts = int(parts["t"])
    except (ValueError, KeyError):
        return False
    if abs((now or time.time()) - ts) > tolerance:
        return False
    return hmac.compare_digest(sign(secret, ts, body), header)


class Refused(Exception):
    pass


def checked_address(host, port):
    """The address to connect to: the first the name resolves to, after
    every one of them was checked to lie on the internet."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as e:
        raise Refused(f"{host} does not resolve ({e})")
    addrs = [i[4][0] for i in infos]
    if host.lower() not in ALLOW:
        for a in addrs:
            if not ipaddress.ip_address(a.split("%")[0]).is_global:
                raise Refused(f"{host} resolves to {a}, not an address on the internet")
    return addrs[0]


class _Pinned(http.client.HTTPSConnection):
    def __init__(self, host, addr, port, timeout):
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self._addr = addr

    def connect(self):
        sock = socket.create_connection((self._addr, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


class _PinnedPlain(http.client.HTTPConnection):
    def __init__(self, host, addr, port, timeout):
        super().__init__(host, port, timeout=timeout)
        self._addr = addr

    def connect(self):
        self.sock = socket.create_connection((self._addr, self.port), self.timeout)


def post(url, body, headers, method="POST"):
    """(status, short text, response headers) for OPTIONS, (status, short
    text) for POST -- never follows a redirect."""
    u = urlsplit(url)
    host = u.hostname or ""
    if u.scheme == "http" and host.lower() not in ALLOW:
        raise Refused("a webhook URL is https")
    port = u.port or (443 if u.scheme == "https" else 80)
    addr = checked_address(host, port)
    cls = _Pinned if u.scheme == "https" else _PinnedPlain
    conn = cls(host, addr, port, TIMEOUT_S)
    try:
        conn.request(method, (u.path or "/") + (f"?{u.query}" if u.query else ""), body=body,
                     headers=headers)
        r = conn.getresponse()
        text = r.read(300).decode("utf-8", "replace")
        if method == "OPTIONS":
            return r.status, text, {k.lower(): v for k, v in r.getheaders()}
        return r.status, text
    finally:
        conn.close()


def hook_origin(tenant_id, tenants):
    """WebHook-Request-Origin: OAAP_HOOK_ORIGIN, else `<tenant label>.oaap`."""
    if os.environ.get("OAAP_HOOK_ORIGIN"):
        return os.environ["OAAP_HOOK_ORIGIN"]
    label = (tsy.tenant_of(tenants, tenant_id) or {}).get("label") or tenant_id
    return f"{label}.oaap"


def handshake_problem(url, origin, send=post):
    """None when the target allows us (CloudEvents webhook spec, abuse
    protection), else the sentence."""
    try:
        status, text, headers = send(url, None, {"WebHook-Request-Origin": origin,
                                                 "User-Agent": USER_AGENT}, method="OPTIONS")
    except (Refused, OSError, http.client.HTTPException, ssl.SSLError) as e:
        return "handshake: " + (str(e)[:180] or e.__class__.__name__)
    allowed = (headers.get("webhook-allowed-origin") or "").strip()
    if not 200 <= status < 300:
        return f"handshake: OPTIONS answered HTTP {status}"
    if allowed not in (origin, "*"):
        return (f"handshake: WebHook-Allowed-Origin is {allowed or 'missing'!r}, "
                f"not {origin!r} or '*'")
    return None


def bus_batch(conn, principal, cursor, limit):
    """([row], last seq) of the store's events granted to this client after
    `cursor` -- nothing in a schema without the bus tables."""
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('bus_grants') IS NOT NULL AS ok")
        r = cur.fetchone()
        if not r or not r.get("ok"):
            return [], cursor
        cur.execute("SELECT type, subject_prefix, granted_at FROM bus_grants WHERE instance = %s "
                    "AND revoked_at IS NULL", (principal,))
        where = bm.client_conditions(cur.fetchall(), principal)
        if where is None:
            return [], cursor
        cur.execute("SELECT seq, id, type, source_instance, source_app, subject, time, data, "
                    f"semantic, oaapto FROM bus_events WHERE seq > %s AND {where[0]} "
                    "ORDER BY seq LIMIT %s", [cursor] + where[1] + [limit])
        rows = cur.fetchall()
    return rows, (rows[-1]["seq"] if rows else cursor)


def _record(conn, hook, err, n=0, cursor=None, bus_cursor=None, handshake=False):
    with conn.cursor() as cur:
        if handshake:
            cur.execute("UPDATE webhooks SET handshake_at = now() WHERE id = %s", (hook["id"],))
        if err is None:
            cur.execute("UPDATE webhooks SET cursor = %s, bus_cursor = %s, "
                        "delivered = delivered + %s, failures = 0, last_ok_at = now(), "
                        "last_error = NULL, next_try_at = NULL WHERE id = %s",
                        (cursor, bus_cursor, n, hook["id"]))
            return f"sent {n}"
        f = int(hook["failures"] or 0) + 1
        wait = min(2 ** f, BACKOFF_MAX_S)
        cur.execute("UPDATE webhooks SET failures = %s, last_error = %s, next_try_at = now() + "
                    "%s * interval '1 second', stopped_at = CASE WHEN %s THEN now() END "
                    "WHERE id = %s", (f, err, wait, f >= FAILURES_MAX, hook["id"]))
        return "stopped" if f >= FAILURES_MAX else "failed"


def deliver_ce(conn, hook, tenant_id, types, origin, now=None, send=post):
    """One step of a webhook in the CloudEvents format (0.8)."""
    now = now or time.time()
    handshook = False
    if not hook.get("handshake_at"):
        err = handshake_problem(hook["url"], origin, send)
        if err:
            return _record(conn, hook, err)
        handshook = True
    changes, nxt, _more = tr.changes(conn, types, hook["cursor"], BATCH) if types else (
        [], hook["cursor"], False)
    kinds = set(hook["kinds"] or [])
    own = "client:" + hook["principal"].split(":", 1)[1]
    evs = [bm.twin_change_event(c, tenant_id) for c in changes
           if (not kinds or c["kind"] in kinds) and (hook["own"] or c["origin"] != own)]
    evs = [e for e in evs if e]
    rows, bnext = bus_batch(conn, hook["principal"], int(hook.get("bus_cursor") or 0), BATCH)
    evs += [bm.envelope(r, tenant_id, r["source_app"]) for r in rows]
    if not evs:
        with conn.cursor() as cur:
            if handshook:
                # the target said yes: the earlier refusals are over
                cur.execute("UPDATE webhooks SET handshake_at = now(), failures = 0, "
                            "last_error = NULL, next_try_at = NULL WHERE id = %s", (hook["id"],))
            if nxt != hook["cursor"]:
                cur.execute("UPDATE webhooks SET cursor = %s WHERE id = %s", (nxt, hook["id"]))
        return "nothing"
    body = json.dumps(evs, separators=(",", ":"), default=str).encode()
    ts = int(now)
    headers = {"Content-Type": "application/cloudevents-batch+json", "User-Agent": USER_AGENT,
               "X-OAAP-Signature": sign(hook["secret"], ts, body),
               "X-OAAP-Hook": str(hook["id"])}
    try:
        status, text = send(hook["url"], body, headers)
        err = None if 200 <= status < 300 else f"HTTP {status}: {text[:120]}"
    except (Refused, OSError, http.client.HTTPException, ssl.SSLError) as e:
        err = str(e)[:200] or e.__class__.__name__
    return _record(conn, hook, err, len(evs), nxt, bnext, handshook)


def deliver_one(conn, hook, client, tenant_id, closed, now=None, send=post, origin="oaap"):
    """One step of one webhook: post its next events, or nothing. Returns a
    word for the log ('sent n', 'nothing', 'failed', 'stopped', 'waiting')."""
    now = now or time.time()
    with conn.cursor() as cur:
        if client is None:
            cur.execute("UPDATE webhooks SET stopped_at = now(), last_error = %s WHERE id = %s",
                        ("the client was removed", hook["id"]))
            return "stopped"
        if closed:
            cur.execute("UPDATE webhooks SET last_error = %s, next_try_at = now() + "
                        "interval '60 seconds' WHERE id = %s",
                        ("the tenant's outbound traffic is closed (RFC-0068)", hook["id"]))
            return "waiting"
    principal = hook["principal"]
    readable = {b["type_key"] for b in tsy.client_bindings(conn, tenant_id, principal, client)}
    types = set(hook["types"] or []) & readable if hook["types"] else readable
    if hook.get("format") == "cloudevents":
        return deliver_ce(conn, hook, tenant_id, types, origin, now, send)
    changes, nxt, _more = tr.changes(conn, types, hook["cursor"], BATCH) if types else (
        [], hook["cursor"], False)
    kinds = set(hook["kinds"] or [])
    own = "client:" + principal.split(":", 1)[1]
    events = [c for c in changes if (not kinds or c["kind"] in kinds)
              and (hook["own"] or c["origin"] != own)]
    if not events:
        if nxt != hook["cursor"]:
            with conn.cursor() as cur:
                cur.execute("UPDATE webhooks SET cursor = %s WHERE id = %s", (nxt, hook["id"]))
        return "nothing"
    body = json.dumps({"hook": int(hook["id"]), "events": events,
                       "next": nxt}, separators=(",", ":")).encode()
    ts = int(now)
    headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT,
               "X-OAAP-Signature": sign(hook["secret"], ts, body),
               "X-OAAP-Hook": str(hook["id"]), "X-OAAP-Event-Ids":
               f"{events[0]['id']}-{events[-1]['id']}"}
    try:
        status, text = send(hook["url"], body, headers)
        err = None if 200 <= status < 300 else f"HTTP {status}: {text[:120]}"
    except (Refused, OSError, http.client.HTTPException, ssl.SSLError) as e:
        err = str(e)[:200] or e.__class__.__name__
    with conn.cursor() as cur:
        if err is None:
            cur.execute("UPDATE webhooks SET cursor = %s, delivered = delivered + %s, "
                        "failures = 0, last_ok_at = now(), last_error = NULL, next_try_at = NULL "
                        "WHERE id = %s", (nxt, len(events), hook["id"]))
            return f"sent {len(events)}"
        n = int(hook["failures"] or 0) + 1
        wait = min(2 ** n, BACKOFF_MAX_S)
        cur.execute("UPDATE webhooks SET failures = %s, last_error = %s, next_try_at = now() + "
                    "%s * interval '1 second', stopped_at = CASE WHEN %s THEN now() END "
                    "WHERE id = %s", (n, err, wait, n >= FAILURES_MAX, hook["id"]))
        return "stopped" if n >= FAILURES_MAX else "failed"


def due_hooks(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM webhooks WHERE removed_at IS NULL AND stopped_at IS NULL "
                    "AND (next_try_at IS NULL OR next_try_at <= now()) ORDER BY id")
        return cur.fetchall()


def run_tenant(conn, tenant_id, clients, closed, origin="oaap"):
    out = []
    for hook in due_hooks(conn):
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_xact_lock(hashtext(%s)) AS got",
                        (f"hook:{hook['id']}",))
            if not cur.fetchone()["got"]:
                continue
        label = hook["principal"].split(":", 1)[1]
        client = clients.get(label)
        if client is not None and client.get("tenant") != tenant_id:
            client = None
        out.append((hook["id"], deliver_one(conn, hook, client, tenant_id, closed,
                                            origin=origin)))
    return out


def connect(schema, creds):
    return psycopg2.connect(host=STORE_HOST, port=STORE_PORT, dbname="postgres",
                            user=creds["role"], password=creds["password"],
                            options=f"-c search_path={schema}",
                            cursor_factory=psycopg2.extras.RealDictCursor, connect_timeout=5)


def main():
    log("started" + (f", private hosts allowed: {', '.join(ALLOW)}" if ALLOW else ""))
    quiet = {}
    while True:
        secrets_map = _json(SECRETS_FILE, {})
        clients = (_json(CLIENTS_FILE, {}) or {}).get("clients") or {}
        tenants = _json(TENANTS_FILE, {})
        for schema, creds in sorted(secrets_map.items()):
            if not schema.startswith("twin_") or not isinstance(creds, dict) \
                    or "role" not in creds:
                continue
            tid = schema[len("twin_"):]
            closed = tsy.egress_closed(tsy.tenant_of(tenants, tid))
            try:
                conn = connect(schema, creds)
            except psycopg2.Error as e:
                if quiet.get(schema) != "connect":
                    log(f"{schema}: {str(e).strip()[:120]}")
                    quiet[schema] = "connect"
                continue
            try:
                with conn:
                    for hid, what in run_tenant(conn, tid, clients, closed,
                                                hook_origin(tid, tenants)):
                        if what not in ("nothing", "waiting"):
                            log(f"{schema} webhook {hid}: {what}")
                quiet.pop(schema, None)
            except psycopg2.errors.UndefinedTable:
                pass                       # not migrated yet (oaap data store migrate-twin)
            except psycopg2.Error as e:
                if quiet.get(schema) != "db":
                    log(f"{schema}: {str(e).strip()[:160]}")
                    quiet[schema] = "db"
            finally:
                conn.close()
        time.sleep(IDLE_S)


if __name__ == "__main__":
    main()
