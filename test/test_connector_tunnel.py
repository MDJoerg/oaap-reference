#!/usr/bin/env python3
"""Der Tunnel, Stufe 2 (oaap.net.connector 0.1, RFC-0033).

Zwei echte Prozesse des Verbindungsdienstes -- aussen und innen -- und
ein Backend in diesem Prozess. Das Gateway wird gespielt: der Test ruft
`/via/...` so, wie das Gateway es tut, mit dessen Schluessel.

Festgehalten werden die Regeln der Spec, nicht das heutige Verhalten:

- der Schluessel ist die einzige Tuer, und ein unbekannter und ein
  widerrufener Schluessel bekommen dieselbe Antwort (2.2, 2.4);
- aussen stehen die NAMEN der Angebote, nie die Adressen (2.4, T10);
- die Pruefung passiert innen, vor dem Backend, gegen die aktuelle
  Liste -- und was abgelehnt wird, erreicht das Backend nicht (2.6);
- ein Angebot entfernen, pausieren, widerrufen: jedes schneidet sofort,
  und aussen kann niemand wieder aufmachen (2.3);
- `/via` ohne den Schluessel des Gateways und mit fremdem Mandanten
  wird abgelehnt (2.5);
- derselbe Schluessel ein zweites Mal ersetzt die alte Verbindung (2.4).

Braucht aiohttp (wie der Dienst selbst).
Aufruf: python3 test/test_connector_tunnel.py
"""
import asyncio
import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import time

import aiohttp
from aiohttp import web
from yarl import URL

HERE = os.path.dirname(os.path.abspath(__file__))
SERVICE = os.path.join(HERE, "..", "platform", "services", "connect", "app.py")
sys.path.insert(0, os.path.dirname(SERVICE))
import app as connect                                          # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:700]}")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f)
    os.replace(tmp, path)
    # mtime granularity on some file systems -- make every write visible
    global _writes
    _writes += 1
    t = time.time() + _writes * 0.001
    os.utime(path, (t, t))


_writes = 0


def lan_ip():
    """An address of this machine that is not loopback -- the service
    refuses loopback offers on purpose (spec 2.3), so the backend must
    listen somewhere else. Nothing is sent: a UDP connect only picks
    the interface."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def read_state(side):
    try:
        with open(os.path.join(side["state"], "state.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def side(root, name):
    d = {k: os.path.join(root, name, k) for k in ("apps", "secrets", "state")}
    for p in d.values():
        os.makedirs(p, exist_ok=True)
    d["port"] = free_port()
    return d


def start(s):
    env = dict(os.environ, CONNECT_APPS=s["apps"], CONNECT_SECRETS=s["secrets"],
               CONNECT_STATE=s["state"], CONNECT_PORT=str(s["port"]),
               PYTHONUNBUFFERED="1")
    s["log"] = open(os.path.join(s["state"], "..", "stderr.txt"), "w")
    s["proc"] = subprocess.Popen([sys.executable, SERVICE], env=env,
                                 stdout=s["log"], stderr=subprocess.STDOUT)


async def until(pred, timeout=8.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        await asyncio.sleep(0.1)
    return pred()


# --- das Backend hinter dem inneren Knoten ---------------------------------
SEEN = []


async def backend_handler(request):
    body = await request.read()
    SEEN.append({"method": request.method, "path": request.raw_path,
                 "headers": dict(request.headers), "len": len(body),
                 "sha": hashlib.sha256(body).hexdigest()})
    if request.path.endswith("/big"):
        return web.Response(body=b"x" * (3 * 1024 * 1024 + 7))
    return web.json_response(SEEN[-1])


async def main():
    root = tempfile.mkdtemp(prefix="oaap-connector-test-")
    outer, inner = side(root, "outer"), side(root, "inner")
    bport = free_port()
    bapp = web.Application(client_max_size=64 * 1024 ** 2)
    bapp.router.add_route("*", "/{tail:.*}", backend_handler)
    brunner = web.AppRunner(bapp)
    await brunner.setup()
    bhost = lan_ip()
    await web.TCPSite(brunner, bhost, bport).start()

    KEY = "oaapc_" + "k" * 43
    GW = "gw-" + "g" * 40
    key_rec = {"hash": hashlib.sha256(KEY.encode()).hexdigest(), "tenant": "t1"}
    write_json(os.path.join(outer["apps"], "connect.json"),
               {"schema": "0.1", "keys": {"x01": key_rec}})
    with open(os.path.join(outer["secrets"], "gateway.key"), "w") as f:
        f.write(GW)
    backend = f"http://{bhost}:{bport}/base"
    inner_conf = {"schema": "0.1", "connectors": {"x01": {
        "endpoint": f"http://127.0.0.1:{outer['port']}", "plain": True,
        "paused": False,
        "offers": {"erp": {"to": backend, "path": "/api", "methods": ["GET", "POST", "HEAD"]},
                   "all": {"to": backend}}}}}
    write_json(os.path.join(inner["apps"], "connect.json"), inner_conf)
    write_json(os.path.join(inner["secrets"], "connector-keys.json"), {"keys": {"x01": KEY}})

    # the offer check is pure -- judged before anything runs
    ok("offer: portal refused", connect.offer_refusal("http://portal:8000/") != "")
    ok("offer: identity refused", connect.offer_refusal("http://identity:8000/") != "")
    ok("offer: localhost refused", connect.offer_refusal("http://localhost:9000/") != "")
    ok("offer: 127.0.0.2 refused", connect.offer_refusal("http://127.0.0.2/") != "")
    ok("offer: gateway on 8098 refused",
       connect.offer_refusal("http://oaap-gateway-1:8098/") != "")
    ok("offer: another platform container refused",
       connect.offer_refusal("http://oaap-portal-1:8000/") != "")
    ok("offer: gateway front door accepted",
       connect.offer_refusal("http://gateway:80/twin/") == "")
    ok("offer: a LAN backend accepted",
       connect.offer_refusal("https://erp.lan:8443/api") == "")
    ok("offer: user info refused", connect.offer_refusal("http://u:p@erp.lan/") != "")
    ok("path: %2e%2e is a segment", connect.clean_path("/api/%2e%2e/x")[1] != "")
    ok("path: backslash refused", connect.clean_path("/api\\..\\x")[1] != "")
    ok("prefix: /api does not cover /apix", not connect.under_prefix("/apix", "/api"))
    ok("prefix: /api covers /api/x", connect.under_prefix("/api/x", "/api"))

    start(outer)
    start(inner)
    base = f"http://127.0.0.1:{outer['port']}"
    try:
        async with aiohttp.ClientSession(auto_decompress=False) as http:
            async def up():
                try:
                    async with http.get(base + "/healthz") as r:
                        return r.status == 200
                except aiohttp.ClientError:
                    return False
            for _ in range(80):
                if await up():
                    break
                await asyncio.sleep(0.1)

            # 1 -- the key is the only door
            async with http.get(base + "/connect/tunnel",
                                headers={"Authorization": "Bearer nope"}) as r:
                ok("T1 unknown key -> 401", r.status == 401, r.status)

            # connected AND announced: the hello follows the handshake
            connected = await until(lambda: (read_state(outer).get("tunnels", {})
                                             .get("x01", {}).get("connected"))
                                    and read_state(outer)["tunnels"]["x01"]["offers"])
            ok("the inner node dials out and the tunnel stands", connected,
               open(os.path.join(root, "inner", "stderr.txt")).read()[-800:]
               if not connected else "")
            st = read_state(outer)
            names = [o["name"] for o in st["tunnels"]["x01"]["offers"]]
            ok("T10 outer state names the offers", names == ["all", "erp"], names)
            ok("T10 outer state carries no address",
               str(bport) not in json.dumps(st) and bhost not in json.dumps(st))
            ist = read_state(inner)
            ok("inner state: connected", ist.get("connectors", {}).get("x01", {}).get("connected"))

            def gw(tenant="t1", key=GW):
                h = {connect.GW_CALLER: "orders", connect.GW_DEST: f"{tenant}/erp",
                     "Authorization": "Bearer dest-credential",
                     "X-Forwarded-For": "10.9.9.9"}
                if key:
                    h[connect.GW_KEY] = key
                return h

            # 2 -- a call arrives as sent, with the credential and its own Host
            SEEN.clear()
            # encoded=True: the client must not normalise %2F itself, or
            # the test would prove nothing about the service
            async with http.get(URL(base + "/via/x01/erp/api/orders/42?q=1&x=%2F",
                                    encoded=True), headers=gw()) as r:
                body = await r.json() if r.status == 200 else await r.text()
            ok("T2 a call through the tunnel -> 200", r.status == 200, body)
            if r.status == 200:
                ok("T2 path and query intact below the base",
                   body["path"] == "/base/api/orders/42?q=1&x=%2F", body["path"])
                ok("T2 the credential arrives", body["headers"].get("Authorization")
                   == "Bearer dest-credential", body["headers"])
                ok("T2 Host is the backend's", body["headers"].get("Host")
                   == f"{bhost}:{bport}", body["headers"].get("Host"))
                leaked = [h for h in body["headers"] if h.lower().startswith("x-oaap-connect")
                          or h.lower() == "x-forwarded-for"]
                ok("T2 none of the gateway's headers, no X-Forwarded-For", not leaked, leaked)

            # bodies both ways
            blob = os.urandom(1024 * 1024 + 13)
            async with http.post(base + "/via/x01/erp/api/upload", data=blob,
                                 headers=gw()) as r:
                b = await r.json() if r.status == 200 else {}
            ok("a 1 MiB request body arrives whole",
               b.get("len") == len(blob) and b.get("sha") == hashlib.sha256(blob).hexdigest(), b)
            # oaap-test 2026-09-25: an Expect passed on hung every large
            # upload for 120 s -- the backend here would answer it, a
            # plain http.server does not, so the rule is: it never arrives
            async with http.post(base + "/via/x01/erp/api/upload", data=blob,
                                 headers={**gw(), "Expect": "100-continue"}) as r:
                b = await r.json() if r.status == 200 else {}
            ok("Expect: 100-continue is answered here, never passed to the backend",
               r.status == 200 and "Expect" not in (b.get("headers") or {}), (r.status, b))
            async with http.get(base + "/via/x01/erp/api/big", headers=gw()) as r:
                big = await r.read()
            ok("a 3 MiB answer arrives whole", len(big) == 3 * 1024 * 1024 + 7, len(big))
            async with http.head(base + "/via/x01/erp/api/orders", headers=gw()) as r:
                ok("HEAD passes", r.status == 200, r.status)

            # 3 -- refused inside, and the backend receives nothing
            SEEN.clear()
            async with http.get(base + "/via/x01/nope/x", headers=gw()) as r:
                ok("T3 an offer not announced -> 404", r.status == 404, r.status)
            async with http.delete(base + "/via/x01/erp/api/orders/1", headers=gw()) as r:
                ok("T3 a method not allowed -> 405", r.status == 405, r.status)
            async with http.get(base + "/via/x01/erp/api/%2e%2e/secret", headers=gw()) as r:
                ok("T3 %2e%2e escaping the prefix -> 403", r.status == 403, r.status)
            async with http.get(base + "/via/x01/erp/apix", headers=gw()) as r:
                ok("T3 outside the prefix -> 403", r.status == 403, r.status)
            ok("T3 the backend received none of them", SEEN == [], SEEN)

            # 7 -- only the gateway, only the tunnel's own tenant
            async with http.get(base + "/via/x01/erp/api/x", headers=gw(key="")) as r:
                ok("T7 /via without the gateway's key -> 403", r.status == 403, r.status)
            async with http.get(base + "/via/x01/erp/api/x", headers=gw(key="wrong")) as r:
                ok("T7 /via with a wrong key -> 403", r.status == 403, r.status)
            async with http.get(base + "/via/x01/erp/api/x", headers=gw(tenant="t2")) as r:
                other_tenant = (r.status, await r.text())
            ok("T6 a destination of another tenant -> refused", other_tenant[0] == 502,
               other_tenant)
            ok("T6/T7 the backend received none of them", SEEN == [], SEEN)

            # the stream log: one line per call, no header value, no query
            logf = os.path.join(inner["state"], "log", "connector-x01.jsonl")
            lines = [json.loads(x) for x in open(logf, encoding="utf-8")]
            ok("inner stream log: one line per call that reached the inner side",
               len(lines) >= 6, len(lines))
            txt = open(logf, encoding="utf-8").read()
            ok("stream log holds no credential and no query",
               "dest-credential" not in txt and "q=1" not in txt)
            ok("stream log names caller and destination",
               lines[0].get("caller") == "orders" and lines[0].get("destination") == "t1/erp",
               lines[0])

            # 4 -- removing an offer cuts at once, the outer side unchanged
            del inner_conf["connectors"]["x01"]["offers"]["all"]
            write_json(os.path.join(inner["apps"], "connect.json"), inner_conf)
            gone = await until(lambda: [o["name"] for o in read_state(outer)["tunnels"]["x01"]["offers"]]
                               == ["erp"])
            ok("T4 removing an offer re-announces the list", gone)
            async with http.get(base + "/via/x01/all/x", headers=gw()) as r:
                ok("T4 a call to the removed offer -> 404", r.status == 404, r.status)

            # 8 -- the same key a second time replaces the first
            async with http.ws_connect(base + "/connect/tunnel", protocols=(connect.SUBPROTOCOL,),
                                       headers={"Authorization": "Bearer " + KEY}) as ghost:
                await ghost.send_str(json.dumps({"t": "hello", "connector": "ghost",
                                                 "offers": [], "version": 1}))
                took = await until(lambda: read_state(outer)["tunnels"]["x01"].get("connector")
                                   == "ghost", 5)
                ok("T8 a second connection with the same key takes the tunnel", took)
                replaced = await until(lambda: "replaced" in (read_state(inner)["connectors"]
                                                              ["x01"].get("last_error") or ""), 5)
                ok("T8 the first one is told it was replaced", replaced,
                   read_state(inner)["connectors"]["x01"])
            back = await until(lambda: read_state(outer)["tunnels"]["x01"].get("connector")
                               == "x01" and read_state(outer)["tunnels"]["x01"]["connected"], 10)
            ok("the connector comes back after its ghost left", back)

            # 5 -- pause: closed, 502, and the outer side cannot reopen
            inner_conf["connectors"]["x01"]["paused"] = True
            write_json(os.path.join(inner["apps"], "connect.json"), inner_conf)
            t0 = time.monotonic()
            down = await until(lambda: not read_state(outer)["tunnels"]["x01"]["connected"], 6)
            ok("T5 pause closes the tunnel within 5 s", down and time.monotonic() - t0 < 5.5,
               round(time.monotonic() - t0, 2))
            async with http.get(base + "/via/x01/erp/api/x", headers=gw()) as r:
                t = await r.text()
                ok("T5 a call while paused -> 502 with a sentence",
                   r.status == 502 and "not connected" in t, (r.status, t))
            await asyncio.sleep(3)
            ok("T5 a paused connector does not reconnect",
               not read_state(outer)["tunnels"]["x01"]["connected"])
            inner_conf["connectors"]["x01"]["paused"] = False
            write_json(os.path.join(inner["apps"], "connect.json"), inner_conf)
            ok("resume reconnects", await until(
                lambda: read_state(outer)["tunnels"]["x01"]["connected"], 8))

            # backend down -> 502 with a sentence
            await brunner.cleanup()
            async with http.get(base + "/via/x01/erp/api/x", headers=gw()) as r:
                t = await r.text()
                ok("a backend that is down -> 502 with a sentence",
                   r.status == 502 and "not reachable" in t, (r.status, t))

            # 1 -- revoke: the same answer as unknown, and the tunnel closes
            write_json(os.path.join(outer["apps"], "connect.json"),
                       {"schema": "0.1", "keys": {}})
            t0 = time.monotonic()
            closed = await until(lambda: not (read_state(outer).get("tunnels") or {})
                                 .get("x01", {}).get("connected"), 6)
            ok("T1 revoking closes a connected tunnel within 5 s",
               closed and time.monotonic() - t0 < 5.5, round(time.monotonic() - t0, 2))
            async with http.get(base + "/connect/tunnel",
                                headers={"Authorization": "Bearer " + KEY}) as r:
                ok("T1 a revoked key gets the same 401 as an unknown one", r.status == 401,
                   r.status)
            async with http.get(base + "/via/x01/erp/api/x", headers=gw()) as r:
                revoked = (r.status, await r.text())
            ok("T6 a revoked tunnel and another tenant's get the SAME answer",
               revoked == other_tenant, (revoked, other_tenant))
            told = await until(lambda: "401" in (read_state(inner)["connectors"]["x01"]
                                                 .get("last_error") or "")
                               or "revoked" in (read_state(inner)["connectors"]["x01"]
                                                .get("last_error") or ""), 6)
            ok("the inner side says why", told, read_state(inner)["connectors"]["x01"])
    finally:
        for s in (outer, inner):
            if s.get("proc"):
                s["proc"].terminate()
                try:
                    s["proc"].wait(5)
                except subprocess.TimeoutExpired:
                    s["proc"].kill()
                s["log"].close()


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
    print("")
    print("OK" if not fails else f"{fails} FAILED")
    sys.exit(1 if fails else 0)
