"""oaap.net.connector 0.1 -- the tunnel of RFC-0033 stage 2.

One service, two roles, decided by what the operator configured on this
node (spec 2.1):

- OUTER: the node issued connect keys. Inner nodes dial in on
  `/connect/tunnel` (the gateway passes the path through, spec 2.4), and the
  gateway's destination listener hands calls for `via` destinations to
  `/via/<tunnel>/<offer>/...` (spec 2.5), which this process carries
  into the tunnel.
- INNER: the node has connectors. For each one that is not paused this
  process dials out, announces the offer NAMES, and answers every call
  that arrives from its offer list -- checked here, against the current
  list, before anything is sent to a backend (spec 2.6).

Since 0.2 (RFC-0033 stage 3) the same tunnel also carries EXPOSURES: one
target behind one random public name (spec 2.8). The outer role serves
`/exposure/verify` (who may pass) and `/exposed/...` (the call itself)
to the gateway's zone site; the inner role -- a connector, or the laptop
client `client/oaap-expose.py` speaking for a person -- asks for a name
and answers the calls. The target address never leaves the inner side.

No listener on the host in either role: the port below is reached by
the gateway over the platform network, and the inner role listens on
nothing at all.

Configuration is read, never written, and re-read within a second of
changing -- appctl on the host owns every file:

    /platform-apps/connect.json    keys (hashes) and connectors (offers)
    /secrets/connector-keys.json   the inner side's keys, in the clear
    /secrets/gateway.key           what the gateway presents on /via
    /platform-apps/tenants.json    tenant ids and labels (read only)

What this process writes is state, stream logs and the exposures it holds
(to /state), and one line per decision to the tenant audit log (/audit,
append only -- the posture identity has).
"""
import asyncio
import hashlib
import hmac
import json
import os
import math
import posixpath
import random
import re
import secrets
import struct
import sys
import time
import urllib.parse
from collections import deque

import aiohttp
from aiohttp import web
from yarl import URL

APPS = os.environ.get("CONNECT_APPS", "/platform-apps")
SECRETS = os.environ.get("CONNECT_SECRETS", "/secrets")
STATE = os.environ.get("CONNECT_STATE", "/state")
PORT = int(os.environ.get("CONNECT_PORT", "8000"))
CONF_FILE = os.path.join(APPS, "connect.json")
KEYS_FILE = os.path.join(SECRETS, "connector-keys.json")
GATEWAY_KEY_FILE = os.path.join(SECRETS, "gateway.key")
TENANTS_FILE = os.path.join(APPS, "tenants.json")
EXPOSURES_FILE = os.path.join(STATE, "exposures.json")
AUDIT_LOG = os.environ.get("CONNECT_AUDIT", "/audit/tenant-log.jsonl")
IDENTITY = os.environ.get("CONNECT_IDENTITY", "http://identity:8000").rstrip("/")
CLIENT_FILE = os.environ.get("CONNECT_CLIENT_FILE", os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "client", "oaap-expose.py"))

SUBPROTOCOL = "oaap-connect.1"
WIRE_VERSION = 1
CHUNK = 64 * 1024
DATA, END = 1, 2
HEAD_TIMEOUT = 120          # spec 2.6: no answer within 120 s -> 504
QUEUE_WAIT = 30             # a reader that stalls this long loses its stream
BACKOFF_MAX = 60
LOG_CAP = 5 * 1024 * 1024

GW_KEY = "X-OAAP-Connect-Gateway"
GW_CALLER = "X-OAAP-Connect-Caller"
GW_DEST = "X-OAAP-Connect-Destination"
# exposures (spec 2.8): which name was asked for, and by whom. Set by the
# gateway, never passed on.
GW_HOST = "X-OAAP-Connect-Host"
GW_CLIENT = "X-OAAP-Connect-Client"

# spec 2.8.3
EXPOSE_DEFAULT_TTL = 8 * 3600
EXPOSE_MAX_TTL = 7 * 86400
EXPOSE_PER_OWNER = 10
EXPOSE_PER_NODE = 100
PERSON_TUNNELS = 5
NAME_LEN = 10
NAME_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"
# spec 2.8.4: the brake on a public exposure, RFC-0010's shape
PUBLIC_LIMIT, PUBLIC_WINDOW = 120, 60
# spec 2.8.7: Let's Encrypt's limit per registered domain per week
CERT_WEEK_LIMIT, CERT_WEEK_WARN = 50, 40
SWEEP_EVERY = 5
RECHECK_EVERY = int(os.environ.get("CONNECT_RECHECK", "60"))
SESSION_COOKIE = "oaap_session"
IDENTITY_HEADERS = ("X-OAAP-User", "X-OAAP-Roles", "X-OAAP-User-Id",
                    "X-OAAP-Display-Name", "X-OAAP-Email")
# RFC 7230 6.1 plus the ones a proxy must never pass on. X-Forwarded-*
# would tell the backend where the call came from on the OUTER node --
# nothing it has a use for, and nothing the inner side chose to learn.
#
# `expect` too, measured on oaap-test 2026-09-25: curl sends
# "Expect: 100-continue" for a body over 1 MiB, the gateway already
# answered it -- and passed on, it made the inner client wait for a 100
# from a backend that was waiting for the body. 120 s, then 504. An
# expectation is between two neighbours, like the rest of this list.
HOP = {"connection", "keep-alive", "proxy-authenticate", "expect",
       "proxy-authorization", "te", "trailer", "trailers",
       "transfer-encoding", "upgrade", "host", "content-length",
       "x-forwarded-for", "x-forwarded-proto", "x-forwarded-host",
       GW_KEY.lower(), GW_CALLER.lower(), GW_DEST.lower(),
       GW_HOST.lower(), GW_CLIENT.lower()}
# Spec 2.3: an offer may name the node's front door, never a platform
# service behind it. Re-checked here although appctl refuses them at
# `offer add` -- connect.json is a file, and a file can be edited.
PLATFORM_HOSTS = {"identity", "portal", "store", "twin", "broker", "relay",
                  "connect", "localhost"}
FRONT_DOOR = {"gateway", "oaap-gateway-1"}
# What is only between the gateway and this service and must not be
# handed on to identity. NOT the X-Forwarded-* set: identity needs those
# to build the login's return address and to know the client.
VERIFY_SKIP = {"connection", "keep-alive", "transfer-encoding", "upgrade",
               "content-length", "host", GW_KEY.lower(), GW_HOST.lower(),
               GW_CLIENT.lower(), GW_CALLER.lower(), GW_DEST.lower()}


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f) or {}
    except (OSError, ValueError):
        return {}


def read_text(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def mtime(path):
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return 0


def offer_refusal(to):
    """Why this offer target must not be carried, or ""."""
    try:
        u = urllib.parse.urlsplit(to or "")
        port = u.port
    except ValueError:
        return "not a usable URL"
    if u.scheme not in ("http", "https") or not u.hostname:
        return "an offer is an http:// or https:// URL"
    if u.username or u.password or u.query or u.fragment:
        return "an offer carries no user, query or fragment"
    h = u.hostname.lower()
    if h in PLATFORM_HOSTS or h.startswith("127.") or h in ("::1", "0.0.0.0"):
        return f"'{h}' is a platform service or this machine"
    if re.fullmatch(r"oaap-[a-z0-9-]+-\d+", h) and h not in FRONT_DOOR:
        return f"'{h}' is a platform container"
    if h in FRONT_DOOR and (port or (443 if u.scheme == "https" else 80)) not in (80, 443):
        return "the gateway is offered on its public ports (80, 443) only"
    return ""


def clean_path(raw):
    """(decoded path, error) -- spec 2.6 step 4. `raw` is the percent-
    encoded path as it arrived, without the query."""
    if "\\" in raw:
        return "", "a backslash in the path"
    dec = urllib.parse.unquote(raw)
    if "\\" in dec or "\x00" in dec:
        return "", "a backslash or NUL in the path"
    segs = dec.split("/")
    if any(s in (".", "..") for s in segs):
        return "", "a '.' or '..' segment in the path"
    if not dec.startswith("/"):
        dec = "/" + dec
    return dec, ""


def under_prefix(path, prefix):
    p = (prefix or "").rstrip("/")
    return not p or path == p or path.startswith(p + "/")


# --------------------------------------------------------------- logs

class StreamLog:
    """One line per call, capped, one predecessor kept (spec 2.7)."""

    def __init__(self, name):
        self.path = os.path.join(STATE, "log", name + ".jsonl")

    def write(self, **rec):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            if mtime(self.path) and os.path.getsize(self.path) > LOG_CAP:
                os.replace(self.path, self.path + ".1")
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"time": now_iso(), **rec}) + "\n")
        except OSError as e:
            log(f"stream log {self.path}: {e}")


# -------------------------------------------------------------- frames

def frame(kind, sid, payload=b""):
    return struct.pack(">BI", kind, sid) + payload


def unframe(data):
    if len(data) < 5:
        return None, None, b""
    kind, sid = struct.unpack(">BI", data[:5])
    return kind, sid, data[5:]


class Stream:
    """One call's receiving half: control answers and body chunks."""

    def __init__(self):
        self.head = asyncio.get_running_loop().create_future()
        self.body = asyncio.Queue(maxsize=64)

    async def feed(self, item):
        try:
            await asyncio.wait_for(self.body.put(item), QUEUE_WAIT)
            return True
        except asyncio.TimeoutError:
            return False


async def body_chunks(stream):
    while True:
        item = await stream.body.get()
        if item is None:
            return
        yield item


# ----------------------------------------------------------- the node

class Node:
    def __init__(self):
        self.conf = {}
        self.keys = {}
        self.gateway_key = ""
        self._seen = None
        self.tunnels = {}          # outer: label -> Tunnel
        self.connectors = {}       # inner: label -> Connector
        self.failures = {}         # outer: ip -> deque of times
        self.exposures = {}        # outer: name -> exposure (spec 2.8)
        # inner: label -> {ref: what the outer node answered}. Kept HERE,
        # not on the Connector: a pause throws the Connector away, and the
        # name it was given is what makes a resume the same address. Read
        # back from the last state, so a restart of this process keeps them.
        self.exposed_store = {}
        for label, c in ((read_json(os.path.join(STATE, "state.json")).get("connectors") or {})
                         .items()):
            for ref, v in (c.get("exposures") or {}).items():
                if isinstance(v, dict) and v.get("expired"):
                    self.exposed_store.setdefault(label, {})[ref] = {"expired": v["expired"]}
                elif isinstance(v, dict) and v.get("name"):
                    self.exposed_store.setdefault(label, {})[ref] = {"name": v["name"]}
        self.opened = []           # outer: when names were opened (2.8.7)
        self.brakes = {}           # outer: (name, client) -> deque of times
        self.http = None           # one client session for identity
        self.exposure_log = StreamLog("exposures")
        self._state_dirty = asyncio.Event()

    # --- configuration --------------------------------------------------
    def load(self):
        seen = (mtime(CONF_FILE), mtime(KEYS_FILE), mtime(GATEWAY_KEY_FILE))
        if seen == self._seen:
            return False
        self._seen = seen
        self.conf = read_json(CONF_FILE)
        self.keys = read_json(KEYS_FILE).get("keys") or {}
        self.gateway_key = read_text(GATEWAY_KEY_FILE)
        return True

    def key_records(self):
        return self.conf.get("keys") or {}

    def connector_conf(self, label):
        return (self.conf.get("connectors") or {}).get(label)

    async def watch(self):
        while True:
            try:
                if self.load():
                    await self.reconcile()
            except Exception as e:                       # noqa: BLE001
                log(f"reconcile: {e!r}")
            await asyncio.sleep(1)

    async def reconcile(self):
        # outer: a tunnel whose key is gone (revoked, spec 2.2) closes
        recs = self.key_records()
        for label, t in list(self.tunnels.items()):
            if t.person:
                # a person's key is not in this file (identity holds it, and
                # Tunnel.recheck asks identity) -- found live on oaap-test
                # 2026-09-26: every change to connect.json closed every
                # laptop with "key revoked"
                continue
            r = recs.get(label)
            if not r or r.get("hash") != t.key_hash or r.get("tenant") != t.tenant:
                log(f"tunnel {label}: key revoked -- closing")
                await t.close(4001, "key revoked")
        for label in [l for l in self.exposed_store
                      if l not in (self.conf.get("connectors") or {})]:
            del self.exposed_store[label]          # a removed connector has no names
        # inner: start, stop, restart, re-announce
        want = {}
        for label, c in (self.conf.get("connectors") or {}).items():
            if not c.get("paused") and self.keys.get(label):
                want[label] = (c.get("endpoint", ""), self.keys[label])
        for label, conn in list(self.connectors.items()):
            if label not in want or want[label] != conn.ident:
                await conn.stop()
                del self.connectors[label]
        for label, ident in want.items():
            if label not in self.connectors:
                conn = Connector(self, label, ident)
                self.connectors[label] = conn
                conn.start()
            else:
                await self.connectors[label].announce()
        self.dirty()

    # --- state -----------------------------------------------------------
    def dirty(self):
        self._state_dirty.set()

    def state(self):
        tunnels = {}
        for label, r in self.key_records().items():
            t = self.tunnels.get(label)
            tunnels[label] = {
                "tenant": r.get("tenant", ""),
                "connected": bool(t and not t.closed),
                "since": t.since if t else "",
                "remote": t.remote if t else "",
                "connector": t.connector if t else "",
                "offers": t.offers if t else [],
                "last_seen": t.since if t else "",
            }
        connectors = {}
        for label, c in (self.conf.get("connectors") or {}).items():
            conn = self.connectors.get(label)
            connectors[label] = {
                "exposures": exposure_view(self.exposed_store.get(label) or {}),
                "endpoint": c.get("endpoint", ""),
                "paused": bool(c.get("paused")),
                "key": bool(self.keys.get(label)),
                "connected": bool(conn and conn.connected),
                "since": conn.since if conn else "",
                "last_error": conn.last_error if conn else "",
                "next_attempt": conn.next_attempt if conn else "",
                "offers": sorted((c.get("offers") or {}).keys()),
            }
        zone, scheme = self.zone()
        exposures = {}
        for name, e in self.exposures.items():
            if not self.alive(e):
                continue
            t = self.tunnel_of(e["owner"])
            exposures[name] = {"host": self.host_of(name), "tenant": e["tenant"],
                               "owner": e["owner"], "public": bool(e.get("public")),
                               "opened": iso(e["opened"]), "expires": iso(e["expires"]),
                               "opened_by": e["opened_by"], "calls": e.get("calls", 0),
                               "connected": bool(t)}
        people = [{"user": t.who, "tenant": t.tenant, "since": t.since, "remote": t.remote}
                  for t in self.tunnels.values() if t.person and not t.closed]
        return {"schema": "0.2", "time": now_iso(), "tunnels": tunnels,
                "connectors": connectors, "zone": zone, "scheme": scheme,
                "exposures": exposures, "people": people,
                "certs_week": len(self.opened), "certs_limit": CERT_WEEK_LIMIT}

    async def write_state(self):
        while True:
            await self._state_dirty.wait()
            self._state_dirty.clear()
            try:
                os.makedirs(STATE, exist_ok=True)
                tmp = os.path.join(STATE, "state.json.tmp")
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self.state(), f, indent=2)
                os.replace(tmp, os.path.join(STATE, "state.json"))
            except OSError as e:
                log(f"state: {e}")
            await asyncio.sleep(0.2)

    # --- exposures (spec 2.8) ---------------------------------------------
    def zone(self):
        """(zone, scheme): where a name lives, or ("", "") on a node with
        no external hostname. Written by appctl next to the gateway site,
        so the two cannot name different zones."""
        z = self.conf.get("exposure") or {}
        zone = str(z.get("zone") or "").lower().strip(".")
        return (zone, "http" if z.get("scheme") == "http" else "https") if zone else ("", "")

    def host_of(self, name):
        zone, _ = self.zone()
        return f"{name}.{zone}" if zone else ""

    def tenants(self):
        return read_json(TENANTS_FILE).get("tenants") or {}

    def tenant_id(self, ref):
        """A tenant's id from what a person typed (label or id), or None.
        Identity only takes ids, and the client is meant to say `cls`."""
        ref = (ref or "").strip()
        ts = self.tenants()
        if ref in ts:
            return ref
        low = ref.lower()
        for tid, t in ts.items():
            if str(t.get("label", "")).lower() == low:
                return tid
        return None

    def audit(self, action, tenant, subject, who, role="-", detail="", result="ok"):
        """One line in the tenant audit log (oaap.core.tenant 1.7): the
        same file and the same shape identity and appctl append to. A
        record, not a lock -- a failure to write is reported, and the
        operation goes on."""
        entry = {"when": now_iso(), "who": who or "?", "role": role or "-",
                 "action": action, "tenant": tenant or "",
                 "tenant_label": (self.tenants().get(tenant or "") or {}).get("label", ""),
                 "subject": subject, "result": result}
        if detail:
            entry["detail"] = detail
        try:
            os.makedirs(os.path.dirname(AUDIT_LOG), exist_ok=True)
            with open(AUDIT_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as e:
            log(f"audit log: {e}")

    def load_exposures(self):
        d = read_json(EXPOSURES_FILE)
        self.exposures = {n: e for n, e in (d.get("exposures") or {}).items()
                          if isinstance(e, dict) and e.get("expires", 0) > time.time()}
        self.opened = [t for t in (d.get("opened") or []) if t > time.time() - 7 * 86400]

    def save_exposures(self):
        try:
            os.makedirs(STATE, exist_ok=True)
            tmp = EXPOSURES_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"exposures": self.exposures, "opened": self.opened}, f)
            os.replace(tmp, EXPOSURES_FILE)
        except OSError as e:
            log(f"exposures: {e}")
        self.dirty()

    def alive(self, e):
        return bool(e) and e.get("expires", 0) > time.time()

    def exposure_by_host(self, host):
        """The live exposure a requested host names, or None. The zone is
        checked FIRST and the name must be exactly one label under it --
        `a.b.t.host` and the zone itself are nothing."""
        zone, _ = self.zone()
        host = (host or "").split(":")[0].lower().strip(".")
        if not zone or not host.endswith("." + zone):
            return None
        name = host[:-len(zone) - 1]
        if not re.fullmatch(r"[a-z0-9]{%d}" % NAME_LEN, name):
            return None
        e = self.exposures.get(name)
        return e if self.alive(e) else None

    def tunnel_of(self, owner):
        """The newest connected tunnel of an owner, or None."""
        best = None
        for t in self.tunnels.values():
            if t.owner == owner and not t.closed and (best is None or t.since >= best.since):
                best = t
        return best

    def new_name(self):
        while True:
            name = "".join(secrets.choice(NAME_ALPHABET) for _ in range(NAME_LEN))
            if name not in self.exposures:
                return name

    def closed_names(self):
        return set((self.conf.get("exposure_closed") or {}).keys())

    async def expose(self, t, m):
        """`expose` from a tunnel (spec 2.8.2, rules 2.8.3 in this order)."""
        ref = str(m.get("ref", ""))[:40]

        async def refuse(reason):
            await t.send_json({"t": "expose-refused", "ref": ref, "reason": reason})

        if not re.fullmatch(r"[A-Za-z0-9._-]{1,40}", ref):
            return await refuse("a ref is 1-40 characters of letters, digits and . _ -")
        zone, scheme = self.zone()
        if not zone:
            return await refuse("this node has no external hostname (oaap external set) "
                                "-- there is no zone to expose under")
        public = bool(m.get("public"))
        try:
            # missing means the default; a 0 is a request for no time at all
            ttl = EXPOSE_DEFAULT_TTL if m.get("ttl") is None else int(m["ttl"])
        except (TypeError, ValueError):
            return await refuse("ttl is a number of seconds")
        # The requester sends the time that is LEFT (a reconnect, a request
        # that waited a second for the connector to dial in), so the
        # minimum of a request -- one minute -- is the requester's own
        # rule (`oaap connector expose`, the client). Found live on
        # oaap-test 2026-09-26: `--ttl 60s` arrived as 59 and was refused.
        if ttl < 1:
            return await refuse("ttl is a number of seconds, at least 1")
        ttl = min(ttl, EXPOSE_MAX_TTL)
        now = time.time()
        resume = str(m.get("resume") or "")
        if resume and resume in self.closed_names():
            return await refuse("the operator of this node closed this exposure "
                                "(oaap connect exposure close) -- it does not come back "
                                "under this request")
        e = self.exposures.get(resume) if resume else None
        if (e and self.alive(e) and e["owner"] == t.owner and e["ref"] == ref
                and resume not in self.closed_names()):
            if bool(e.get("public")) != public:
                return await refuse("this name was opened as "
                                    + ("public" if e.get("public") else "login-protected")
                                    + " -- open a new one to change that")
            # a reconnecting client sends what is LEFT of its time: only a
            # later end than the one held is an extension (never shorter)
            if now + ttl > e["expires"] + 30:
                e["expires"] = now + ttl
                self.audit("exposure.extend", e["tenant"], self.host_of(resume), t.who,
                           t.role, f"until {iso(e['expires'])}")
            self.save_exposures()
            return await t.send_json(self.answer(ref, resume, e))
        mine = sum(1 for x in self.exposures.values() if x["owner"] == t.owner and self.alive(x))
        if mine >= EXPOSE_PER_OWNER:
            return await refuse(f"at most {EXPOSE_PER_OWNER} exposures per tunnel")
        if sum(1 for x in self.exposures.values() if self.alive(x)) >= EXPOSE_PER_NODE:
            return await refuse(f"this node holds {EXPOSE_PER_NODE} exposures already")
        name = self.new_name()
        e = {"name": name, "tenant": t.tenant, "owner": t.owner, "ref": ref,
             "public": public, "opened": now, "expires": now + ttl,
             "opened_by": t.who, "calls": 0}
        self.exposures[name] = e
        self.opened.append(now)
        self.save_exposures()
        self.audit("exposure.open", t.tenant, self.host_of(name), t.who, t.role,
                   ("public" if public else "login") + f", until {iso(e['expires'])}")
        log(f"exposure {name}: opened by {t.who} on {t.label}")
        await t.send_json(self.answer(ref, name, e))

    def answer(self, ref, name, e):
        zone, scheme = self.zone()
        host = f"{name}.{zone}"
        return {"t": "exposed", "ref": ref, "name": name, "host": host,
                "url": f"{scheme}://{host}/", "expires": iso(e["expires"]),
                "public": bool(e.get("public"))}

    async def unexpose(self, t, m):
        ref = str(m.get("ref", ""))[:40]
        for name, e in list(self.exposures.items()):
            if e["owner"] == t.owner and e["ref"] == ref:
                await self.end_exposure(name, "exposure.close", t.who, t.role, "closed by its owner",
                                        tell=False)

    async def end_exposure(self, name, action, who, role, detail, reason="closed", tell=True):
        e = self.exposures.pop(name, None)
        if not e:
            return
        self.save_exposures()
        self.audit(action, e["tenant"], self.host_of(name), who, role, detail)
        log(f"exposure {name}: {detail}")
        t = self.tunnel_of(e["owner"])
        if tell and t:
            try:
                await t.send_json({"t": "expired", "ref": e["ref"], "reason": reason})
            except Exception:                            # noqa: BLE001
                pass

    async def end_owner(self, owner, detail):
        for name, e in list(self.exposures.items()):
            if e["owner"] == owner:
                await self.end_exposure(name, "exposure.close", "system", "-", detail,
                                        reason="revoked")

    async def sweep(self):
        """Spec 2.8.6: expiry, the operator's `close`, a revoked key."""
        while True:
            await asyncio.sleep(SWEEP_EVERY)
            try:
                await self.sweep_once()
            except Exception as e:                       # noqa: BLE001
                log(f"sweep: {e!r}")

    async def sweep_once(self):
        now = time.time()
        closed = self.closed_names()
        recs = self.key_records()
        for name, e in list(self.exposures.items()):
            if e["expires"] <= now:
                await self.end_exposure(name, "exposure.expire", "system", "-",
                                        "its time ran out", reason="ttl")
            elif name in closed:
                await self.end_exposure(name, "exposure.close", "operator", "server_admin",
                                        "closed by the operator of this node")
            elif e["owner"].startswith("c:") and e["owner"][2:] not in recs:
                await self.end_exposure(name, "exposure.close", "system", "-",
                                        "the connect key was revoked", reason="revoked")
        self.opened = [t for t in self.opened if t > now - 7 * 86400]
        for ip, q in list(self.failures.items()):
            if not q or q[-1] < now - 300:
                del self.failures[ip]

    def brake(self, name, client):
        """True when this client has had its share of a public exposure
        (spec 2.8.4). Bounded: the table cannot grow past a few thousand
        addresses however hard somebody knocks."""
        key = (name, client or "?")
        q = self.brakes.setdefault(key, deque(maxlen=PUBLIC_LIMIT + 1))
        cut = time.time() - PUBLIC_WINDOW
        while q and q[0] < cut:
            q.popleft()
        if len(q) >= PUBLIC_LIMIT:
            return True
        q.append(time.time())
        if len(self.brakes) > 5000:
            for k in [k for k, v in self.brakes.items() if not v or v[-1] < cut][:1000]:
                del self.brakes[k]
        return False

    async def identity(self, path, headers, params=None):
        """One call to identity, for a visitor (2.8.4) or a laptop's key
        (2.8.5). Returns (status, headers, body)."""
        if self.http is None:
            self.http = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10), auto_decompress=False)
        async with self.http.get(IDENTITY + path, headers=headers, params=params,
                                 allow_redirects=False) as r:
            return r.status, r.headers.copy(), await r.read()

    async def handle_client(self, request):
        """The laptop client, for download (spec 2.8.5). Public, exact, a
        file with no secret in it."""
        try:
            with open(CLIENT_FILE, "rb") as f:
                body = f.read()
        except OSError:
            return text(404, "This node has no client to offer.")
        return web.Response(body=body, content_type="text/x-python", charset="utf-8",
                            headers={"Content-Disposition": 'attachment; filename="oaap-expose.py"',
                                     "Cache-Control": "no-store"})

    def _gateway_only(self, request):
        presented = request.headers.get(GW_KEY, "")
        return bool(self.gateway_key) and hmac.compare_digest(presented, self.gateway_key)

    async def handle_expose_verify(self, request):
        """`forward_auth` target of the zone site (spec 2.8.4): may this
        request pass? 204 with the five identity headers -- empty for a
        public exposure -- or the answer identity gave, unchanged."""
        if not self._gateway_only(request):
            return web.Response(status=403, text="denied\n")
        e = self.exposure_by_host(request.headers.get(GW_HOST, ""))
        if not e:
            return text(404, "There is no such address: this name is not a live exposure "
                             "(it never existed, or its time ran out).")
        if e.get("public"):
            if self.brake(e["name"], request.headers.get(GW_CLIENT, "")):
                return web.Response(status=429, text="Too many requests\n",
                                    headers={"Retry-After": str(PUBLIC_WINDOW)})
            return web.Response(status=204, headers={h: "" for h in IDENTITY_HEADERS})
        # everything the gateway sent, so identity sees the same request a
        # generated app site would show it (cookie, the original address
        # for the login's return target, the client) -- minus what is
        # only between the gateway and this service
        fwd = {k: v for k, v in request.headers.items() if k.lower() not in VERIFY_SKIP}
        status, headers, body = await self.identity(
            "/verify", fwd, params={"tenant": e["tenant"]})
        if status == 204:
            return web.Response(status=204, headers={h: headers.get(h, "") for h in IDENTITY_HEADERS})
        keep = {k: v for k, v in headers.items()
                if k.lower() in ("location", "www-authenticate", "retry-after",
                                 "cache-control", "content-type")}
        return web.Response(status=status, body=body, headers=keep)

    async def handle_exposed(self, request):
        """The call itself (spec 2.8.4), after the verify above said yes."""
        if not self._gateway_only(request):
            return web.Response(status=403, text="denied\n")
        e = self.exposure_by_host(request.headers.get(GW_HOST, ""))
        if not e:
            return text(404, "There is no such address: this name is not a live exposure "
                             "(it never existed, or its time ran out).")
        if request.headers.get("Upgrade"):
            return text(501, "A WebSocket through the tunnel is not part of "
                             "oaap.net.connector 0.2.")
        t = self.tunnel_of(e["owner"])
        if not t:
            return text(502, "The machine behind this address is not connected right now "
                             "-- it dials out again by itself; the address stays until its "
                             "time runs out.")
        visitor = request.headers.get("X-OAAP-User", "") or "public"
        e["calls"] = e.get("calls", 0) + 1
        self.dirty()                # the count is in the state the CLI and the portal read
        return await t.call_exposure(request, e, visitor)

    # --- outer: a person's tunnel (spec 2.8.5) --------------------------------
    async def handle_person(self, request, key, ip):
        want = self.tenant_id(request.headers.get("X-OAAP-Tenant", ""))
        try:
            status, headers, _body = await self.identity(
                "/verify", {"Authorization": "Bearer " + key, "X-Forwarded-For": ip},
                params={"tenant": want or "unknown"})
        except (aiohttp.ClientError, asyncio.TimeoutError) as ex:
            return text(502, f"The node could not ask its identity service: {type(ex).__name__}")
        if status == 429:
            return web.Response(status=429, text="too many attempts -- wait a minute\n")
        if status == 401:
            self._failed(ip)
            return web.Response(status=401, text="denied\n")
        roles = [r for r in (headers.get("X-OAAP-Roles") or "").split(",") if r]
        if status != 204 or want is None:
            return text(403, "This key may not open an exposure for that tenant "
                             "(a key of another tenant, or an unknown tenant).")
        if "tenant_admin" not in roles:
            return text(403, "Only a tenant_admin may open an exposure from a laptop "
                             "(spec 2.8.5): this key's principal is not one.")
        user = headers.get("X-OAAP-User", "") or "?"
        owner = f"p:{user}:{want}"
        if sum(1 for x in self.tunnels.values() if x.owner == owner and not x.closed) >= PERSON_TUNNELS:
            return text(429, f"{user} holds {PERSON_TUNNELS} tunnels already.")
        label = "p-" + secrets.token_hex(4)
        ws = web.WebSocketResponse(protocols=(SUBPROTOCOL,), heartbeat=20,
                                   max_msg_size=CHUNK + 1024)
        await ws.prepare(request)
        t = Tunnel(self, label, want, "", ws, ip)
        t.person, t.owner, t.who, t.role = True, owner, user, "tenant_admin"
        t.key = key                 # in memory, for the life of this tunnel only
        self.tunnels[label] = t
        log(f"person tunnel {label}: {user} of {want} from {ip}")
        self.dirty()
        recheck = asyncio.ensure_future(t.recheck())
        try:
            await t.run()
        finally:
            recheck.cancel()
            if self.tunnels.get(label) is t:
                del self.tunnels[label]
            t.fail_all(502, "the tunnel closed during the call")
            log(f"person tunnel {label}: closed")
            self.dirty()
        return ws

    # --- outer: accepting tunnels ---------------------------------------
    def _client_ip(self, request):
        fwd = request.headers.get("X-Forwarded-For", "")
        return (fwd.split(",")[0].strip() if fwd else request.remote) or "?"

    def _blocked(self, ip):
        q = self.failures.get(ip)
        if not q:
            return False
        cut = time.time() - 300
        while q and q[0] < cut:
            q.popleft()
        # five failures in five minutes -> one attempt per minute
        return len(q) >= 5 and time.time() - q[-1] < 60

    def _failed(self, ip):
        self.failures.setdefault(ip, deque(maxlen=20)).append(time.time())

    async def handle_tunnel(self, request):
        ip = self._client_ip(request)
        if self._blocked(ip):
            return web.Response(status=429, text="too many attempts -- wait a minute\n")
        auth = request.headers.get("Authorization", "")
        presented = auth[7:].strip() if auth.startswith("Bearer ") else ""
        if presented.startswith("oaapk_"):
            # a person's API key (spec 2.8.5): may expose, nothing else
            return await self.handle_person(request, presented, ip)
        h = hashlib.sha256(presented.encode()).hexdigest() if presented else ""
        label = rec = None
        for lab, r in self.key_records().items():
            if h and hmac.compare_digest(r.get("hash", ""), h):
                label, rec = lab, r
        if not rec:
            self._failed(ip)
            # one answer for every failure -- no hint which keys exist
            return web.Response(status=401, text="denied\n")
        ws = web.WebSocketResponse(protocols=(SUBPROTOCOL,), heartbeat=20,
                                   max_msg_size=CHUNK + 1024)
        await ws.prepare(request)
        old = self.tunnels.get(label)
        t = Tunnel(self, label, rec.get("tenant", ""), h, ws, ip)
        t.owner, t.who = f"c:{label}", f"connector:{label}"
        self.tunnels[label] = t
        if old:
            # spec 2.4: one tunnel per key, the NEW one wins -- a
            # connector that lost its network must not be locked out by
            # its own ghost
            await old.close(4000, "replaced by a new connection")
        log(f"tunnel {label}: connected from {ip}")
        self.dirty()
        try:
            await t.run()
        finally:
            if self.tunnels.get(label) is t:
                del self.tunnels[label]
            t.fail_all(502, "the tunnel closed during the call")
            log(f"tunnel {label}: closed")
            self.dirty()
        return ws

    async def handle_via(self, request):
        presented = request.headers.get(GW_KEY, "")
        if not self.gateway_key or not hmac.compare_digest(presented, self.gateway_key):
            # spec 2.5: nothing is looked up for a caller that is not
            # the gateway
            return web.Response(status=403, text="denied\n")
        label = request.match_info["tunnel"]
        offer = request.match_info["offer"]
        dest = request.headers.get(GW_DEST, "")
        caller = request.headers.get(GW_CALLER, "")
        dest_tenant = dest.split("/", 1)[0]
        rec = self.key_records().get(label)
        if not rec or not dest_tenant or rec.get("tenant") != dest_tenant:
            # a destination of another tenant cannot select this tunnel
            # (spec 2.2, 2.5) -- answered EXACTLY like a revoked one, so
            # the answer says nothing about another tenant's tunnels.
            # 502 for both: measured on oaap-test, a revoked key used to
            # get 403 here while `oaap connect key revoke` promised 502.
            return text(502, f"There is no tunnel '{label}' for this destination on "
                             "this node -- its key was revoked, or never issued "
                             "for this tenant (oaap connect list).")
        t = self.tunnels.get(label)
        if not t or t.closed:
            return text(502, f"The tunnel '{label}' is not connected -- the inner "
                             "node has not dialled in, is paused, or its key was "
                             "revoked (oaap connect list).")
        if request.headers.get("Upgrade"):
            return text(501, "A WebSocket through the tunnel is not part of "
                             "oaap.net.connector 0.1.")
        return await t.call(request, offer, caller, dest)


def text(status, msg):
    return web.Response(status=status, text=msg + "\n",
                        content_type="text/plain")


class Tunnel:
    """Outer side of one tunnel."""

    def __init__(self, node, label, tenant, key_hash, ws, remote):
        self.node, self.label, self.tenant = node, label, tenant
        self.key_hash, self.ws, self.remote = key_hash, ws, remote
        self.since = now_iso()
        self.offers = []
        self.connector = ""
        self.streams = {}
        self.next_id = 1
        self.closed = False
        self.send_lock = asyncio.Lock()
        self.logger = StreamLog("tunnel-" + label)
        # exposures (spec 2.8): whose tunnel this is. A connector key's
        # tunnel is `c:<label>`; a person's is `p:<user>:<tenant>`, and
        # may expose and nothing else (`person`).
        self.owner = ""
        self.who = ""
        self.role = "-"
        self.person = False
        self.key = ""

    async def send_json(self, obj):
        async with self.send_lock:
            await self.ws.send_str(json.dumps(obj))

    async def send_bytes(self, b):
        async with self.send_lock:
            await self.ws.send_bytes(b)

    async def close(self, code, why):
        self.closed = True
        try:
            await self.ws.close(code=code, message=why.encode())
        except Exception:                                # noqa: BLE001
            pass

    async def recheck(self):
        """A person's tunnel is a credential held open. RFC-0027 makes a
        revocation count on the very next request; a tunnel has no
        request, so it asks again -- and a key that no longer holds ends
        the tunnel and the exposures its owner opened (spec 2.8.6)."""
        while not self.closed:
            await asyncio.sleep(RECHECK_EVERY)
            try:
                status, headers, _ = await self.node.identity(
                    "/verify", {"Authorization": "Bearer " + self.key,
                                "X-Forwarded-For": self.remote},
                    params={"tenant": self.tenant})
            except (aiohttp.ClientError, asyncio.TimeoutError):
                continue            # identity being down is not a revocation
            roles = (headers.get("X-OAAP-Roles") or "").split(",")
            if status in (401, 403) or (status == 204 and "tenant_admin" not in roles):
                log(f"person tunnel {self.label}: key no longer holds -- closing")
                await self.node.end_owner(self.owner, "the key was revoked or lost its role")
                await self.close(4001, "key revoked")
                return

    def fail_all(self, status, reason):
        for s in self.streams.values():
            if not s.head.done():
                s.head.set_result({"t": "error", "status": status, "reason": reason})
            try:
                s.body.put_nowait(None)
            except asyncio.QueueFull:
                pass

    async def run(self):
        async for msg in self.ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    m = json.loads(msg.data)
                except ValueError:
                    continue
                t = m.get("t")
                if t in ("hello", "offers"):
                    if not self.person:
                        # spec 2.8.5: a person's tunnel has no offers
                        self.offers = _offers(m.get("offers"))
                    if t == "hello":
                        self.connector = str(m.get("connector", ""))[:40]
                    self.node.dirty()
                elif t == "expose":
                    await self.node.expose(self, m)
                elif t == "unexpose":
                    await self.node.unexpose(self, m)
                elif t in ("head", "error"):
                    s = self.streams.get(m.get("id"))
                    if s and not s.head.done():
                        s.head.set_result(m)
                elif t == "cancel":
                    s = self.streams.get(m.get("id"))
                    if s:
                        if not s.head.done():
                            s.head.set_result({"t": "error", "status": 502,
                                               "reason": "the inner side cancelled the call"})
                        await s.feed(None)
            elif msg.type == aiohttp.WSMsgType.BINARY:
                kind, sid, payload = unframe(msg.data)
                s = self.streams.get(sid)
                if not s:
                    continue
                if not await s.feed(payload if kind == DATA else None):
                    self.streams.pop(sid, None)
                    await self.send_json({"t": "cancel", "id": sid})
            elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                break
        self.closed = True

    async def call(self, request, offer, caller, dest):
        if self.person:
            # a person's tunnel is nobody's `via` target (spec 2.8.5)
            return text(502, f"There is no tunnel '{self.label}' for this destination "
                             "on this node.")
        if offer not in {o["name"] for o in self.offers}:
            self.logger.write(offer=offer, caller=caller, destination=dest,
                              method=request.method, path=request.rel_url.path,
                              status=404, result="not offered")
            return text(404, f"The inner node does not offer '{offer}' on this "
                             "tunnel (oaap connect offers " + self.label + ").")
        return await self.carry(request, f"/via/{self.label}/{offer}",
                                {"offer": offer, "destination": dest}, caller,
                                self.logger, {"offer": offer, "destination": dest})

    async def call_exposure(self, request, e, visitor):
        """A visitor's call to an exposure (spec 2.8.4). What the target
        is, is the inner side's business: only the `ref` is sent."""
        return await self.carry(request, "/exposed", {"exposure": e["ref"]}, visitor,
                                self.node.exposure_log,
                                {"name": e["name"], "tenant": e["tenant"]},
                                exposure=True)

    async def carry(self, request, prefix, extra, caller, logger, logrec, exposure=False):
        sid = self.next_id
        self.next_id += 1
        s = Stream()
        self.streams[sid] = s
        raw = request.raw_path
        rest = raw[len(prefix):] if raw.startswith(prefix) else "/"
        if not rest.startswith("/"):
            rest = "/" + rest
        has_body = bool(request.body_exists)
        headers = [[k, v] for k, v in request.headers.items() if k.lower() not in HOP]
        if exposure:
            # the target is not a platform app: what only the platform may
            # see does not go to it (spec 2.8.4) -- the session cookie
            # (valid on the whole node, so a target that kept it could BE
            # the visitor) and a platform API key
            headers = [[k, v] for k, v in (_strip_platform_credentials(h) for h in headers) if v]
        if request.headers.get("Content-Length"):
            headers.append(["Content-Length", request.headers["Content-Length"]])
        t0 = time.monotonic()
        sent = received = 0
        status, result = 502, ""
        pump = None
        try:
            await self.send_json({"t": "open", "id": sid, **extra,
                                  "method": request.method, "path": rest,
                                  "headers": headers, "body": has_body,
                                  "caller": caller})

            async def pump_body():
                nonlocal sent
                if has_body:
                    async for chunk in request.content.iter_chunked(CHUNK):
                        sent += len(chunk)
                        await self.send_bytes(frame(DATA, sid, chunk))
                await self.send_bytes(frame(END, sid))
            pump = asyncio.ensure_future(pump_body())
            try:
                head = await asyncio.wait_for(asyncio.shield(s.head), HEAD_TIMEOUT)
            except asyncio.TimeoutError:
                status, result = 504, "no answer"
                await self.send_json({"t": "cancel", "id": sid})
                return text(504, "The backend behind the tunnel did not answer in time.")
            if head.get("t") == "error":
                status, result = int(head.get("status") or 502), head.get("reason", "")
                return text(status, head.get("reason") or "refused by the inner node")
            status, result = int(head.get("status") or 502), "ok"
            resp = web.StreamResponse(status=status)
            for k, v in head.get("headers") or []:
                if k.lower() in HOP:
                    continue
                if (exposure and k.lower() == "set-cookie"
                        and v.split("=", 1)[0].strip() == SESSION_COOKIE):
                    continue        # a target cannot set the node's session
                resp.headers.add(k, v)
            for k, v in head.get("headers") or []:
                if k.lower() == "content-length":
                    resp.content_length = int(v)
            await resp.prepare(request)
            while True:
                chunk = await s.body.get()
                if chunk is None:
                    break
                received += len(chunk)
                await resp.write(chunk)
            await resp.write_eof()
            return resp
        except (asyncio.CancelledError, ConnectionResetError):
            result = "caller went away"
            try:
                await self.send_json({"t": "cancel", "id": sid})
            except Exception:                            # noqa: BLE001
                pass
            raise
        finally:
            if pump and not pump.done():
                pump.cancel()
            self.streams.pop(sid, None)
            logger.write(**logrec, caller=caller, method=request.method,
                         path=urllib.parse.urlsplit(rest).path,
                         status=status, sent=sent, received=received,
                         ms=int((time.monotonic() - t0) * 1000), result=result)


def exposure_view(exposed):
    return {ref: {k: v for k, v in x.items()
                  if k in ("name", "host", "url", "expires", "public", "error", "expired")}
            for ref, x in exposed.items()}


def _strip_platform_credentials(pair):
    """(name, value) with the node's own credentials taken out (spec
    2.8.4): the session cookie, and a platform API key. The value comes
    back empty when nothing else was in it."""
    k, v = pair
    low = k.lower()
    if low == "cookie":
        rest = [c for c in v.split(";") if c.strip().split("=", 1)[0] != SESSION_COOKIE]
        return k, ";".join(rest).strip()
    if low == "authorization" and v.strip().lower().startswith("bearer oaapk_"):
        return k, ""
    return k, v


def _offers(raw):
    out = []
    for o in raw or []:
        if isinstance(o, dict) and o.get("name"):
            out.append({"name": str(o["name"])[:40], "kind": str(o.get("kind", "http"))[:10]})
    return sorted(out, key=lambda o: o["name"])


# -------------------------------------------------------- inner side

class Connector:
    def __init__(self, node, label, ident):
        self.node, self.label, self.ident = node, label, ident
        self.endpoint, self.key = ident
        self.task = None
        self.ws = None
        self.connected = False
        self.since = ""
        self.last_error = ""
        self.next_attempt = ""
        self.streams = {}          # id -> (Stream, task)
        self.send_lock = asyncio.Lock()
        self.logger = StreamLog("connector-" + label)
        self._announced = None
        # exposures (spec 2.8): what the outer node answered, per ref, and
        # what has been asked for on THIS connection. The names outlive a
        # restart of this process: they are read back from the last state.
        self.exposed = node.exposed_store.setdefault(label, {})
        self.sent = {}

    def conf(self):
        return self.node.connector_conf(self.label) or {}

    def offers(self):
        return self.conf().get("offers") or {}

    def start(self):
        self.task = asyncio.ensure_future(self.run())

    async def stop(self):
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    async def send_json(self, obj):
        async with self.send_lock:
            await self.ws.send_str(json.dumps(obj))

    async def send_bytes(self, b):
        async with self.send_lock:
            await self.ws.send_bytes(b)

    def offer_list(self):
        return [{"name": n, "kind": "http"} for n in sorted(self.offers())]

    def wanted(self):
        """The exposures the operator asked for and whose time has not run
        out -- appctl writes them with an ABSOLUTE end, so a restart or a
        reconnect never gives one a fresh lease."""
        now = time.time()
        return {ref: w for ref, w in (self.conf().get("exposures") or {}).items()
                if isinstance(w, dict) and float(w.get("expires_at") or 0) > now}

    def exposure_view(self):
        return exposure_view(self.exposed)

    async def sync_exposures(self, fresh=False):
        """Ask for what is wanted and let go of what is not (spec 2.8.2)."""
        if not self.connected or self.ws is None:
            return
        if fresh:
            self.sent = {}
        want = self.wanted()
        try:
            for ref, w in sorted(want.items()):
                sig = (float(w["expires_at"]), bool(w.get("public")))
                if self.sent.get(ref) == sig:
                    continue
                if (self.exposed.get(ref) or {}).get("expired"):
                    # ended by the outer node (its operator closed it, its time
                    # ran out, its key went): that is a decision of the OTHER
                    # side, and asking again after a reconnect would undo it.
                    # Found live on oaap-test 2026-09-26: after a restart the
                    # closed exposure came back under a new name. A new one is
                    # `unexpose` and `expose` again.
                    continue
                self.sent[ref] = sig
                await self.send_json({"t": "expose", "ref": ref,
                                      "ttl": max(1, math.ceil(sig[0] - time.time())),
                                      "public": sig[1],
                                      "resume": (self.exposed.get(ref) or {}).get("name", "")})
            for ref in [r for r in self.sent if r not in want]:
                del self.sent[ref]
                self.exposed.pop(ref, None)
                await self.send_json({"t": "unexpose", "ref": ref})
        except Exception:                                # noqa: BLE001
            pass
        self.node.dirty()

    async def announce(self):
        """Names and kinds, never addresses (spec 2.4) -- sent again
        whenever the list changes."""
        lst = self.offer_list()
        if self.connected and lst != self._announced:
            self._announced = lst
            try:
                await self.send_json({"t": "offers", "offers": lst})
            except Exception:                            # noqa: BLE001
                pass
        await self.sync_exposures()

    async def run(self):
        delay = 1
        url = self.endpoint.rstrip("/") + "/connect/tunnel"
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=20)
        async with aiohttp.ClientSession(trust_env=True, timeout=timeout,
                                         auto_decompress=False) as http:
            while True:
                try:
                    async with http.ws_connect(
                            url, protocols=(SUBPROTOCOL,), heartbeat=20,
                            max_msg_size=CHUNK + 1024,
                            headers={"Authorization": "Bearer " + self.key}) as ws:
                        self.ws = ws
                        self.connected, self.since = True, now_iso()
                        self.last_error, self.next_attempt = "", ""
                        delay = 1
                        self._announced = self.offer_list()
                        await self.send_json({"t": "hello", "connector": self.label,
                                              "offers": self._announced,
                                              "version": WIRE_VERSION})
                        log(f"connector {self.label}: connected to {url}")
                        self.node.dirty()
                        await self.sync_exposures(fresh=True)
                        await self.serve(ws, http)
                        why = f"closed by the outer node ({ws.close_code})"
                        if ws.close_code == 4001:
                            why = "the outer node revoked the key"
                        elif ws.close_code == 4000:
                            why = "replaced by another connection with the same key"
                        self.last_error = why
                except asyncio.CancelledError:
                    self._drop()
                    raise
                except aiohttp.WSServerHandshakeError as e:
                    self.last_error = ("the outer node refused the key (401)"
                                       if e.status == 401 else
                                       f"the outer node answered {e.status}")
                except Exception as e:                   # noqa: BLE001
                    self.last_error = f"{type(e).__name__}: {e}"[:300]
                self._drop()
                wait = min(delay, BACKOFF_MAX) * (0.8 + 0.4 * random.random())
                self.next_attempt = time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + wait))
                log(f"connector {self.label}: {self.last_error} -- again in {wait:.0f}s")
                self.node.dirty()
                await asyncio.sleep(wait)
                delay = min(delay * 2, BACKOFF_MAX)

    def _drop(self):
        self.connected = False
        self.ws = None
        self.sent = {}
        for s, task in self.streams.values():
            task.cancel()
        self.streams.clear()
        self.node.dirty()

    async def serve(self, ws, http):
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    m = json.loads(msg.data)
                except ValueError:
                    continue
                if m.get("t") == "open":
                    sid = m.get("id")
                    s = Stream()
                    task = asyncio.ensure_future(self.handle_open(m, s, http))
                    self.streams[sid] = (s, task)
                    task.add_done_callback(lambda _t, sid=sid: self.streams.pop(sid, None))
                elif m.get("t") == "cancel":
                    entry = self.streams.pop(m.get("id"), None)
                    if entry:
                        entry[1].cancel()
                elif m.get("t") == "exposed":
                    self.exposed[str(m.get("ref", ""))] = {
                        k: m.get(k) for k in ("name", "host", "url", "expires", "public")}
                    log(f"connector {self.label}: exposure {m.get('ref')} is {m.get('url')}")
                    self.node.dirty()
                elif m.get("t") == "expose-refused":
                    ref = str(m.get("ref", ""))
                    self.exposed[ref] = {"error": str(m.get("reason", ""))[:300]}
                    self.sent.pop(ref, None)     # a changed request may try again
                    log(f"connector {self.label}: exposure {ref} refused: {m.get('reason')}")
                    self.node.dirty()
                elif m.get("t") == "expired":
                    ref = str(m.get("ref", ""))
                    self.exposed[ref] = {"expired": str(m.get("reason", ""))[:40]}
                    self.node.dirty()
            elif msg.type == aiohttp.WSMsgType.BINARY:
                kind, sid, payload = unframe(msg.data)
                entry = self.streams.get(sid)
                if entry:
                    if not await entry[0].feed(payload if kind == DATA else None):
                        entry[1].cancel()
            elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                break

    def check(self, m):
        """Spec 2.6, in order, against the CURRENT configuration.
        Returns (url, headers, None) or (None, None, (status, reason))."""
        c = self.conf()
        if c.get("paused"):
            return None, None, (503, "the connector is paused")
        if m.get("exposure") is not None:
            ref = str(m.get("exposure"))
            w = self.wanted().get(ref)
            if not w:
                return None, None, (404, "the inner node no longer holds this exposure")
            name, o = f"exposure {ref}", {"to": w.get("target", "")}
            why = offer_refusal(o["to"])
            if why:
                return None, None, (403, f"the target is refused on the inner node: {why}")
        else:
            name = m.get("offer", "")
            o = self.offers().get(name)
            if not o:
                return None, None, (404, f"'{name}' is not offered by the inner node")
            why = offer_refusal(o.get("to", ""))
            if why:
                return None, None, (403, f"the offer '{name}' is refused on the inner node: {why}")
        method = str(m.get("method", "")).upper()
        allowed = [x.upper() for x in o.get("methods") or []]
        if allowed and method not in allowed:
            return None, None, (405, f"'{name}' does not accept {method}")
        raw = str(m.get("path") or "/")
        raw_path, _, query = raw.partition("?")
        path, err = clean_path(raw_path)
        if err:
            return None, None, (403, f"refused: {err}")
        if not under_prefix(path, o.get("path", "")):
            return None, None, (403, f"'{name}' is offered below {o.get('path')} only")
        base = urllib.parse.urlsplit(o["to"])
        target = (f"{base.scheme}://{base.netloc}{base.path.rstrip('/')}"
                  f"{raw_path if raw_path.startswith('/') else '/' + raw_path}"
                  + (f"?{query}" if query else ""))
        headers = [(k, v) for k, v in m.get("headers") or []
                   if k.lower() not in HOP or k.lower() == "content-length"]
        return URL(target, encoded=True), headers, None

    async def handle_open(self, m, s, http):
        sid = m.get("id")
        t0 = time.monotonic()
        sent = received = 0
        status, result = 502, ""
        url, headers, refusal = self.check(m)
        try:
            if refusal:
                status, result = refusal
                # drain what the outer side already sends for this call
                await self.send_json({"t": "error", "id": sid, "status": status,
                                      "reason": result})
                return
            has_body = bool(m.get("body"))

            async def body():
                nonlocal received
                async for chunk in body_chunks(s):
                    received += len(chunk)
                    yield chunk
            try:
                async with http.request(
                        m.get("method", "GET"), url, headers=headers,
                        data=body() if has_body else None,
                        allow_redirects=False,
                        timeout=aiohttp.ClientTimeout(total=None, sock_connect=20,
                                                      sock_read=HEAD_TIMEOUT)) as r:
                    status, result = r.status, "ok"
                    await self.send_json({"t": "head", "id": sid, "status": r.status,
                                          "headers": [[k, v] for k, v in r.headers.items()
                                                      if k.lower() not in HOP
                                                      or k.lower() == "content-length"]})
                    async for chunk in r.content.iter_chunked(CHUNK):
                        sent += len(chunk)
                        await self.send_bytes(frame(DATA, sid, chunk))
                    await self.send_bytes(frame(END, sid))
            except asyncio.TimeoutError:
                status, result = 504, "the backend did not answer in time"
                await self.send_json({"t": "error", "id": sid, "status": 504,
                                      "reason": result})
            except aiohttp.ClientError as e:
                if result == "ok":
                    # the head went out already -- all we can do is end it
                    result = f"broken mid-answer: {type(e).__name__}"
                    await self.send_json({"t": "cancel", "id": sid})
                else:
                    status = 502
                    result = f"the backend is not reachable: {type(e).__name__}"
                    await self.send_json({"t": "error", "id": sid, "status": 502,
                                          "reason": result})
        except asyncio.CancelledError:
            result = result or "cancelled"
            raise
        except Exception as e:                           # noqa: BLE001
            result = f"{type(e).__name__}: {e}"[:200]
        finally:
            self.logger.write(offer=m.get("offer") or f"exposure:{m.get('exposure', '')}",
                              caller=m.get("caller", ""),
                              destination=m.get("destination", ""),
                              method=m.get("method", ""),
                              path=urllib.parse.urlsplit(str(m.get("path") or "/")).path,
                              status=status, sent=sent, received=received,
                              ms=int((time.monotonic() - t0) * 1000), result=result)


# -------------------------------------------------------------- main

def make_app(node):
    app = web.Application(client_max_size=1024 ** 3)
    app.router.add_get("/connect/tunnel", node.handle_tunnel)
    app.router.add_route("*", r"/via/{tunnel:[a-z0-9-]+}/{offer:[a-z0-9-]+}{rest:(/.*)?}",
                         node.handle_via)
    app.router.add_get("/connect/client", node.handle_client)
    app.router.add_get("/exposure/verify", node.handle_expose_verify)
    app.router.add_route("*", r"/exposed{rest:(/.*)?}", node.handle_exposed)
    app.router.add_get("/healthz", lambda _r: web.Response(text="ok\n"))
    return app


async def main():
    node = Node()
    node.load()
    node.load_exposures()
    await node.reconcile()
    runner = web.AppRunner(make_app(node), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log(f"connect: listening on :{PORT} (platform network only)")
    await asyncio.gather(node.watch(), node.write_state(), node.sweep())


if __name__ == "__main__":
    asyncio.run(main())
