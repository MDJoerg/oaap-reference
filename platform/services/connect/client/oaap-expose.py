#!/usr/bin/env python3
"""oaap-expose -- put one address of THIS machine on the internet for a
while, behind an OAAP node's login (oaap.net.connector 0.2, spec 2.8.5).

    export OAAP_KEY=oaapk_...            # your API key, from the portal
    python oaap-expose.py http://localhost:3000 \\
           --server https://oaap.example --tenant myclub --ttl 4h

It prints an address like https://k3f9x2mh4a.t.oaap.example/ and keeps
it open until you press Ctrl-C or the time is up. Signed-in users of the
tenant can open it; `--public` opens it to everybody, with a brake.

What it does and does not do:
  * it dials OUT to the node (WebSocket over https, works through a
    proxy, opens no port on this machine);
  * the target address stays here -- the node is told only that you want
    a name, never where it points;
  * it may expose and nothing else, and only with the API key of a
    tenant_admin;
  * the key is read from OAAP_KEY or a hidden prompt, never an argument.

Needs Python 3.9+ and aiohttp (pip install aiohttp). One file, same on
Linux, macOS and Windows.
"""
import argparse
import asyncio
import getpass
import json
import math
import os
import random
import re
import secrets
import struct
import sys
import time
import urllib.parse

try:
    import aiohttp
    from yarl import URL
except ImportError:                                            # pragma: no cover
    sys.exit("oaap-expose needs aiohttp:  pip install aiohttp")

SUBPROTOCOL = "oaap-connect.1"
WIRE_VERSION = 1
CHUNK = 64 * 1024
DATA, END = 1, 2
HEAD_TIMEOUT = 120
BACKOFF_MAX = 60
# the tunnel's own hop-by-hop list (spec 2.6) -- what only sits between
# two neighbours is never passed on
HOP = {"connection", "keep-alive", "proxy-authenticate", "expect",
       "proxy-authorization", "te", "trailer", "trailers",
       "transfer-encoding", "upgrade", "host", "content-length",
       "x-forwarded-for", "x-forwarded-proto", "x-forwarded-host"}


class Fatal(Exception):
    """Something only the person can fix: said once, then the program ends."""


def frame(kind, sid, payload=b""):
    return struct.pack(">BI", kind, sid) + payload


def unframe(data):
    if len(data) < 5:
        return None, None, b""
    kind, sid = struct.unpack(">BI", data[:5])
    return kind, sid, data[5:]


def parse_ttl(text):
    """8h, 30m, 2d, 90s or a bare number of seconds."""
    m = re.fullmatch(r"(\d+)\s*([smhd]?)", (text or "").strip().lower())
    if not m:
        raise argparse.ArgumentTypeError("a time like 30m, 8h or 2d")
    secs = int(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]
    if secs < 60:
        raise argparse.ArgumentTypeError("an exposure lasts at least a minute")
    if secs > 7 * 86400:
        raise argparse.ArgumentTypeError("an exposure lasts at most 7 days -- open it "
                                         "again when the time is up")
    return secs


def check_target(url):
    u = urllib.parse.urlsplit(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise SystemExit("the target is an http:// or https:// address, "
                         "e.g. http://localhost:3000")
    if u.username or u.password or u.query or u.fragment:
        raise SystemExit("the target carries no user, query or fragment")
    return url


def clean_path(raw):
    if "\\" in raw:
        return "", "a backslash in the path"
    dec = urllib.parse.unquote(raw)
    if "\\" in dec or "\x00" in dec:
        return "", "a backslash or NUL in the path"
    if any(seg in (".", "..") for seg in dec.split("/")):
        return "", "a '.' or '..' segment in the path"
    return dec if dec.startswith("/") else "/" + dec, ""


class Stream:
    def __init__(self):
        self.body = asyncio.Queue(maxsize=64)

    async def feed(self, item):
        try:
            await asyncio.wait_for(self.body.put(item), 30)
            return True
        except asyncio.TimeoutError:
            return False


async def body_chunks(stream):
    while True:
        item = await stream.body.get()
        if item is None:
            return
        yield item


class Client:
    def __init__(self, server, key, tenant, target, ttl, public, quiet=False):
        self.server = server.rstrip("/")
        self.key, self.tenant = key, tenant
        self.target, self.ttl, self.public = target, ttl, public
        self.ref = "x-" + secrets.token_hex(4)
        self.deadline = time.time() + ttl        # ONE end, kept across reconnects
        self.name = ""
        self.url = ""
        self.quiet = quiet
        self.ws = None
        self.streams = {}
        self.send_lock = asyncio.Lock()
        self.ended = asyncio.Event()
        self.answered = asyncio.Event()

    def say(self, msg):
        if not self.quiet:
            print(msg, flush=True)

    async def send_json(self, obj):
        async with self.send_lock:
            await self.ws.send_str(json.dumps(obj))

    async def send_bytes(self, b):
        async with self.send_lock:
            await self.ws.send_bytes(b)

    async def run(self):
        url = self.server + "/connect/tunnel"
        delay = 1
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=20)
        async with aiohttp.ClientSession(trust_env=True, timeout=timeout,
                                         auto_decompress=False) as http:
            while not self.ended.is_set():
                try:
                    async with http.ws_connect(
                            url, protocols=(SUBPROTOCOL,), heartbeat=20,
                            max_msg_size=CHUNK + 1024,
                            headers={"Authorization": "Bearer " + self.key,
                                     "X-OAAP-Tenant": self.tenant}) as ws:
                        self.ws = ws
                        delay = 1
                        await self.send_json({"t": "hello", "connector": "laptop",
                                              "offers": [], "version": WIRE_VERSION})
                        await self.send_json({
                            "t": "expose", "ref": self.ref, "public": self.public,
                            "ttl": max(1, math.ceil(self.deadline - time.time())),
                            "resume": self.name})
                        await self.serve(ws, http)
                        why = "the node closed the connection"
                        if ws.close_code == 4001:
                            raise Fatal("the node no longer accepts this key -- "
                                        "revoked, or it lost the tenant_admin role")
                        self.say(f"  {why}; dialling again")
                except asyncio.CancelledError:
                    raise
                except aiohttp.WSServerHandshakeError as e:
                    if e.status in (401, 403, 429):
                        raise Fatal(self.refusal(e.status, await self.why(http, url)))
                    self.say(f"  the node answered {e.status}; trying again")
                except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as e:
                    self.say(f"  cannot reach the node ({type(e).__name__}); trying again")
                self.ws = None
                for _s, task in list(self.streams.values()):
                    task.cancel()
                self.streams.clear()
                if self.ended.is_set():
                    return
                await asyncio.sleep(min(delay, BACKOFF_MAX) * (0.8 + 0.4 * random.random()))
                delay = min(delay * 2, BACKOFF_MAX)

    async def why(self, http, url):
        """The node's sentence for a refusal. A WebSocket handshake error
        carries no body, so ask once more the plain way -- the node answers
        a refused key the same at the same door, and says why."""
        try:
            async with http.get(url, headers={"Authorization": "Bearer " + self.key,
                                              "X-OAAP-Tenant": self.tenant}) as r:
                return (await r.text()).strip()[:300]
        except (aiohttp.ClientError, OSError, asyncio.TimeoutError):
            return "no reason given"

    @staticmethod
    def refusal(status, message):
        if status == 401:
            return "the node refused the key (401): unknown, revoked or expired"
        if status == 429:
            return "too many attempts (429) -- wait a minute"
        return f"the node refused ({status}): {message}"

    async def serve(self, ws, http):
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    m = json.loads(msg.data)
                except ValueError:
                    continue
                t = m.get("t")
                if t == "exposed":
                    first = not self.name
                    self.name = m.get("name", "")
                    self.url = m.get("url", "")
                    self.answered.set()
                    if first:
                        self.say(f"\n  {self.url}\n  "
                                 f"{'public (no login), braked' if m.get('public') else 'behind the login of the tenant'}"
                                 f"; open until {m.get('expires')}  (Ctrl-C ends it)\n")
                elif t == "expose-refused":
                    raise Fatal("the node refused: " + str(m.get("reason")))
                elif t == "expired":
                    if m.get("reason") == "revoked":
                        raise Fatal("the node ended this exposure: the key was revoked, "
                                    "or it lost the tenant_admin role")
                    self.say("  the node ended this exposure (" + str(m.get("reason")) + ")")
                    self.ended.set()
                    return
                elif t == "open":
                    sid = m.get("id")
                    s = Stream()
                    task = asyncio.ensure_future(self.handle_open(m, s, http))
                    self.streams[sid] = (s, task)
                    task.add_done_callback(lambda _t, sid=sid: self.streams.pop(sid, None))
                elif t == "cancel":
                    entry = self.streams.pop(m.get("id"), None)
                    if entry:
                        entry[1].cancel()
            elif msg.type == aiohttp.WSMsgType.BINARY:
                kind, sid, payload = unframe(msg.data)
                entry = self.streams.get(sid)
                if entry and not await entry[0].feed(payload if kind == DATA else None):
                    entry[1].cancel()
            elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                break

    def route(self, m):
        """(url, headers, None) or (None, None, (status, reason)). The
        same steps as spec 2.6 for an offer, for the one target."""
        if m.get("exposure") != self.ref:
            return None, None, (404, "this client holds no such exposure")
        if time.time() >= self.deadline:
            return None, None, (404, "this exposure has ended")
        raw = str(m.get("path") or "/")
        raw_path, _, query = raw.partition("?")
        path, err = clean_path(raw_path)
        if err:
            return None, None, (403, "refused: " + err)
        base = urllib.parse.urlsplit(self.target)
        target = (f"{base.scheme}://{base.netloc}{base.path.rstrip('/')}{path}"
                  + (f"?{query}" if query else ""))
        headers = [(k, v) for k, v in m.get("headers") or []
                   if k.lower() not in HOP or k.lower() == "content-length"]
        return URL(target, encoded=True), headers, None

    async def handle_open(self, m, s, http):
        sid = m.get("id")
        url, headers, refusal = self.route(m)
        try:
            if refusal:
                await self.send_json({"t": "error", "id": sid, "status": refusal[0],
                                      "reason": refusal[1]})
                return

            async def body():
                async for chunk in body_chunks(s):
                    yield chunk
            sent = False
            try:
                async with http.request(
                        m.get("method", "GET"), url, headers=headers,
                        data=body() if m.get("body") else None, allow_redirects=False,
                        timeout=aiohttp.ClientTimeout(total=None, sock_connect=20,
                                                      sock_read=HEAD_TIMEOUT)) as r:
                    sent = True
                    await self.send_json({"t": "head", "id": sid, "status": r.status,
                                          "headers": [[k, v] for k, v in r.headers.items()
                                                      if k.lower() not in HOP
                                                      or k.lower() == "content-length"]})
                    async for chunk in r.content.iter_chunked(CHUNK):
                        await self.send_bytes(frame(DATA, sid, chunk))
                    await self.send_bytes(frame(END, sid))
            except asyncio.TimeoutError:
                await self.send_json({"t": "error", "id": sid, "status": 504,
                                      "reason": "the target did not answer in time"})
            except aiohttp.ClientError as e:
                if sent:
                    await self.send_json({"t": "cancel", "id": sid})
                else:
                    await self.send_json({"t": "error", "id": sid, "status": 502,
                                          "reason": "the target is not reachable: "
                                                    + type(e).__name__})
        except asyncio.CancelledError:
            raise
        except Exception:                                        # noqa: BLE001
            pass

    async def stop(self):
        """Ctrl-C: tell the node, so the name goes at once."""
        self.ended.set()
        if self.ws is not None and not self.ws.closed:
            try:
                await self.send_json({"t": "unexpose", "ref": self.ref})
                await self.ws.close()
            except Exception:                                    # noqa: BLE001
                pass


async def amain(args):
    key = os.environ.get("OAAP_KEY") or getpass.getpass("API key (oaapk_...): ").strip()
    if not key.startswith("oaapk_"):
        raise SystemExit("that is not an API key (it starts with oaapk_ -- portal, Zugänge)")
    c = Client(args.server, key, args.tenant, check_target(args.target), args.ttl, args.public)
    task = asyncio.ensure_future(c.run())
    timer = asyncio.ensure_future(_deadline(c))
    try:
        await asyncio.wait({task, timer}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        await c.stop()
        for t in (task, timer):
            t.cancel()
        if task.done() and not task.cancelled() and task.exception():
            raise task.exception()


async def _deadline(c):
    while time.time() < c.deadline and not c.ended.is_set():
        await asyncio.sleep(1)
    if not c.ended.is_set():
        c.say("  the time is up")


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="oaap-expose", description="Put one address of this machine on the "
        "internet for a while, behind an OAAP node's login.")
    ap.add_argument("target", help="what to expose, e.g. http://localhost:3000")
    ap.add_argument("--server", default=os.environ.get("OAAP_SERVER", ""),
                    help="the node, e.g. https://oaap.example (or OAAP_SERVER)")
    ap.add_argument("--tenant", default=os.environ.get("OAAP_TENANT", ""),
                    help="your tenant (or OAAP_TENANT)")
    ap.add_argument("--ttl", type=parse_ttl, default=8 * 3600,
                    help="how long: 30m, 8h, 2d (default 8h, at most 7d)")
    ap.add_argument("--public", action="store_true",
                    help="no login for visitors (still braked, still expires)")
    args = ap.parse_args(argv)
    if not args.server or not args.tenant:
        ap.error("--server and --tenant are needed (or OAAP_SERVER / OAAP_TENANT)")
    if not args.server.startswith(("https://", "http://")):
        ap.error("--server is an address like https://oaap.example")
    try:
        asyncio.run(amain(args))
    except KeyboardInterrupt:
        print("\n  closed.")
    except Fatal as e:
        sys.exit(f"oaap-expose: {e}")


if __name__ == "__main__":
    main()
