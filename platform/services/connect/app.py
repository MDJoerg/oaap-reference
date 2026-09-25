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

No listener on the host in either role: the port below is reached by
the gateway over the platform network, and the inner role listens on
nothing at all.

Configuration is read, never written, and re-read within a second of
changing -- appctl on the host owns every file:

    /platform-apps/connect.json    keys (hashes) and connectors (offers)
    /secrets/connector-keys.json   the inner side's keys, in the clear
    /secrets/gateway.key           what the gateway presents on /via

What this process writes is state and stream logs, to /state.
"""
import asyncio
import hashlib
import hmac
import json
import os
import posixpath
import random
import re
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
       GW_KEY.lower(), GW_CALLER.lower(), GW_DEST.lower()}
# Spec 2.3: an offer may name the node's front door, never a platform
# service behind it. Re-checked here although appctl refuses them at
# `offer add` -- connect.json is a file, and a file can be edited.
PLATFORM_HOSTS = {"identity", "portal", "store", "twin", "broker", "relay",
                  "connect", "localhost"}
FRONT_DOOR = {"gateway", "oaap-gateway-1"}


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


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
            r = recs.get(label)
            if not r or r.get("hash") != t.key_hash or r.get("tenant") != t.tenant:
                log(f"tunnel {label}: key revoked -- closing")
                await t.close(4001, "key revoked")
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
                "endpoint": c.get("endpoint", ""),
                "paused": bool(c.get("paused")),
                "key": bool(self.keys.get(label)),
                "connected": bool(conn and conn.connected),
                "since": conn.since if conn else "",
                "last_error": conn.last_error if conn else "",
                "next_attempt": conn.next_attempt if conn else "",
                "offers": sorted((c.get("offers") or {}).keys()),
            }
        return {"schema": "0.1", "time": now_iso(), "tunnels": tunnels,
                "connectors": connectors}

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
            # (spec 2.2, 2.5) -- answered like a tunnel that is not there
            return text(403, f"There is no tunnel '{label}' for this destination's tenant.")
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
                    self.offers = _offers(m.get("offers"))
                    if t == "hello":
                        self.connector = str(m.get("connector", ""))[:40]
                    self.node.dirty()
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
        if offer not in {o["name"] for o in self.offers}:
            self.logger.write(offer=offer, caller=caller, destination=dest,
                              method=request.method, path=request.rel_url.path,
                              status=404, result="not offered")
            return text(404, f"The inner node does not offer '{offer}' on this "
                             "tunnel (oaap connect offers " + self.label + ").")
        sid = self.next_id
        self.next_id += 1
        s = Stream()
        self.streams[sid] = s
        prefix = f"/via/{self.label}/{offer}"
        raw = request.raw_path
        rest = raw[len(prefix):] if raw.startswith(prefix) else "/"
        if not rest.startswith("/"):
            rest = "/" + rest
        has_body = bool(request.body_exists)
        headers = [[k, v] for k, v in request.headers.items() if k.lower() not in HOP]
        if request.headers.get("Content-Length"):
            headers.append(["Content-Length", request.headers["Content-Length"]])
        t0 = time.monotonic()
        sent = received = 0
        status, result = 502, ""
        pump = None
        try:
            await self.send_json({"t": "open", "id": sid, "offer": offer,
                                  "method": request.method, "path": rest,
                                  "headers": headers, "body": has_body,
                                  "caller": caller, "destination": dest})

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
                if k.lower() not in HOP:
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
            self.logger.write(offer=offer, caller=caller, destination=dest,
                              method=request.method, path=urllib.parse.urlsplit(rest).path,
                              status=status, sent=sent, received=received,
                              ms=int((time.monotonic() - t0) * 1000), result=result)


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
            self.logger.write(offer=m.get("offer", ""), caller=m.get("caller", ""),
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
    app.router.add_get("/healthz", lambda _r: web.Response(text="ok\n"))
    return app


async def main():
    node = Node()
    node.load()
    await node.reconcile()
    runner = web.AppRunner(make_app(node), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log(f"connect: listening on :{PORT} (platform network only)")
    await asyncio.gather(node.watch(), node.write_state())


if __name__ == "__main__":
    asyncio.run(main())
