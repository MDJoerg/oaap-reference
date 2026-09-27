#!/usr/bin/env python3
"""Portweiterleitung, echter Prozess (RFC-0044 §4, oaap.net.remote-access
0.2).

Der Verbindungsdienst läuft einmal (kein "aussen"/"innen" -- eine
Portweiterleitung ist ein einziger Sprung, nie zwischen zwei Knoten),
dazu eine gespielte Anmeldung (identity) und ein einfaches TCP-Echo als
Ersatz für den Container, den der Zugang benennt. Kein appctl, kein
Docker -- der Zugang wird direkt in remote-access.json geschrieben, wie
appctl es täte.

Festgehalten werden die Regeln von §4, nicht das heutige Verhalten:

- ein WebSocket je weitergeleiteter TCP-Verbindung, roh, ohne eigene
  Rahmung -- ein einziger Sprung, nichts sonst wird multiplext;
- der Dienst wählt das Ziel SELBST, aus dem Zugang -- die Anfrage
  liefert nur eine Zugangs-Id, nie eine Adresse;
- nur der API-Schlüssel des EIGENEN Inhabers öffnet; ein Schlüssel eines
  anderen wird abgelehnt, ohne zu verraten, wem der Zugang gehört;
- eine unbekannte, abgelaufene oder nicht-'forward'-Zugangs-Id wird mit
  einem eigenen Satz beantwortet (404/410), nie einfach 401;
- jede erfolgreiche Verbindung steht im Mandantenprotokoll, mit der
  Person, nicht mit dem Ziel.

Braucht aiohttp (wie der Dienst selbst).
Aufruf: python3 test/test_forward_tunnel.py
"""
import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone

import aiohttp
from aiohttp import web

HERE = os.path.dirname(os.path.abspath(__file__))
SERVICE = os.path.join(HERE, "..", "platform", "services", "connect", "app.py")

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


def iso(delta_hours):
    return (datetime.now(timezone.utc) + timedelta(hours=delta_hours)).isoformat(timespec="seconds")


def audit_lines(path):
    try:
        with open(path, encoding="utf-8") as f:
            return [json.loads(x) for x in f if x.strip()]
    except OSError:
        return []


# --- a stand-in for the container the access names --------------------------
async def handle_echo(reader, writer):
    try:
        while True:
            data = await reader.read(4096)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (OSError, ConnectionResetError):
        pass
    writer.close()


# --- a stand-in for identity --------------------------------------------------
KEYS = {"oaapk_karin": ("karin", "t1", "tenant_admin,user"),
        "oaapk_uwe": ("uwe", "t1", "user")}


async def ident_verify(request):
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return web.Response(status=401, text="no key")
    k = auth[7:]
    if k not in KEYS:
        return web.Response(status=401, text="unknown key")
    user, tenant, roles = KEYS[k]
    return web.Response(status=204, headers={
        "X-OAAP-User": user, "X-OAAP-Roles": roles, "X-OAAP-User-Id": "id-" + user,
        "X-OAAP-Display-Name": user.title(), "X-OAAP-Email": ""})


async def main():
    root = tempfile.mkdtemp(prefix="oaap-forward-test-")
    apps, secrets, state, audit_dir = (os.path.join(root, d)
                                       for d in ("apps", "secrets", "state", "audit"))
    for d in (apps, secrets, state, audit_dir):
        os.makedirs(d, exist_ok=True)
    write_json(os.path.join(apps, "connect.json"), {"schema": "0.2", "keys": {}, "connectors": {}})
    with open(os.path.join(secrets, "gateway.key"), "w") as f:
        f.write("gw-" + "g" * 40)

    # --- the echo "container" ------------------------------------------------
    eport = free_port()
    echo_srv = await asyncio.start_server(handle_echo, "127.0.0.1", eport)

    # --- identity ------------------------------------------------------------
    iport = free_port()
    iapp = web.Application()
    iapp.router.add_get("/verify", ident_verify)
    irunner = web.AppRunner(iapp)
    await irunner.setup()
    await web.TCPSite(irunner, "127.0.0.1", iport).start()

    # --- the access records (as appctl would write them) --------------------
    LIVE = {"id": "live0001", "instance": "kunde-orders", "tenant": "t1",
            "shape": "forward", "target": {"service": "db", "port": eport,
                                           "container": "127.0.0.1"},
            "holder": "karin", "opened_by": "karin", "opened": iso(-1),
            "expires": iso(7), "hours": 8, "state": "open"}
    EXPIRED = {**LIVE, "id": "expired1", "expires": iso(-1)}
    WIREGUARD = {**LIVE, "id": "wg000001", "shape": "wireguard"}
    write_json(os.path.join(apps, "remote-access.json"), {"schema": "0.1", "accesses": {
        LIVE["id"]: LIVE, EXPIRED["id"]: EXPIRED, WIREGUARD["id"]: WIREGUARD}})

    # --- the connect service itself ------------------------------------------
    cport = free_port()
    audit_path = os.path.join(audit_dir, "tenant-log.jsonl")
    env = dict(os.environ, CONNECT_APPS=apps, CONNECT_SECRETS=secrets, CONNECT_STATE=state,
              CONNECT_AUDIT=audit_path, CONNECT_PORT=str(cport),
              CONNECT_IDENTITY=f"http://127.0.0.1:{iport}", PYTHONUNBUFFERED="1")
    logf = open(os.path.join(root, "stderr.txt"), "w")
    proc = subprocess.Popen([sys.executable, SERVICE], env=env, stdout=logf, stderr=subprocess.STDOUT)

    base = f"http://127.0.0.1:{cport}"
    try:
        healthy = False
        for _ in range(60):
            try:
                async with aiohttp.ClientSession() as s:
                    async with s.get(base + "/healthz",
                                     timeout=aiohttp.ClientTimeout(total=1)) as r:
                        if r.status == 200:
                            healthy = True
                            break
            except (aiohttp.ClientError, OSError, asyncio.TimeoutError):
                pass
            await asyncio.sleep(0.2)
        ok("der Dienst antwortet auf /healthz", healthy)

        async with aiohttp.ClientSession() as http:
            # --- der eigentliche Weg: der Inhaber verbindet, Bytes gehen durch ---
            async with http.ws_connect(base + "/connect/forward",
                                       params={"access": LIVE["id"]},
                                       protocols=("oaap-forward.1",),
                                       headers={"Authorization": "Bearer oaapk_karin"}) as ws:
                await ws.send_bytes(b"hallo welt")
                msg = await ws.receive(timeout=5)
                ok("§4: der Inhaber verbindet, und Bytes kommen durch das Ziel zurueck "
                   "(WebSocket <-> TCP, roh, ohne eigene Rahmung)",
                   msg.type == aiohttp.WSMsgType.BINARY and msg.data == b"hallo welt", msg)
                await ws.close()
            ok("§4: die Verbindung steht im Mandantenprotokoll, mit der Person, nicht dem Ziel",
               any(e["action"] == "access.forward.connected" and e["who"] == "karin"
                   and e["tenant"] == "t1" and "127.0.0.1" not in json.dumps(e)
                   for e in audit_lines(audit_path)), audit_lines(audit_path))

            # --- ein Schluessel, der nicht dem Inhaber gehoert ---
            try:
                async with http.ws_connect(base + "/connect/forward",
                                           params={"access": LIVE["id"]},
                                           protocols=("oaap-forward.1",),
                                           headers={"Authorization": "Bearer oaapk_uwe"}) as ws:
                    ok("§4: ein Schluessel, der nicht dem Inhaber gehoert, wird abgelehnt",
                       False, "die Verbindung haette nicht zustande kommen duerfen")
            except aiohttp.WSServerHandshakeError as e:
                ok("§4: ein Schluessel, der nicht dem Inhaber gehoert, wird mit 403 abgelehnt",
                   e.status == 403, e.status)

            # --- kein Schluessel / kein oaapk_-Praefix ---
            try:
                async with http.ws_connect(base + "/connect/forward",
                                           params={"access": LIVE["id"]},
                                           protocols=("oaap-forward.1",)) as ws:
                    ok("§4: ohne Schluessel wird abgelehnt", False, "durchgekommen")
            except aiohttp.WSServerHandshakeError as e:
                ok("§4: ohne Schluessel gibt es 401", e.status == 401, e.status)

            # --- unbekannte Zugangs-Id ---
            try:
                async with http.ws_connect(base + "/connect/forward",
                                           params={"access": "unbekannt"},
                                           protocols=("oaap-forward.1",),
                                           headers={"Authorization": "Bearer oaapk_karin"}) as ws:
                    ok("§4: eine unbekannte Zugangs-Id wird abgelehnt", False, "durchgekommen")
            except aiohttp.WSServerHandshakeError as e:
                ok("§4: eine unbekannte Zugangs-Id gibt 404, nicht 401",
                   e.status == 404, e.status)

            # --- abgelaufene Zugangs-Id ---
            try:
                async with http.ws_connect(base + "/connect/forward",
                                           params={"access": EXPIRED["id"]},
                                           protocols=("oaap-forward.1",),
                                           headers={"Authorization": "Bearer oaapk_karin"}) as ws:
                    ok("§4: eine abgelaufene Zugangs-Id wird abgelehnt", False, "durchgekommen")
            except aiohttp.WSServerHandshakeError as e:
                ok("§4: eine abgelaufene Zugangs-Id gibt 410", e.status == 410, e.status)

            # --- shape 'wireguard' -- gehoert nicht diesem Weg ---
            try:
                async with http.ws_connect(base + "/connect/forward",
                                           params={"access": WIREGUARD["id"]},
                                           protocols=("oaap-forward.1",),
                                           headers={"Authorization": "Bearer oaapk_karin"}) as ws:
                    ok("§4: ein Zugang der Form 'wireguard' wird hier abgelehnt", False, "durchgekommen")
            except aiohttp.WSServerHandshakeError as e:
                ok("§4: 'wireguard' antwortet wie 'unbekannt' (404), nicht wie 'forward'",
                   e.status == 404, e.status)

            # --- zwei Verbindungen gleichzeitig, unabhaengig ---
            async with http.ws_connect(base + "/connect/forward",
                                       params={"access": LIVE["id"]},
                                       protocols=("oaap-forward.1",),
                                       headers={"Authorization": "Bearer oaapk_karin"}) as ws1, \
                      http.ws_connect(base + "/connect/forward",
                                       params={"access": LIVE["id"]},
                                       protocols=("oaap-forward.1",),
                                       headers={"Authorization": "Bearer oaapk_karin"}) as ws2:
                await ws1.send_bytes(b"eins")
                await ws2.send_bytes(b"zwei")
                m1 = await ws1.receive(timeout=5)
                m2 = await ws2.receive(timeout=5)
                ok("§4: zwei Verbindungen desselben Zugangs sind unabhaengig",
                   m1.data == b"eins" and m2.data == b"zwei", (m1.data, m2.data))

        # --- der echte Laptop-Client, als eigener Prozess ------------------
        CLIENT = os.path.join(HERE, "..", "platform", "services", "connect",
                              "client", "oaap-expose.py")
        lport = free_port()
        cli = subprocess.Popen(
            [sys.executable, CLIENT, "forward", "--access", LIVE["id"],
             "--server", base, "--local-port", str(lport)],
            env=dict(os.environ, OAAP_KEY="oaapk_karin", PYTHONUNBUFFERED="1"),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            # NICHT per Verbinden-und-sofort-Trennen pruefen: eine TCP-
            # Verbindung ohne jede Anfrage lässt aiohttps Anfrage-Parser
            # auf Daten warten, die nie kommen -- gefunden hier, als
            # genau das einen Verbindungs-Handler des Knotens fuer immer
            # haengen liess (ein echter Client sendet immer etwas).
            # Stattdessen liest die eigene Meldung des Clients ab.
            line = await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(None, cli.stdout.readline), 5)
            ok("der Client hoert lokal, sobald er gestartet ist",
               "listening on" in line, line)
            # Async, nicht ueber das blockierende socket-Modul: dieser
            # Testprozess trägt selbst die gespielte Anmeldung auf
            # DEMSELBEN Event-Loop -- ein blockierender Aufruf hier legt
            # sie fuer die Dauer lahm, und der Knoten, der gerade auf
            # genau diese Anmeldung wartet, bekommt keine Antwort. Genau
            # das war der erste Befund an dieser Stelle: kein Fehler im
            # Client, ein blockierter Aufruf im TEST.
            got = b""
            try:
                r, w = await asyncio.wait_for(
                    asyncio.open_connection("127.0.0.1", lport), timeout=5)
                w.write(b"durch die ganze kette")
                await w.drain()
                got = await asyncio.wait_for(r.read(4096), timeout=5)
                w.close()
            except (OSError, asyncio.TimeoutError) as e:
                got = str(e).encode()
            passed = got == b"durch die ganze kette"
        finally:
            cli.terminate()
            loop = asyncio.get_event_loop()
            try:
                out = await asyncio.wait_for(
                    loop.run_in_executor(None, lambda: cli.communicate(timeout=5)[0]), 8)
            except (asyncio.TimeoutError, subprocess.TimeoutExpired):
                cli.kill()
                out = await loop.run_in_executor(None, lambda: cli.communicate(timeout=5)[0])
        ok("§4: der echte Client -- lokaler TCP-Socket, WebSocket zum Knoten, "
           "TCP zum Ziel -- gibt genau die gesendeten Bytes zurueck",
           passed, (got, out))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        echo_srv.close()
        await echo_srv.wait_closed()

    print("")
    print("PASS" if not fails else f"{fails} FEHLER")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    asyncio.run(main())
