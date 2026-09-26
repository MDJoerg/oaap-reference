#!/usr/bin/env python3
"""Freigaben durch den Tunnel, echte Prozesse (oaap.net.connector 0.2,
RFC-0033 Stufe 3).

Der Verbindungsdienst laeuft zweimal (aussen, innen), dazu der echte
Laptop-Client `oaap-expose.py` als eigener Prozess, ein Backend in diesem
Prozess und eine gespielte Anmeldung (identity). Das Gateway wird
gespielt: der Test ruft `/exposure/verify` und `/exposed/...` so, wie die
erzeugte Site es tut, mit dem Schluessel des Dienstes.

Festgehalten werden die Regeln der Spec (2.8), nicht das heutige
Verhalten:

- den Namen waehlt der AEUSSERE Knoten, 10 Zeichen, unter der Zone; die
  Adresse des Ziels steht nirgends aussen -- nicht im Zustand, nicht im
  Pruefprotokoll (2.8.1, 2.8.2, 2.8.8);
- Anmeldung als Vorgabe: ohne Sitzung wird umgeleitet, ein Benutzer des
  Mandanten kommt durch, einer aus einem anderen nicht, server_admin
  immer -- und was die Anmeldung antwortet, wird unveraendert
  weitergereicht (2.8.4);
- was nur die Plattform sehen darf, geht nicht ans Ziel: das Sitzungs-
  Cookie (auf dem ganzen Knoten gueltig -- wer es behielte, WAERE der
  Besucher) und ein API-Schluessel; und ein Ziel kann die Sitzung des
  Knotens nicht setzen (2.8.4);
- oeffentlich: keine Anmeldung, die Identitaets-Kopfzeilen LEER, und
  nach 120 Aufrufen in 60 s je Adresse ein 429 (2.8.4);
- die Frist laeuft aus, wird vom Ausleger geraeumt und der Gegenstelle
  gesagt; ein Wiederverbinden gibt denselben Namen zurueck und verlaengert
  nichts; nur ein spaeteres Ende ist eine Verlaengerung (2.8.2, 2.8.6);
- der Laptop-Client: nur mit dem Schluessel eines tenant_admin des
  eigenen Mandanten, sonst 401/403 mit Satz; sein Tunnel kann
  freigeben und sonst nichts; ein widerrufener Schluessel beendet Tunnel
  und Freigaben (2.8.5, 2.8.6);
- `close` des Betreibers und ein widerrufener Verbindungsschluessel
  beenden eine Freigabe, und zwar sofort (2.8.6);
- ohne Zone: ein Satz, der es sagt, und danach klappt es (2.8.3).

Braucht aiohttp (wie der Dienst selbst).
Aufruf: python3 test/test_exposures_tunnel.py
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
CLIENT = os.path.join(HERE, "..", "platform", "services", "connect", "client", "oaap-expose.py")
sys.path.insert(0, os.path.dirname(SERVICE))
import app as connect                                          # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:900]}")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


_writes = 0


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f)
    os.replace(tmp, path)
    global _writes
    _writes += 1
    t = time.time() + _writes * 0.001
    os.utime(path, (t, t))


def lan_ip():
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


def start(s, **extra):
    env = dict(os.environ, CONNECT_APPS=s["apps"], CONNECT_SECRETS=s["secrets"],
               CONNECT_STATE=s["state"], CONNECT_PORT=str(s["port"]),
               PYTHONUNBUFFERED="1", **extra)
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


def audit_lines(path):
    try:
        with open(path, encoding="utf-8") as f:
            return [json.loads(x) for x in f if x.strip()]
    except OSError:
        return []


# --- the target behind the inner side -----------------------------------------
SEEN = []


async def backend_handler(request):
    body = await request.read()
    SEEN.append({"method": request.method, "path": request.raw_path,
                 "headers": dict(request.headers), "len": len(body)})
    if request.path.endswith("/setcookie"):
        resp = web.json_response(SEEN[-1])
        resp.headers.add("Set-Cookie", "oaap_session=evil; Domain=example.org; Path=/")
        resp.headers.add("Set-Cookie", "theme=dark; Path=/")
        return resp
    return web.json_response(SEEN[-1])


# --- a stand-in for identity ---------------------------------------------------
IDENT_CALLS = []
SESSIONS = {"alice": ("t1", "user"), "bob": ("t2", "user"), "carol": ("t9", "server_admin,user")}
KEYS = {"oaapk_admin": ("jm", "t1", "tenant_admin,user"),
        "oaapk_plain": ("kim", "t1", "user"),
        "oaapk_foreign": ("lee", "t2", "tenant_admin,user")}
REVOKED = set()


async def ident_verify(request):
    IDENT_CALLS.append({"query": dict(request.query), "headers": dict(request.headers)})
    tenant = request.query.get("tenant", "")
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        k = auth[7:]
        if k in REVOKED or k not in KEYS:
            return web.Response(status=401, text="unknown or revoked key")
        user, ten, roles = KEYS[k]
    else:
        cookie = request.cookies.get("oaap_session", "")
        if cookie not in SESSIONS:
            return web.Response(status=303, headers={
                "Location": "/auth/login?next=" + request.headers.get("X-Forwarded-Uri", "/")})
        user, (ten, roles) = cookie, SESSIONS[cookie]
    if tenant and "server_admin" not in roles and tenant != ten:
        return web.Response(status=403, text="Forbidden: this app belongs to another tenant")
    return web.Response(status=204, headers={
        "X-OAAP-User": user, "X-OAAP-Roles": roles, "X-OAAP-User-Id": "id-" + user,
        "X-OAAP-Display-Name": user.title(), "X-OAAP-Email": ""})


async def main():
    root = tempfile.mkdtemp(prefix="oaap-exposures-test-")
    outer, inner = side(root, "outer"), side(root, "inner")
    audit_path = os.path.join(root, "audit", "tenant-log.jsonl")
    bport, iport = free_port(), free_port()

    bapp = web.Application(client_max_size=64 * 1024 ** 2)
    bapp.router.add_route("*", "/{tail:.*}", backend_handler)
    brunner = web.AppRunner(bapp)
    await brunner.setup()
    await web.TCPSite(brunner, "0.0.0.0", bport).start()
    bhost = lan_ip()
    iapp = web.Application()
    iapp.router.add_get("/verify", ident_verify)
    irunner = web.AppRunner(iapp)
    await irunner.setup()
    await web.TCPSite(irunner, "127.0.0.1", iport).start()

    KEY = "oaapc_" + "k" * 43
    GW = "gw-" + "g" * 40
    ZONE = "t.example.org"
    write_json(os.path.join(outer["apps"], "tenants.json"),
               {"tenants": {"t1": {"label": "club"}, "t2": {"label": "other"}}})
    outer_conf = {"schema": "0.2",
                  "keys": {"x01": {"hash": hashlib.sha256(KEY.encode()).hexdigest(), "tenant": "t1"}}}
    write_json(os.path.join(outer["apps"], "connect.json"), outer_conf)   # NO zone yet (2.8.3)
    with open(os.path.join(outer["secrets"], "gateway.key"), "w") as f:
        f.write(GW)
    # exposures the outer node held before this run: one that runs out in
    # a few seconds, one that will be extended by a reconnecting client
    now = time.time()
    write_json(os.path.join(outer["state"], "exposures.json"), {"exposures": {
        "ttlexpire1": {"name": "ttlexpire1", "tenant": "t1", "owner": "c:x01", "ref": "x-ttl001",
                       "public": False, "opened": now, "expires": now + 9,
                       "opened_by": "connector:x01", "calls": 0}}, "opened": [now]})

    target = f"http://{bhost}:{bport}/base"
    inner_conf = {"schema": "0.2", "connectors": {"x01": {
        "endpoint": f"http://127.0.0.1:{outer['port']}", "plain": True, "paused": False,
        "offers": {},
        "exposures": {"x-aaa111": {"target": target, "public": False,
                                   "expires_at": now + 3600}}}}}
    write_json(os.path.join(inner["apps"], "connect.json"), inner_conf)
    write_json(os.path.join(inner["secrets"], "connector-keys.json"), {"keys": {"x01": KEY}})

    ok("the service knows its limits (2.8.3)",
       (connect.EXPOSE_DEFAULT_TTL, connect.EXPOSE_MAX_TTL,
        connect.EXPOSE_PER_OWNER, connect.EXPOSE_PER_NODE, connect.PERSON_TUNNELS,
        connect.NAME_LEN) == (8 * 3600, 7 * 86400, 10, 100, 5, 10))

    start(outer, CONNECT_IDENTITY=f"http://127.0.0.1:{iport}", CONNECT_AUDIT=audit_path,
          CONNECT_RECHECK="1", CONNECT_CLIENT_FILE=CLIENT)
    start(inner)
    base = f"http://127.0.0.1:{outer['port']}"
    clients = []
    try:
        async with aiohttp.ClientSession(auto_decompress=False) as http:
            for _ in range(80):
                try:
                    async with http.get(base + "/healthz") as r:
                        if r.status == 200:
                            break
                except aiohttp.ClientError:
                    pass
                await asyncio.sleep(0.1)

            def gwh(host, key=GW, client="9.9.9.9", **extra):
                h = {connect.GW_HOST: host, connect.GW_CLIENT: client, **extra}
                if key:
                    h[connect.GW_KEY] = key
                return h

            # 2.8.3 -- no zone on this node yet: an answer that says so
            ref_state = lambda: (read_state(inner).get("connectors", {}).get("x01", {})
                                 .get("exposures", {}).get("x-aaa111") or {})
            refused = await until(lambda: ref_state().get("error"), 10)
            ok("no zone: the outer node says so in a sentence", refused
               and "external hostname" in ref_state()["error"], ref_state())
            ok("no zone: nothing was opened", not [e for e in read_state(outer).get("exposures", {})
                                                    if e != "ttlexpire1"])

            # the operator sets the external name; the request is asked again
            outer_conf["exposure"] = {"zone": ZONE, "scheme": "https"}
            write_json(os.path.join(outer["apps"], "connect.json"), outer_conf)
            # the outer service re-reads within a second; ask again only when it knows
            knows = await until(lambda: read_state(outer).get("zone") == ZONE, 10)
            ok("the outer service picks the zone up from connect.json", knows, read_state(outer).get("zone"))
            write_json(os.path.join(inner["apps"], "connect.json"), inner_conf)  # a change: ask again
            got = await until(lambda: ref_state().get("url"), 10)
            ok("with a zone the same request is answered", got, ref_state())
            st = ref_state()
            name = st.get("name", "")
            ok("2.8.1 the name is 10 characters of [a-z0-9], chosen by the outer node",
               len(name) == 10 and name.isalnum() and name == name.lower(), name)
            host = f"{name}.{ZONE}"
            ok("2.8.1 the address is under the zone, with the zone's scheme",
               st.get("url") == f"https://{host}/" and st.get("host") == host, st)
            ost = read_state(outer)
            ok("2.8.9 the outer node lists it -- with tenant, opener, no target",
               name in ost.get("exposures", {}) and ost["exposures"][name]["tenant"] == "t1"
               and ost["exposures"][name]["opened_by"] == "connector:x01", ost.get("exposures"))
            ok("2.8.2 the target's address stands nowhere on the outer node",
               str(bport) not in json.dumps(ost) and bhost not in json.dumps(ost)
               and target not in open(os.path.join(outer["state"], "exposures.json")).read())
            au = audit_lines(audit_path)
            opened = [e for e in au if e["action"] == "exposure.open" and e["subject"] == host]
            ok("2.8.8 opening is one line in the tenant's log: who, where, login, until",
               len(opened) == 1 and opened[0]["who"] == "connector:x01"
               and opened[0]["tenant"] == "t1" and opened[0]["tenant_label"] == "club"
               and "login" in opened[0]["detail"] and "until" in opened[0]["detail"], opened)
            ok("2.8.8 ... and the log never names the target",
               all(str(bport) not in json.dumps(e) and bhost not in json.dumps(e) for e in au))

            # --- who may pass (2.8.4) ---------------------------------------
            async def verify(host_, cookie=None, key=GW, **kw):
                h = gwh(host_, key=key, **{"X-Forwarded-Uri": "/some/page?x=1",
                                             "X-Forwarded-Host": host_})
                if cookie:
                    h["Cookie"] = f"oaap_session={cookie}; theme=dark"
                async with http.get(base + "/exposure/verify", headers=h,
                                    allow_redirects=False) as r:
                    return r.status, dict(r.headers), await r.text()

            s, hd, _ = await verify(host)
            ok("2.8.4 without a session: identity's redirect, unchanged",
               s == 303 and hd.get("Location") == "/auth/login?next=/some/page?x=1", (s, hd))
            call = IDENT_CALLS[-1]
            ok("2.8.4 identity saw what a generated site would show it: the tenant, the "
               "original address, and not the gateway's keys",
               call["query"].get("tenant") == "t1"
               and call["headers"].get("X-Forwarded-Uri") == "/some/page?x=1"
               and connect.GW_KEY not in call["headers"] and connect.GW_HOST not in call["headers"],
               call)
            s, hd, _ = await verify(host, "alice")
            ok("2.8.4 a user of the tenant passes, with the five identity headers",
               s == 204 and hd.get("X-OAAP-User") == "alice"
               and hd.get("X-OAAP-Roles") == "user" and hd.get("X-OAAP-User-Id") == "id-alice"
               and "X-OAAP-Display-Name" in hd and "X-OAAP-Email" in hd, (s, hd))
            s, hd, _ = await verify(host, "bob")
            ok("2.8.4 a user of ANOTHER tenant is refused (403 from identity, unchanged)", s == 403, s)
            s, hd, _ = await verify(host, "carol")
            ok("2.8.4 server_admin passes", s == 204 and hd.get("X-OAAP-User") == "carol", (s, hd))
            s, _, _ = await verify(host, "alice", key="")
            ok("without the gateway's key -> 403", s == 403, s)
            s, _, _ = await verify(host, "alice", key="wrong")
            ok("with a wrong gateway key -> 403", s == 403, s)
            s, _, body = await verify("nosuchname0." + ZONE, "alice")
            ok("2.8.4 a name that is no exposure -> 404 with a sentence",
               s == 404 and "not a live exposure" in body, (s, body))
            for bad in (ZONE, f"a.{name}.{ZONE}", f"{name}.other.example", name + "." + ZONE + ".evil.io",
                        f"{name[:-1]}.{ZONE}"):
                s, _, _ = await verify(bad, "alice")
                ok(f"2.8.4 '{bad}' is no exposure -> 404", s == 404, s)

            # --- the call itself --------------------------------------------
            async def exposed(path, host_=host, headers=None, method="GET", key=GW, **kw):
                h = gwh(host_, key=key, **(headers or {}))
                async with http.request(method, URL(base + "/exposed" + path, encoded=True),
                                        headers=h, allow_redirects=False, **kw) as r:
                    try:
                        b = await r.json()
                    except Exception:                          # noqa: BLE001
                        b = await r.text()
                    return r.status, dict(r.headers), r.headers.getall("Set-Cookie", []), b

            SEEN.clear()
            s, hd, sc, b = await exposed(
                "/hello/x?q=1&y=%2F",
                headers={"X-OAAP-User": "alice", "X-OAAP-Roles": "user",
                         "Cookie": "oaap_session=alice; theme=dark; other=1",
                         "Authorization": "Bearer oaapk_secretsecret",
                         "X-Forwarded-For": "6.6.6.6"})
            ok("2.8.4 a call reaches the target through the tunnel", s == 200, (s, b))
            if s == 200:
                sent = b["headers"]
                ok("2.8.4 path and query arrive below the target's base path",
                   b["path"] == "/base/hello/x?q=1&y=%2F", b["path"])
                ok("2.8.4 Host is the target's", sent.get("Host") == f"{bhost}:{bport}", sent.get("Host"))
                ok("2.8.4 the target may trust the identity headers",
                   sent.get("X-OAAP-User") == "alice", sent)
                ok("2.8.4 the NODE'S SESSION COOKIE does not reach the target",
                   "oaap_session" not in sent.get("Cookie", ""), sent.get("Cookie"))
                ok("2.8.4 ... the target's own cookies do",
                   "theme=dark" in sent.get("Cookie", "") and "other=1" in sent.get("Cookie", ""),
                   sent.get("Cookie"))
                ok("2.8.4 a platform API key does not reach the target",
                   "Authorization" not in sent, sent)
                leaked = [h for h in sent if h.lower().startswith("x-oaap-connect")
                          or h.lower() in ("x-forwarded-for", "x-forwarded-host")]
                ok("the gateway's own headers and X-Forwarded-* are not passed on", not leaked, leaked)
            s, hd, sc, b = await exposed("/setcookie", headers={"X-OAAP-User": "alice"})
            ok("2.8.4 the target cannot set the node's session ...",
               all(not c.startswith("oaap_session") for c in sc), sc)
            ok("2.8.4 ... its own cookies pass", any(c.startswith("theme=dark") for c in sc), sc)
            s, *_ = await exposed("/x", headers={"Upgrade": "websocket", "Connection": "Upgrade"})
            ok("2.8.4 a WebSocket through the tunnel -> 501", s == 501, s)
            SEEN.clear()
            s, *_ = await exposed("/a/%2e%2e/secret")
            ok("2.6 %2e%2e -> 403 and the target receives nothing", s == 403 and SEEN == [], (s, SEEN))
            s, hd, sc, b = await exposed("/x", host_="nosuchname0." + ZONE)
            ok("2.8.4 /exposed for a name that is no exposure -> 404", s == 404, s)
            s, *_ = await exposed("/x", key="wrong")
            ok("/exposed with a wrong gateway key -> 403", s == 403, s)
            s, *_ = await exposed("/x", key="")
            ok("/exposed without the gateway's key -> 403", s == 403, s)
            blob = os.urandom(1024 * 1024 + 5)
            SEEN.clear()
            s, hd, sc, b = await exposed("/upload", method="POST", data=blob)
            ok("a 1 MiB body arrives whole", s == 200 and b.get("len") == len(blob), (s, b))

            # logs: metadata, never a header value or a body
            log_out = os.path.join(outer["state"], "log", "exposures.jsonl")
            txt = open(log_out, encoding="utf-8").read()
            lines = [json.loads(x) for x in txt.splitlines()]
            ok("2.8.8 the outer stream log has a line per call, naming the visitor",
               any(l.get("caller") == "alice" and l.get("name") == name and l.get("status") == 200
                   for l in lines), lines[:2])
            ok("2.8.8 ... and no cookie, key or query",
               "oaapk_" not in txt and "oaap_session" not in txt and "q=1" not in txt)
            counted = await until(lambda: read_state(outer)["exposures"][name]["calls"] >= 3, 5)
            ok("2.8.9 the state carries the number of calls (found live: it stayed 0)", counted,
               read_state(outer)["exposures"][name])
            ilog = open(os.path.join(inner["state"], "log", "connector-x01.jsonl"), encoding="utf-8").read()
            ok("the inner stream log says whose exposure it was", "exposure:x-aaa111" in ilog)

            # --- TTL and the sweep (2.8.6) ----------------------------------
            s, _, _ = await verify("ttlexpire1." + ZONE, "alice")
            ok("2.8.6 an exposure that still has time is served", s == 204, s)
            gone = await until(lambda: "ttlexpire1" not in read_state(outer).get("exposures", {}), 16)
            ok("2.8.6 the sweep removes it once its time has run out", gone, read_state(outer).get("exposures"))
            s, _, _ = await verify("ttlexpire1." + ZONE, "alice")
            ok("2.8.6 ... and the next request is a 404", s == 404, s)
            ok("2.8.8 ... with one line: expired",
               any(e["action"] == "exposure.expire" and e["subject"] == "ttlexpire1." + ZONE
                   for e in audit_lines(audit_path)))
            told = await until(lambda: (read_state(inner)["connectors"]["x01"]["exposures"]
                                        .get("x-ttl001") or {}).get("expired") == "ttl", 5)
            ok("2.8.2 the inner side is told (`expired`, reason ttl)", told,
               read_state(inner)["connectors"]["x01"]["exposures"])

            # --- resume: same name, nothing longer (2.8.2) ------------------
            before = dict(read_state(outer)["exposures"][name])
            names_before = set(read_state(outer)["exposures"])
            inner_conf["connectors"]["x01"]["paused"] = True
            write_json(os.path.join(inner["apps"], "connect.json"), inner_conf)
            down = await until(lambda: not read_state(outer)["exposures"][name]["connected"], 8)
            ok("a paused connector closes its tunnel; the exposure stays until its time", down)
            s, *_ = await exposed("/x")
            ok("2.8.4 ... and a call meanwhile is a 502 that says why", s == 502, s)
            inner_conf["connectors"]["x01"]["paused"] = False
            write_json(os.path.join(inner["apps"], "connect.json"), inner_conf)
            back = await until(lambda: read_state(outer)["exposures"].get(name, {}).get("connected"), 10)
            settled = await until(lambda: ref_state().get("url"), 8)
            ok("2.8.2 the reconnecting connector gets its name back -- and opens no second one",
               back and settled and ref_state().get("name") == name
               and set(read_state(outer)["exposures"]) == names_before,
               (ref_state(), list(read_state(outer)["exposures"]), names_before))
            after = read_state(outer)["exposures"][name]
            ok("2.8.2 ... and a reconnect is not an extension (the end does not move)",
               after["expires"] == before["expires"], (before["expires"], after["expires"]))
            ok("2.8.8 ... and writes no extension line",
               not [e for e in audit_lines(audit_path) if e["action"] == "exposure.extend"])

            # --- extension (2.8.3 rule 4): a later end is an extension ------
            inner_conf["connectors"]["x01"]["exposures"]["x-aaa111"]["expires_at"] = time.time() + 7200
            write_json(os.path.join(inner["apps"], "connect.json"), inner_conf)
            ext = await until(lambda: read_state(outer)["exposures"][name]["expires"] > before["expires"], 8)
            ok("2.8.3 a later end is an extension, and the name stays", ext
               and ref_state().get("name") == name)
            ok("2.8.8 ... and it is written down as one",
               any(e["action"] == "exposure.extend" and e["subject"] == host
                   for e in audit_lines(audit_path)))

            # --- public (2.8.4) ---------------------------------------------
            inner_conf["connectors"]["x01"]["exposures"]["x-pub222"] = {
                "target": target, "public": True, "expires_at": time.time() + 600}
            write_json(os.path.join(inner["apps"], "connect.json"), inner_conf)
            pub = await until(lambda: (read_state(inner)["connectors"]["x01"]["exposures"]
                                       .get("x-pub222") or {}).get("url"), 8)
            ok("a public exposure is opened", pub)
            pname = read_state(inner)["connectors"]["x01"]["exposures"]["x-pub222"]["name"]
            phost = f"{pname}.{ZONE}"
            ok("2.8.8 ... and named public in the log",
               any(e["action"] == "exposure.open" and e["subject"] == phost and "public" in e["detail"]
                   for e in audit_lines(audit_path)))
            s, hd, _ = await verify(phost)
            ok("2.8.4 public: no login, and the identity headers arrive EMPTY (a forged one is "
               "overwritten, not passed)", s == 204 and all(hd.get(h) == "" for h in
                                                          connect.IDENTITY_HEADERS), (s, hd))
            n429 = 0
            for _ in range(130):
                s, hd, _ = await verify(phost)
                n429 += s == 429
            ok("2.8.4 public: the brake -- 120 in 60 s per client, then 429 with Retry-After",
               n429 >= 10 and hd.get("Retry-After") == "60", (n429, hd))
            async with http.get(base + "/exposure/verify",
                                headers=gwh(phost, client="8.8.8.8")) as r:
                ok("2.8.4 ... another client is not braked", r.status == 204, r.status)

            # --- the laptop client (2.8.5) ----------------------------------
            async def run_client(key, tenant="club", ttl="2h", public=False, target_=None):
                args = [sys.executable, CLIENT, target_ or f"http://127.0.0.1:{bport}/dev",
                        "--server", base, "--tenant", tenant, "--ttl", ttl]
                if public:
                    args.append("--public")
                p = await asyncio.create_subprocess_exec(
                    *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                    env=dict(os.environ, OAAP_KEY=key, PYTHONUNBUFFERED="1"))
                clients.append(p)
                return p

            async def first_output(p, needle, timeout=10):
                buf = b""
                end = time.monotonic() + timeout
                while time.monotonic() < end:
                    try:
                        chunk = await asyncio.wait_for(p.stdout.read(512), 1.0)
                    except asyncio.TimeoutError:
                        continue
                    if not chunk:
                        break
                    buf += chunk
                    if needle.encode() in buf:
                        break
                return buf.decode(errors="replace")

            for key, why, needle in (("oaapk_plain", "a key without tenant_admin", "tenant_admin"),
                                     ("oaapk_foreign", "a key of another tenant", "403"),
                                     ("oaapk_nobody", "an unknown key", "401")):
                p = await run_client(key)
                outp = await first_output(p, needle)
                code = await asyncio.wait_for(p.wait(), 10)
                ok(f"2.8.5 {why}: the client says why and ends", needle in outp and code != 0,
                   (code, outp))
            p = await run_client("oaapk_admin", tenant="nonesuch")
            outp = await first_output(p, "403")
            await asyncio.wait_for(p.wait(), 10)
            ok("2.8.5 an unknown tenant is a 403 like another tenant's", "403" in outp, outp)

            p = await run_client("oaapk_admin", ttl="2h")
            outp = await first_output(p, ".t.example.org")
            url = next((w for w in outp.split() if ".t.example.org" in w), "")
            ok("2.8.5 a tenant_admin's client is given an address under the zone",
               url.startswith("https://") and url.rstrip("/").endswith(".t.example.org"), outp)
            lname = url.split("//")[1].split(".")[0] if url else ""
            await until(lambda: any(x["user"] == "jm" for x in read_state(outer).get("people", []))
                        and lname in read_state(outer).get("exposures", {}), 5)
            ost = read_state(outer)
            ok("2.8.5 the outer node lists the laptop's tunnel and the exposure",
               any(x["user"] == "jm" and x["tenant"] == "t1" for x in ost.get("people", []))
               and ost["exposures"].get(lname, {}).get("opened_by") == "jm"
               and ost["exposures"][lname]["owner"] == "p:jm:t1", ost)
            ok("2.8.5 ... which is not a tunnel a destination can name",
               not [l for l in ost["tunnels"] if l.startswith("p-")])
            ok("2.8.8 the laptop's decision is in the tenant's log, as the person",
               any(e["action"] == "exposure.open" and e["who"] == "jm" and e["role"] == "tenant_admin"
                   and e["subject"] == f"{lname}.{ZONE}" for e in audit_lines(audit_path)))
            # found live on oaap-test: a change to connect.json (the operator did
            # ANYTHING) closed every laptop with "key revoked" -- a person's key is
            # not in that file, and reconcile judged every tunnel by it
            outer_conf["note"] = time.time()
            write_json(os.path.join(outer["apps"], "connect.json"), outer_conf)
            await asyncio.sleep(3.5)
            ok("2.8.5 a change to connect.json does not close a laptop's tunnel",
               p.returncode is None and any(x["user"] == "jm" for x in read_state(outer).get("people", [])),
               (p.returncode, read_state(outer).get("people")))
            SEEN.clear()
            s, hd, sc, b = await exposed("/from/the/laptop?a=1", host_=f"{lname}.{ZONE}",
                                         headers={"X-OAAP-User": "alice"})
            ok("2.8.5 a visitor's call reaches the laptop's target", s == 200
               and b.get("path") == "/dev/from/the/laptop?a=1", (s, b))
            s, hd, _ = await verify(f"{lname}.{ZONE}", "alice")
            ok("2.8.5 the laptop's exposure is behind the login of ITS tenant",
               s == 204, s)
            s, hd, _ = await verify(f"{lname}.{ZONE}", "bob")
            ok("... and not open to another tenant's users", s == 403, s)

            # a person's raw tunnel: hello with offers is ignored, no via
            async with http.ws_connect(base + "/connect/tunnel", protocols=(connect.SUBPROTOCOL,),
                                       headers={"Authorization": "Bearer oaapk_admin",
                                                "X-OAAP-Tenant": "t1"}) as ws:
                await ws.send_str(json.dumps({"t": "hello", "connector": "x", "version": 1,
                                              "offers": [{"name": "sneaky", "kind": "http"}]}))
                await ws.send_str(json.dumps({"t": "expose", "ref": "raw-1", "ttl": 3600}))
                msg = json.loads((await ws.receive(timeout=5)).data)
                ok("2.8.2 an `expose` is answered with `exposed`",
                   msg["t"] == "exposed" and msg["ref"] == "raw-1" and len(msg["name"]) == 10, msg)
                rname = msg["name"]
                await ws.send_str(json.dumps({"t": "expose", "ref": "bad ref!", "ttl": 3600}))
                msg2 = json.loads((await ws.receive(timeout=5)).data)
                ok("2.8.3 a malformed ref is refused with a sentence",
                   msg2["t"] == "expose-refused" and "ref" in msg2["reason"], msg2)
                await ws.send_str(json.dumps({"t": "expose", "ref": "raw-2", "ttl": 0}))
                msg3 = json.loads((await ws.receive(timeout=5)).data)
                ok("2.8.3 no time left at all is refused", msg3["t"] == "expose-refused"
                   and "at least" in msg3["reason"], msg3)
                await ws.send_str(json.dumps({"t": "expose", "ref": "raw-4", "ttl": 59}))
                msg3b = json.loads((await ws.receive(timeout=5)).data)
                ok("2.8.3 59 seconds LEFT is honoured -- the minimum of a request is the "
                   "requester's rule (found live: `--ttl 60s` arrived as 59)",
                   msg3b["t"] == "exposed", msg3b)
                await ws.send_str(json.dumps({"t": "expose", "ref": "raw-5", "ttl": 99999999}))
                msg3c = json.loads((await ws.receive(timeout=5)).data)
                exp = time.mktime(time.strptime(msg3c["expires"], "%Y-%m-%dT%H:%M:%SZ")) - time.timezone
                ok("2.8.3 more than 7 days is cut to 7 days, not honoured", msg3c["t"] == "exposed"
                   and exp - time.time() < 7 * 86400 + 60, msg3c)
                await ws.send_str(json.dumps({"t": "expose", "ref": "raw-3", "ttl": 3600,
                                              "resume": rname}))
                msg4 = json.loads((await ws.receive(timeout=5)).data)
                ok("2.8.2 resume needs the SAME ref: another ref gets a NEW name",
                   msg4["t"] == "exposed" and msg4["name"] != rname, msg4)
                await ws.send_str(json.dumps({"t": "expose", "ref": "raw-1", "ttl": 3600,
                                              "public": True, "resume": rname}))
                msg5 = json.loads((await ws.receive(timeout=5)).data)
                ok("2.8.2 visibility cannot be changed by resuming", msg5["t"] == "expose-refused"
                   and "public" in msg5["reason"] or "login" in msg5.get("reason", ""), msg5)
                await until(lambda: rname in read_state(outer).get("exposures", {}), 5)
                await ws.send_str(json.dumps({"t": "unexpose", "ref": "raw-1"}))
                gone = await until(lambda: rname not in read_state(outer)["exposures"], 5)
                ok("2.8.2 `unexpose` ends it at once", gone)
                ok("2.8.8 ... and is one line: closed by its owner",
                   any(e["action"] == "exposure.close" and e["subject"] == f"{rname}.{ZONE}"
                       and e["who"] == "jm" for e in audit_lines(audit_path)),
                   audit_lines(audit_path)[-4:])
                ost = read_state(outer)
                ok("2.8.5 the person's offers were ignored", not any("sneaky" in json.dumps(t)
                                                                      for t in ost["tunnels"].values()))
                s, *_ = await exposed("/x", host_=f"{rname}.{ZONE}")
                ok("2.8.6 the closed name is a 404", s == 404, s)

            # the client goes away: the exposure stays until its time
            p = clients[-1]
            p.terminate()
            await asyncio.wait_for(p.wait(), 10)
            off = await until(lambda: not read_state(outer)["exposures"].get(lname, {"connected": 1})["connected"], 8)
            ok("2.8.6 a laptop that went away leaves the exposure standing, unconnected", off,
               read_state(outer)["exposures"].get(lname))
            s, *_ = await exposed("/x", host_=f"{lname}.{ZONE}")
            ok("2.8.4 ... and a call meanwhile is a 502 that says why", s == 502, s)

            # --- a key that stops holding ends the tunnel and what it opened (2.8.6)
            p = await run_client("oaapk_admin", ttl="1h")
            outp = await first_output(p, ".t.example.org")
            url2 = next((w for w in outp.split() if ".t.example.org" in w), "")
            n2 = url2.split("//")[1].split(".")[0] if url2 else ""
            up2 = await until(lambda: n2 in read_state(outer).get("exposures", {}), 5)
            ok("a second laptop exposure is opened", up2, outp)
            REVOKED.add("oaapk_admin")
            ended = await until(lambda: n2 not in read_state(outer).get("exposures", {}), 8)
            ok("2.8.6 revoking the key ends the tunnel and every exposure its owner opened", ended,
               read_state(outer).get("exposures"))
            code = await asyncio.wait_for(p.wait(), 10)
            outp2 = await first_output(p, "no longer accepts", 1) if code is None else ""
            ok("2.8.5 the client ends with a sentence about the key", code != 0, code)
            ok("2.8.8 ... and the log says the key was revoked",
               any(e["action"] == "exposure.close" and "revoked" in e.get("detail", "")
                   for e in audit_lines(audit_path)))
            REVOKED.discard("oaapk_admin")

            # --- the operator closes one (2.8.6) ----------------------------
            outer_conf["exposure_closed"] = {name: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            write_json(os.path.join(outer["apps"], "connect.json"), outer_conf)
            closed = await until(lambda: name not in read_state(outer).get("exposures", {}), 10)
            ok("2.8.6 `exposure close` by the operator ends it within seconds", closed)
            ok("2.8.8 ... as the operator, in the tenant's log",
               any(e["action"] == "exposure.close" and e["subject"] == host and e["who"] == "operator"
                   for e in audit_lines(audit_path)))
            told = await until(lambda: (ref_state().get("expired") == "closed"), 6)
            ok("2.8.2 the inner side is told (`expired`, reason closed)", told, ref_state())

            # the operator's `close` sticks: not on a reconnect, not on a restart of the
            # inner side (found live: after a restart the closed exposure came back
            # under a NEW name, because the inner side asked again)
            mine = lambda: {n for n, e in read_state(outer).get("exposures", {}).items()
                            if e["owner"] == "c:x01"}
            before_names = mine()
            inner_conf["connectors"]["x01"]["paused"] = True
            write_json(os.path.join(inner["apps"], "connect.json"), inner_conf)
            await until(lambda: not read_state(outer)["tunnels"]["x01"]["connected"], 8)
            inner_conf["connectors"]["x01"]["paused"] = False
            write_json(os.path.join(inner["apps"], "connect.json"), inner_conf)
            await until(lambda: read_state(outer)["tunnels"]["x01"]["connected"], 10)
            await asyncio.sleep(2.5)
            ok("2.8.6 a closed exposure does not come back when the connector reconnects",
               mine() == before_names and ref_state().get("expired") == "closed",
               (mine(), before_names, ref_state()))
            inner["proc"].terminate()
            inner["proc"].wait(5)
            start(inner)
            await until(lambda: read_state(outer)["tunnels"]["x01"]["connected"], 10)
            await asyncio.sleep(3)
            ok("2.8.6 ... nor when the inner service restarts (the end is remembered)",
               mine() == before_names and ref_state().get("expired") == "closed",
               (mine(), before_names, ref_state()))
            async with http.ws_connect(base + "/connect/tunnel", protocols=(connect.SUBPROTOCOL,),
                                       headers={"Authorization": "Bearer " + KEY}) as ws2:
                await ws2.send_str(json.dumps({"t": "hello", "connector": "x01", "offers": [],
                                               "version": 1}))
                await ws2.send_str(json.dumps({"t": "expose", "ref": "x-aaa111", "ttl": 3600,
                                               "resume": name}))
                got = json.loads((await ws2.receive(timeout=5)).data)
                ok("2.8.6 and a request that resumes a closed name is refused, not honoured "
                   "as a new one", got["t"] == "expose-refused" and "closed" in got["reason"], got)

            # --- the connect key is revoked: what it opened ends ------------
            pname_alive = pname in read_state(outer).get("exposures", {})
            ok("(the public one is still there before the key goes)", pname_alive)
            outer_conf["keys"] = {}
            write_json(os.path.join(outer["apps"], "connect.json"), outer_conf)
            ended = await until(lambda: pname not in read_state(outer).get("exposures", {}), 10)
            ok("2.8.6 revoking the connect key ends the exposures its tunnel held", ended,
               read_state(outer).get("exposures"))

            # --- the client for download (2.8.5) ----------------------------
            async with http.get(base + "/connect/client") as r:
                body = await r.text()
                ok("2.8.5 /connect/client serves the client file", r.status == 200
                   and "oaap-expose" in body and "aiohttp" in body, r.status)
                ok("... with no key of any kind in it", "oaapk_" not in body.replace("oaapk_...", "")
                   .replace("startswith(\"oaapk_\")", "").replace("(it starts with oaapk_", "")
                   .replace("oaapk_\"", ""), "")

            # the persisted exposures survive a restart of the outer service
            outer_conf["keys"] = {"x01": {"hash": hashlib.sha256(KEY.encode()).hexdigest(), "tenant": "t1"}}
            write_json(os.path.join(outer["apps"], "connect.json"), outer_conf)
    finally:
        for p in clients:
            try:
                p.kill()
            except ProcessLookupError:
                pass
        for s_ in (outer, inner):
            s_["proc"].terminate()
            try:
                s_["proc"].wait(5)
            except subprocess.TimeoutExpired:
                s_["proc"].kill()
        await brunner.cleanup()
        await irunner.cleanup()
        for s_ in (outer, inner):
            s_["log"].close()
        if fails:
            for n in ("outer", "inner"):
                try:
                    print(f"--- {n} stderr ---")
                    print(open(os.path.join(root, n, "stderr.txt")).read()[-1500:])
                except OSError:
                    pass


asyncio.run(main())
print("")
print("OK" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
