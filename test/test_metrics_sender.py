#!/usr/bin/env python3
"""Der Metrik-Sender (RFC-0052, Stufe 1) gegen einen Schein-Broker.

Der Schein-Broker spricht MQTT 5 und prueft das Protokoll selbst nach
(Protokollname, Version, Flags), statt den Code des Senders zu benutzen.
Geprueft wird, was die Spezifikation verspricht und was am ehesten still
schiefginge:

    geloescht wird erst nach der Bestaetigung des Brokers, ein Absturz
    dazwischen sendet doppelt (mindestens einmal), ein verweigertes
    Veroeffentlichen loescht NICHTS, ein Wiederholen wartet (Backoff),
    ein falsches Passwort wartet die lange Zeit, TLS-Fehler senden nichts,
    und das Passwort steht in keinem Text.

Braucht kein Docker, keinen Knoten, keine Bibliothek.

Aufruf: python3 test/test_metrics_sender.py
"""
import contextlib
import io
import json
import os
import socket
import struct
import sys
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services"))
sys.path.insert(0, os.path.join(HERE, "..", "platform"))

import metrics as m                                            # noqa: E402
import metrics_sender as s                                     # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:500]}")


# --- ein Schein-Broker, MQTT 5 ------------------------------------------

def _read(conn):
    first = conn.recv(1)
    if not first:
        return None
    mult, length = 1, 0
    while True:
        b = conn.recv(1)[0]
        length += (b & 127) * mult
        if not b & 128:
            break
        mult *= 128
    body = b""
    while len(body) < length:
        chunk = conn.recv(length - len(body))
        if not chunk:
            return None
        body += chunk
    return first[0], body


def _str(body, i):
    n = struct.unpack(">H", body[i:i + 2])[0]
    return body[i + 2:i + 2 + n].decode(), i + 2 + n


class Broker:
    def __init__(self, users, can_write=None, drop_on_publish=None,
                 connack_rc=None):
        self.users = users
        self.can_write = can_write or (lambda user, topic: True)
        self.drop_on_publish = drop_on_publish      # n-te Nachricht: ohne PUBACK trennen
        self.connack_rc = connack_rc
        self.received = []                          # (user, topic, payload, retain)
        self.connects = 0
        self.protocol_errors = []
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(5)
        self.port = self.srv.getsockname()[1]
        self.running = True
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while self.running:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        user = None
        try:
            conn.settimeout(10)
            pkt = _read(conn)
            if not pkt or pkt[0] != 0x10:                   # kein CONNECT (z. B. ein TLS-Handschlag)
                conn.close()
                return
            self.connects += 1
            body = pkt[1]
            name, i = _str(body, 0)
            level, flags = body[i], body[i + 1]
            if name != "MQTT" or level != 5 or flags != 0xC2:
                self.protocol_errors.append((name, level, flags))
            i += 2 + 2                              # flags, keepalive
            i += 1 + body[i]                        # properties
            _cid, i = _str(body, i)
            user, i = _str(body, i)
            pw, i = _str(body, i)
            good = self.users.get(user) == pw
            rc = self.connack_rc if self.connack_rc is not None else (0 if good else 0x86)
            conn.sendall(bytes([0x20, 3, 0, rc, 0]))
            if rc:
                conn.close()
                return
            while True:
                pkt = _read(conn)
                if pkt is None or pkt[0] >> 4 == 14:
                    break
                if pkt[0] >> 4 != 3:
                    continue
                retain = bool(pkt[0] & 1)
                if (pkt[0] >> 1) & 3 != 1:
                    self.protocol_errors.append(("qos", pkt[0], 0))
                body = pkt[1]
                topic, i = _str(body, 0)
                pid = body[i:i + 2]
                i += 2
                i += 1 + body[i]                    # properties
                payload = body[i:]
                if self.can_write(user, topic):
                    self.received.append((user, topic, payload, retain))
                    n = len(self.received)
                    if self.drop_on_publish and n == self.drop_on_publish:
                        conn.close()                # angekommen, aber NICHT bestaetigt
                        return
                    conn.sendall(bytes([0x40, 2]) + pid)
                else:
                    conn.sendall(bytes([0x40, 4]) + pid + b"\x87\x00")
        except (OSError, IndexError, struct.error, UnicodeDecodeError):
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def stop(self):
        self.running = False
        self.srv.close()


def fresh():
    return (tempfile.mkdtemp(prefix="oaap-snd-m-"), tempfile.mkdtemp(prefix="oaap-snd-c-"))


BASE = 1_790_000_000 - 1_790_000_000 % 60
SECRET = "geheim-Wert-4711"


def fill(mdir, n, start=0):
    for i in range(n):
        m.queue_add(mdir, {"t": BASE + 60 * (start + i), "cpu": 10.0 + i,
                           "mem": 40.0, "disk": 50.0})


def cfg_for(cdir, port, **kw):
    kw.setdefault("node", "oaapx02")
    return s.configure(cdir, f"mqtt://127.0.0.1:{port}", "node-x02", SECRET,
                       allow_plain=True, **kw)


print("=== die Eingaben ===")
mdir, cdir = fresh()
c = s.configure(cdir, "mqtts://broker.example.org", "u", "p", node="Raspberry_Pi")
ok("der Knotenname wird zu einem Namen (klein, Bindestrich)", c["node"] == "raspberry-pi", c)
ok("die Wurzel ist standardmaessig oaap-node", c["root"] == "oaap-node", c)
ok("der Port folgt dem Schema (mqtts = 8883)", c["url"].endswith(":8883"), c)
ok("eine eigene Wurzel mit mehreren Ebenen geht",
   s.configure(cdir, "mqtts://b.example.org", "u", "p", node="n1", root="home/nodes")["root"] == "home/nodes")
for bad in ("/a", "a/", "a/#", "a/+/b", "$SYS", "Home", "a//b", "", "a" * 70, "a b"):
    try:
        s.validate_root(bad)
        ok(f"Wurzel {bad!r} wird abgelehnt", False)
    except s.ConfigError:
        ok(f"Wurzel {bad!r} wird abgelehnt", True)
for bad in ("x", "-a-", "a" * 50):
    try:
        s.normalise_node(bad)
        ok(f"Knotenname {bad!r} wird abgelehnt", False)
    except s.ConfigError:
        ok(f"Knotenname {bad!r} wird abgelehnt", True)
for url, plain, label in (("mqtt://broker.example.org", False, "Klartext ohne Erlaubnis"),
                          ("mqtt://8.8.8.8", True, "Klartext zu oeffentlicher Adresse"),
                          ("http://x", False, "falsches Schema"),
                          ("mqtts://", False, "kein Host")):
    try:
        s.configure(cdir, url, "u", "p", node="n1", allow_plain=plain)
        ok(f"{label} wird abgelehnt", False)
    except s.ConfigError:
        ok(f"{label} wird abgelehnt", True)
ok("Klartext zu einer privaten Adresse mit Erlaubnis geht",
   s.configure(cdir, "mqtt://192.168.1.5:1883", "u", "p", node="n1", allow_plain=True)["allow_plain"])
c = cfg_for(cdir, 1883)
ok("das Passwort steht in einer eigenen Datei, nicht in der Konfiguration",
   SECRET not in open(os.path.join(cdir, s.CONFIG_FILE)).read()
   and SECRET in open(os.path.join(cdir, s.SECRET_FILE)).read())
if os.name != "nt":
    ok("die Passwortdatei ist 0600",
       oct(os.stat(os.path.join(cdir, s.SECRET_FILE)).st_mode & 0o777) == "0o600")
try:
    s.configure(cdir, "mqtts://b.example.org", "u", "", node="n1")
    ok("ein leeres Passwort wird abgelehnt", False)
except s.ConfigError:
    ok("ein leeres Passwort wird abgelehnt", True)

print("\n=== nichts eingerichtet, nichts zu senden ===")
mdir, cdir = fresh()
fill(mdir, 2)
ok("ohne Einrichtung geschieht nichts", s.run(mdir, cdir, now=BASE)["status"] == "unconfigured"
   and len(m.queue_pending(mdir)) == 2)
srv = Broker({"node-x02": SECRET})
cfg_for(cdir, srv.port)
mdir2 = fresh()[0]
ok("ohne wartende Zeilen wird nicht einmal verbunden",
   s.run(mdir2, cdir, now=BASE)["status"] == "idle" and srv.connects == 0)

print("\n=== der Weg: senden, bestaetigen, loeschen ===")
mdir, cdir = fresh()
srv = Broker({"node-x02": SECRET})
cfg_for(cdir, srv.port)
fill(mdir, 5)
r = s.run(mdir, cdir, now=BASE + 1000)
ok("fuenf Zeilen gesendet", r == {"status": "ok", "sent": 5}, r)
ok("fuenfzehn Nachrichten (drei je Zeile) kamen an", len(srv.received) == 15, len(srv.received))
ok("das Protokoll stimmt (MQTT, Version 5, Flags)", not srv.protocol_errors, srv.protocol_errors)
ok("die Themen sind <Wurzel>/<Knoten>/metrics/<Reihe>",
   {t for _u, t, _p, _r in srv.received} ==
   {"oaap-node/oaapx02/metrics/" + x for x in ("cpu", "mem", "disk")})
ok("alle mit dem Retain-Zeichen", all(r for *_x, r in srv.received))
msg = json.loads(srv.received[0][2])
ok("die Nachricht ist die der Spezifikation (v, node, t, m, x)",
   set(msg) == {"v", "node", "t", "m", "x"} and msg["v"] == 1 and msg["node"] == "oaapx02"
   and msg["t"].endswith("Z") and msg["m"] == "cpu", msg)
ts = [json.loads(p)["t"] for _u, t, p, _r in srv.received if t.endswith("/cpu")]
ok("aelteste zuerst", ts == sorted(ts) and len(ts) == 5, ts)
ok("danach ist die Warteschlange leer (bestaetigt = geloescht)", not m.queue_pending(mdir)
   and m.queue_status(mdir)["acked"] == 5)
st = s.load_state(mdir)
ok("der Zustand merkt sich den Erfolg", st.get("ok_at") == BASE + 1000 and st.get("fails") == 0
   and st.get("sent_total") == 5, st)
ok("ein Lauf ohne Neues verbindet nicht erneut",
   s.run(mdir, cdir, now=BASE + 1100)["status"] == "idle" and srv.connects == 1)

print("\n=== eine eigene Wurzel ===")
mdir, cdir = fresh()
srv = Broker({"node-x02": SECRET})
cfg_for(cdir, srv.port, root="home/nodes")
fill(mdir, 1)
s.run(mdir, cdir, now=BASE)
ok("die Themen tragen die gewaehlte Wurzel",
   {t for _u, t, _p, _r in srv.received} ==
   {"home/nodes/oaapx02/metrics/" + x for x in ("cpu", "mem", "disk")},
   [t for _u, t, _p, _r in srv.received])

print("\n=== ein Absturz zwischen Ankunft und Bestaetigung (mindestens einmal) ===")
mdir, cdir = fresh()
srv = Broker({"node-x02": SECRET}, drop_on_publish=5)   # die 5. Nachricht kommt an, wird nicht bestaetigt
cfg_for(cdir, srv.port)
fill(mdir, 5)
r = s.run(mdir, cdir, now=BASE)
ok("der Lauf meldet den Fehler", r["status"] == "error" and r["kind"] == "network", r)
pend = m.queue_pending(mdir)
ok("Zeile 1 ist bestaetigt und weg, Zeile 2 bleibt (nicht alle ihre Nachrichten bestaetigt)",
   [e["q"] for e in pend] == [2, 3, 4, 5], [e["q"] for e in pend])
srv.drop_on_publish = None
r = s.run(mdir, cdir, now=BASE + 10_000)
ok("der naechste Lauf sendet den Rest", r["status"] == "ok" and not m.queue_pending(mdir), r)
t2 = BASE + 60
same = [1 for _u, t, p, _r in srv.received
        if t.endswith("/cpu") and json.loads(p)["t"] ==
        __import__("time").strftime("%Y-%m-%dT%H:%M:%SZ", __import__("time").gmtime(t2))]
ok("Zeile 2 kam doppelt an: mindestens einmal, nie verloren", len(same) == 2, len(same))

print("\n=== der Broker ist nicht da: Backoff, nichts geht verloren ===")
mdir, cdir = fresh()
dead = socket.socket()
dead.bind(("127.0.0.1", 0))
port = dead.getsockname()[1]
dead.close()
cfg_for(cdir, port)
fill(mdir, 3)
r = s.run(mdir, cdir, now=BASE)
st = s.load_state(mdir)
ok("Fehler gemeldet, erste Wartezeit 60 s", r["status"] == "error" and st["next_try"] == BASE + 60
   and st["fails"] == 1, (r, st))
ok("innerhalb der Wartezeit wird nicht verbunden", s.run(mdir, cdir, now=BASE + 30)["status"] == "backoff")
s.run(mdir, cdir, now=BASE + 61)
ok("zweiter Fehler: 120 s", s.load_state(mdir)["next_try"] == BASE + 61 + 120
   and s.load_state(mdir)["fails"] == 2, s.load_state(mdir))
for i in range(3, 12):
    s.run(mdir, cdir, now=s.load_state(mdir)["next_try"])
ok("die Wartezeit waechst bis 15 Minuten und nicht weiter",
   s.load_state(mdir)["next_try"] - 0 > 0 and
   s.backoff_seconds(30, "network") == 900 and s.backoff_seconds(3, "network") == 240)
ok("nichts ging verloren, nichts wurde geloescht",
   len(m.queue_pending(mdir)) == 3 and m.queue_status(mdir)["lost"] == 0)
srv = Broker({"node-x02": SECRET})
cfg_for(cdir, srv.port)
r = s.run(mdir, cdir, now=s.load_state(mdir)["next_try"])
ok("kommt der Broker wieder, wird alles nachgeliefert und der Zustand ist sauber",
   r["status"] == "ok" and r["sent"] == 3 and s.load_state(mdir)["fails"] == 0
   and s.load_state(mdir)["err"] == "", r)

print("\n=== ein falsches Passwort ===")
mdir, cdir = fresh()
srv = Broker({"node-x02": "ein-ganz-anderes"})
cfg_for(cdir, srv.port)
fill(mdir, 2)
r = s.run(mdir, cdir, now=BASE)
st = s.load_state(mdir)
ok("als Verweigerung erkannt, wartet gleich die lange Zeit",
   r["kind"] == "auth" and st["next_try"] == BASE + 900, (r, st))
ok("das Passwort steht in keinem Text (Ergebnis, Zustand)",
   SECRET not in json.dumps(r) and SECRET not in json.dumps(st))
ok("nichts wurde geloescht", len(m.queue_pending(mdir)) == 2)

print("\n=== ein verweigertes Veroeffentlichen darf NICHTS loeschen ===")
mdir, cdir = fresh()
srv = Broker({"node-x02": SECRET}, can_write=lambda u, t: t.startswith("oaap-node/anderer/"))
cfg_for(cdir, srv.port)
fill(mdir, 3)
r = s.run(mdir, cdir, now=BASE)
ok("als Verweigerung erkannt (nicht als Netzfehler)", r["status"] == "error" and r["kind"] == "auth", r)
ok("die Zeilen sind noch da (sonst waeren Daten geloescht, die niemand bekam)",
   len(m.queue_pending(mdir)) == 3 and m.queue_status(mdir)["acked"] == 0)
ok("die Meldung nennt das Thema, auf das nicht geschrieben werden darf",
   "oaap-node/oaapx02/metrics/" in r["error"], r["error"])

print("\n=== TLS: ein Fehler sendet nichts ===")
mdir, cdir = fresh()
srv = Broker({"node-x02": SECRET})
s.configure(cdir, f"mqtts://127.0.0.1:{srv.port}", "node-x02", SECRET, node="oaapx02")
fill(mdir, 2)
r = s.run(mdir, cdir, now=BASE)
ok("der Handschlag scheitert, es kommt nichts beim Broker an",
   r["status"] == "error" and "TLS" in r["error"] and not srv.received and srv.connects == 0, r)
ok("... und nichts wird geloescht", len(m.queue_pending(mdir)) == 2)
ok("das Passwort ist nie ueber die Leitung gegangen", srv.connects == 0)

print("\n=== die Zeit eines Laufs ist begrenzt, das ist kein Fehler ===")
mdir, cdir = fresh()
srv = Broker({"node-x02": SECRET})
cfg_for(cdir, srv.port)
fill(mdir, 6)


class Tired(s.Client):
    n = 0

    def publish(self, topic_name, payload):
        Tired.n += 1
        if Tired.n > 6:                      # nach zwei Zeilen (je drei Nachrichten)
            raise s.SenderError("the time budget of this run is used up")
        super().publish(topic_name, payload)


r = s.run(mdir, cdir, now=BASE, client_factory=Tired)
ok("zwei Zeilen gesendet, Rest bleibt, kein Backoff",
   r == {"status": "ok", "sent": 2} and len(m.queue_pending(mdir)) == 4
   and s.load_state(mdir)["next_try"] == 0, r)

print("\n=== ein Rueckstau wird nicht Zeile fuer Zeile umgeschrieben ===")
mdir, cdir = fresh()
srv = Broker({"node-x02": SECRET})
cfg_for(cdir, srv.port)
fill(mdir, 700)
calls = []
orig = m.queue_ack
m.queue_ack = lambda d, u: (calls.append(u), orig(d, u))[1]
r = s.run(mdir, cdir, now=BASE)
m.queue_ack = orig
ok("700 Zeilen alle gesendet", r["sent"] == 700 and not m.queue_pending(mdir), r)
ok("die Bestaetigung (= Umschreiben) geschah gebuendelt, nicht je Zeile", len(calls) <= 5, calls)

print("\n=== `oaap metrics sender` (derselbe Weg wie an der Maschine) ===")
import argparse                                                 # noqa: E402

os.environ["OAAP_DATA_DIR"] = tempfile.mkdtemp(prefix="oaap-snd-data-")
import appctl as a                                              # noqa: E402

mdir, cdir = fresh()
srv = Broker({"cli": "cli-pw"})
a.METRICS_DIR = mdir
a.METRICS_SENDER_DIR = cdir


def cli(action, stdin="", **kw):
    ns = argparse.Namespace(action="sender", sender_action=action, url=None, user=None,
                            node=None, root=None, ca=None, allow_plain=False,
                            window=None, yes=False)
    for k, v in kw.items():
        setattr(ns, k, v)
    buf, err = io.StringIO(), io.StringIO()
    code = 0
    old = sys.stdin
    sys.stdin = io.StringIO(stdin)
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            a.cmd_metrics(ns)
    except SystemExit as e:
        code = e.code or 0
    finally:
        sys.stdin = old
    return buf.getvalue() + err.getvalue(), code


out, code = cli("show")
ok("show ohne Einrichtung sagt es", code == 0 and "No sender configured" in out, out)
out, code = cli("set", stdin="cli-pw\n", url=f"mqtt://127.0.0.1:{srv.port}", user="cli",
                node="cli-node", allow_plain=True)
ok("set liest das Passwort von der Standardeingabe und gibt es nie aus",
   code == 0 and "cli-pw" not in out and "oaap-node/cli-node/metrics" in out, out)
out, code = cli("set", stdin="", url="mqtts://b.example.org", user="u")
ok("set ohne Passwort auf der Standardeingabe wird abgelehnt", code != 0, out)
out, code = cli("set", stdin="x\n", url="mqtts://b.example.org", user="u", root="a/#")
ok("set mit schlechter Wurzel wird abgelehnt und aendert nichts",
   code != 0 and s.load_config(cdir)["root"] == "oaap-node", out)
out, code = cli("show")
ok("show nennt Ziel, Knoten, Thema, Konto -- und dass ein Passwort gesetzt ist, nicht welches",
   code == 0 and "cli-node" in out and "secret: set" in out and "cli-pw" not in out, out)
out, code = cli("test")
ok("test verbindet und veroeffentlicht nichts",
   code == 0 and "nothing was published" in out and not srv.received, out)
fill(mdir, 2)
s.run(mdir, cdir, now=BASE)
out, code = cli("show")
ok("show nennt den Erfolg", "last success" in out and "sent so far: 2" in out, out)
out, code = cli("remove", yes=False)
ok("remove ohne --yes loescht nichts", s.load_config(cdir) is not None, out)
out, code = cli("remove", yes=True)
ok("remove --yes loescht Konfiguration, Passwort und Zustand; die Warteschlange bleibt",
   s.load_config(cdir) is None and s.load_secret(cdir) is None
   and not os.path.exists(os.path.join(mdir, s.STATE_FILE)), out)
fill(mdir, 1)
ok("... und sie fuellt sich weiter", len(m.queue_pending(mdir)) == 1)

print("\n=== der minuetliche Lauf ===")
cfg_for(cdir, srv.port)
a.metrics_sender.run = lambda *x, **k: (_ for _ in ()).throw(RuntimeError("kaputt"))
err = io.StringIO()
with contextlib.redirect_stderr(err):
    a.cmd_metrics(argparse.Namespace(action="sample", window=None, yes=False,
                                     sender_action=None))
ok("ein Fehler im Sender beendet den Lauf nicht (er teilt sich die Einheit)",
   "sender skipped" in err.getvalue(), err.getvalue())

print(f"\n{'OK' if not fails else 'FEHLER'}: {fails} Fehler")
sys.exit(1 if fails else 0)
