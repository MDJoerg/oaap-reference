#!/usr/bin/env python3
"""Knoten-Verkehr: Zugriffsprotokoll, Netzkarte, Container (RFC-0051, Stufe 3).

Geprueft wird der ECHTE Weg (`take_sample`) gegen ein falsches /proc und
ein echtes Protokoll im Temp-Verzeichnis, das der Test Minute fuer Minute
beschreibt. Was am ehesten still schiefgeht:

    - ein WebSocket, der fuenf Minuten offen war, darf die mittlere Dauer
      nicht verfaelschen (Status 101)
    - das Protokoll wird nicht jede Minute von vorn gelesen, und eine
      Rotation (anderes oder kuerzeres File) verliert nichts still
    - der Host-Header gehoert dem Absender: begrenzt, escaped, nie vertraut
    - eine Quelle, die nicht lesbar ist, nimmt der Minute nur ihre eigene
      Reihe, nie die CPU-Zeile
    - ein Zaehler, der rueckwaerts springt (Neustart), ist keine Rate

Braucht kein Docker, keinen Knoten und kein Flask.

Aufruf: python3 test/test_traffic.py
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services"))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services", "portal"))

import metrics as m                                            # noqa: E402
import traffic as tr                                           # noqa: E402
import metrics_view as mv                                      # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:400]}")


BASE = 1_790_000_000 - 1_790_000_000 % 7200
PROC = tempfile.mkdtemp(prefix="oaap-traffic-proc-")
STORE = tempfile.mkdtemp(prefix="oaap-traffic-store-")
DATA = tempfile.mkdtemp(prefix="oaap-traffic-data-")
LOGDIR = tempfile.mkdtemp(prefix="oaap-traffic-log-")
LOG = os.path.join(LOGDIR, "external-access.log")
os.makedirs(os.path.join(PROC, "net"))


def line(host="a.example", status=200, size=1000, dur=0.1):
    return json.dumps({"ts": 1.0, "status": status, "size": size,
                       "duration": dur, "request": {"host": host}}) + "\n"


def write(text, mode="a"):
    with open(LOG, mode, encoding="utf-8") as f:
        f.write(text)


def proc(total, idle, rx=0, tx=0, tcp=10, extra_nic=""):
    with open(os.path.join(PROC, "stat"), "w") as f:
        f.write(f"cpu  {total - idle} 0 0 {idle} 0 0 0 0 0 0\n")
    with open(os.path.join(PROC, "meminfo"), "w") as f:
        f.write("MemTotal: 1000 kB\nMemAvailable: 500 kB\n")
    with open(os.path.join(PROC, "net", "dev"), "w") as f:
        f.write("Inter-|   Receive\n face |bytes\n"
                f"    lo: 999999 1 0 0 0 0 0 0 999999 1 0 0 0 0 0 0\n"
                f"  ens6: {rx} 5 0 0 0 0 0 0 {tx} 5 0 0 0 0 0 0\n"
                f"br-abc: 777777 1 0 0 0 0 0 0 777777 1 0 0 0 0 0 0\n"
                f"vethab: 555555 1 0 0 0 0 0 0 555555 1 0 0 0 0 0 0\n"
                f"docker0: 444444 1 0 0 0 0 0 0 444444 1 0 0 0 0 0 0\n"
                f"  wg0: 333333 1 0 0 0 0 0 0 333333 1 0 0 0 0 0 0\n"
                + extra_nic)
    with open(os.path.join(PROC, "net", "sockstat"), "w") as f:
        f.write(f"sockets: used 5\nTCP: inuse {tcp} orphan 0 tw 0\n")


print("=== die Quellen ===")
proc(1000, 500, rx=5000, tx=9000, tcp=42)
ok("Netzkarte: nur echte Karten (nicht lo, br-, veth, docker, wg)",
   tr.read_nic_bytes(PROC) == (5000, 9000), tr.read_nic_bytes(PROC))
ok("Netzkarte: ohne /proc/net/dev kein Wert",
   tr.read_nic_bytes("/nichts") is None)
ok("TCP: offene Verbindungen aus sockstat", tr.read_tcp_inuse(PROC) == 42)
ok("TCP: ohne Datei kein Wert", tr.read_tcp_inuse("/nichts") is None)
st = tr.parse_docker_stats("oaap-app-a\t103.39%\t437.6MiB / 31.3GiB\n"
                           "oaap-b\t0.31%\t1.5GiB / 31GiB\nkaputt\n"
                           "x\tNaN%\t1MiB / 2MiB\n")
ok("Docker: zwei Zeilen gelesen, kaputte uebersprungen",
   [(n, c) for n, c, _ in st] == [("oaap-app-a", 103.39), ("oaap-b", 0.31)], st)
ok("Docker: Speicher in MB (MiB und GiB)",
   st[0][2] == 437.6 and st[1][2] == 1536.0, st)

print("\n=== die Zaehlung ===")
text = (line("a.example", 200, 1000, 0.2) + line("a.example", 200, 3000, 0.4)
        + line("a.example", 101, 500, 300.0)           # ein WebSocket
        + line("b.example", 502, 100, 8.0)
        + "kein json\n" + "[1,2]\n" + '{"status":"x"}\n\n'
        + line("a.example", 200, -5, -1))                # unsinnige Werte
h = tr.tally(text.splitlines())
a = h["a.example"]
ok("vier Anfragen an a, davon eine Dauerverbindung", a[0] == 4 and a[1] == 1, a)
ok("die Dauerverbindung steht NICHT in der Dauer (0,2 + 0,4 + 0)",
   abs(a[3] - 0.6) < 1e-9 and a[4] == 0.4, a)
ok("sie steht in den Bytes (1000+3000+500 und 0 fuer die unsinnige)",
   a[2] == 4500, a)
ok("5xx wird gezaehlt, und die 502 mit ihren 8 s zaehlt in die Dauer",
   h["b.example"][5] == 1 and h["b.example"][3] == 8.0, h["b.example"])
ok("Muell im Protokoll ist keine Anfrage",
   sum(r[0] for r in h.values()) == 5, h)
many = "".join(line(f"h{i}.example") for i in range(200))
hm = tr.tally(many.splitlines())
ok("hoechstens MAX_HOSTS verschiedene Hosts, der Rest sammelt sich",
   len(hm) <= tr.MAX_HOSTS + 1 and tr.OTHER in hm
   and sum(r[0] for r in hm.values()) == 200, len(hm))
long_host = "x" * 500 + ".example"
ok("ein langer Host wird gekuerzt",
   list(tr.tally([line(long_host).strip()]).keys())[0] == "x" * tr.HOST_LEN)

print("\n=== das Protokoll wird nicht von vorn gelesen ===")
write(line() * 3, "w")
s = {}
lines, s = tr._read_new(LOG, s)
ok("erste Messung: vom ENDE, Vergangenes wird nicht gelesen", lines == [], lines)
write(line("n.example") * 2)
lines, s = tr._read_new(LOG, s)
ok("danach nur das Neue", len(lines) == 2, lines)
lines, s2 = tr._read_new(LOG, s)
ok("ohne Neues nichts, und nichts doppelt", lines == [] and s2 == s)
write('{"status":200,"size":1,"duration":0.1,"reque')
lines, s = tr._read_new(LOG, s)
ok("eine halbe Zeile am Ende wird nicht verbraucht", lines == [], lines)
write('st":{"host":"half.example"}}\n')
lines, s = tr._read_new(LOG, s)
ok("... sondern mit dem Rest zusammen gelesen",
   len(lines) == 1 and "half.example" in lines[0], lines)
write(line("kurz.example"), "w")             # kuerzer als die Position
lines, s = tr._read_new(LOG, s)
ok("eine kuerzere Datei (Rotation) wird von vorn gelesen",
   len(lines) == 1 and "kurz.example" in lines[0], lines)
ok("ohne Datei: keine Messung, Position bleibt",
   tr._read_new(os.path.join(LOGDIR, "gibt-es-nicht"), s) == (None, s))
small = tr.READ_LIMIT
tr.READ_LIMIT = 300
write(line("alt.example") * 20)
lines, s = tr._read_new(LOG, s)
tr.READ_LIMIT = small
ok("zu viel Rueckstand: nur die jungste Menge, der Rest wird uebersprungen",
   0 < len(lines) < 20, len(lines))

print("\n=== ein Lauf durch den echten Weg ===")
write("", "w")
total, idle = 10_000, 9_000
proc(total, idle, rx=1_000_000, tx=2_000_000, tcp=50)
e0 = m.take_sample(STORE, DATA, now=BASE, proc=PROC,
                   sleep=lambda s: proc(total + 100, idle + 50, rx=1_000_000,
                                        tx=2_000_000, tcp=50),
                   log_path=LOG, containers=lambda: [("c1", 10.0, 5.0)])
total, idle = total + 100, idle + 50          # wo der Sampler jetzt steht
ok("erste Minute: CPU da, aber keine Rate (es gibt keinen Vorwert)",
   "cpu" in e0 and "req" not in e0 and "rx" not in e0 and "lat" not in e0, e0)
ok("... TCP und der staerkste Container schon",
   e0.get("conn") == 50 and e0.get("ctop") == 10.0, e0)
write(line("a.example", 200, 2000, 0.2) * 10 + line("a.example", 200, 100, 0.8) * 10
      + line("a.example", 101, 50, 280.0))
proc(total + 6000, idle + 4800, rx=1_000_000 + 600_000, tx=2_000_000 + 3_000_000,
     tcp=60)
e1 = m.take_sample(STORE, DATA, now=BASE + 60, proc=PROC,
                   sleep=lambda s: None, log_path=LOG,
                   containers=lambda: [("small", 3.0, 1.0), ("hot", 104.5, 400.0)])
ok("21 Anfragen in 60 s = 21 je Minute", e1.get("req") == 21.0, e1)
ok("mittlere Dauer ohne die Dauerverbindung: (10*0,2 + 10*0,8)/20 = 500 ms",
   e1.get("lat") == 500.0, e1)
ok("Netzkarte: 600 000 Byte in 60 s rein = 10 000 B/s, raus 50 000 B/s",
   e1.get("rx") == 10000.0 and e1.get("tx") == 50000.0, e1)
ok("staerkster Container 104,5 Prozent eines Kerns (mehr als 100 ist moeglich)",
   e1.get("ctop") == 104.5, e1)
ok("CPU steht daneben unverandert (20 Prozent)", e1.get("cpu") == 20.0, e1)
snap = tr.snapshot(STORE)
ok("Schnappschuss: Host a mit 21 Anfragen, 1 Dauerverbindung",
   snap["hosts"]["a.example"][0] == 21 and snap["hosts"]["a.example"][1] == 1,
   snap["hosts"])
ok("Schnappschuss: Container, der staerkste zuerst",
   [c[0] for c in snap["containers"]] == ["hot", "small"], snap["containers"])
ok("kein Protokoll-Inhalt in der Reihe: nur Zahlen",
   all(isinstance(v, (int, float)) for k, v in e1.items() if k != "t"), e1)
ok("die Warteschlange (MQTT) traegt die neuen Reihen, eine Zeile je Reihe",
   {l["m"] for l in m.sample_lines("n", e1)}
   >= {"cpu", "req", "lat", "tx", "rx", "conn", "ctop"})

print("\n=== was nicht gelesen werden kann ===")
write(line("a.example") * 3)
proc(total + 12000, idle + 9600, rx=500, tx=500, tcp=61)     # Zaehler zurueck
e2 = m.take_sample(STORE, DATA, now=BASE + 120, proc=PROC, sleep=lambda s: None,
                   log_path=os.path.join(LOGDIR, "weg"),
                   containers=lambda: (_ for _ in ()).throw(OSError("docker")))
ok("Zaehler rueckwaerts (Neustart): keine Rate", "rx" not in e2 and "tx" not in e2, e2)
ok("Protokoll fehlt: keine Anfragen-Reihen, CPU bleibt",
   "req" not in e2 and "lat" not in e2 and e2.get("cpu") == 20.0, e2)
ok("Docker wirft: kein Container-Wert, CPU bleibt",
   "ctop" not in e2 and e2.get("cpu") == 20.0, e2)
proc(total + 18000, idle + 14400, rx=900, tx=900, tcp=61)
e3 = m.take_sample(STORE, DATA, now=BASE + 180 + 400, proc=PROC,
                   sleep=lambda s: None, log_path=LOG)
ok("nach einer Luecke von ueber 5 Minuten keine Rate (kein Minutenwert)",
   "req" not in e3 and "rx" not in e3, e3)
write(line("a.example") * 6)
proc(total + 24000, idle + 19200, rx=1500, tx=1500, tcp=61)
e4 = m.take_sample(STORE, DATA, now=BASE + 180 + 460, proc=PROC,
                   sleep=lambda s: None, log_path=LOG)
ok("... und die Luecke hat die Zeilen nicht in die naechste Minute geschoben",
   e4.get("req") == 6.0, e4)

print("\n=== der Host gehoert dem Absender ===")
bait = "<script>alert(1)</script>.evil"
write(line(bait, 200, 10, 0.1) * 3 + line('"><img src=x>', 500, 10, 0.1))
proc(total + 30000, idle + 24000, rx=2000, tx=2000, tcp=61)
m.take_sample(STORE, DATA, now=BASE + 180 + 520, proc=PROC, sleep=lambda s: None,
              log_path=LOG, containers=lambda: [("<b>x</b>", 5.0, 1.0)])
page = mv.block(STORE, "4h", now=BASE + 180 + 520)
ok("der Block zeigt die Tabellen", "Letzte Stunde je Host" in page
   and "Container jetzt" in page, page[-300:])
ok("ein Host mit Markup steht als Text da, nie als Markup",
   "<script>alert" not in page and "&lt;script&gt;alert(1)&lt;/script&gt;" in page
   and '"><img' not in page)
ok("ein Containername mit Markup ebenso", "<b>x</b>" not in page
   and "&lt;b&gt;x&lt;/b&gt;" in page)
ok("die Verkehrsdiagramme sind da (Anfragen, Dauer, Netz)",
   "Anfragen je Minute" in page and "Dauer je Anfrage" in page
   and "Netz raus" in page, page[:200])

print("\n=== die Skala ===")
ok("nice_top: 104,5 -> 200, 0,3 -> 0,5, 5 -> 5, nichts -> 1",
   (mv.nice_top(104.5), mv.nice_top(0.3), mv.nice_top(5), mv.nice_top(0),
    mv.nice_top(None)) == (200, 0.5, 5, 1, 1),
   (mv.nice_top(104.5), mv.nice_top(0.3), mv.nice_top(5)))
ok("fmt: Byte/s lesbar", mv.fmt("tx", 50000) == "50,0 KB/s"
   and mv.fmt("rx", 1_500_000) == "1,5 MB/s" and mv.fmt("rx", 12) == "12 B/s")
ok("fmt: Dauer in ms oder s", mv.fmt("lat", 500) == "500 ms"
   and mv.fmt("lat", 2500) == "2,5 s")

print("\n=== ein Knoten ohne Verkehrsdaten (vorher aktualisiert) ===")
old = tempfile.mkdtemp(prefix="oaap-traffic-old-")
m._append(old, "raw", [{"t": BASE + 60 * i, "cpu": 5.0, "mem": 10.0, "disk": 20.0}
                       for i in range(5)])
old_page = mv.block(old, "4h", now=BASE + 400)
ok("ohne Daten keine Ueberschrift und keine leeren Tabellen",
   "Verkehr" not in old_page and "Letzte Stunde" not in old_page
   and "<table" not in old_page, old_page[-200:])
ok("CPU, Speicher und Platte sind unverandert da",
   "CPU" in old_page and "Arbeitsspeicher" in old_page)

print("\n=== Staffelung ===")
w = m.window(STORE, "4h", now=BASE + 180 + 520)
ok("die neuen Reihen laufen durch die Fenster",
   len(w["series"]["req"]) >= 1 and len(w["series"]["rx"]) >= 1)
m.rollup(STORE, BASE + 3600)
five = m._read(STORE, "5m")
ok("... und durch die Staffelung (Mittel, Minimum, Maximum je Reihe)",
   five and isinstance(five[0].get("req"), list) and len(five[0]["req"]) == 4,
   five[:1])

print("\nFAILS:", fails)
sys.exit(1 if fails else 0)
