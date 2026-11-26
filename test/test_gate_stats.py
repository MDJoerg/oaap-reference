#!/usr/bin/env python3
"""Was an den Toren geschah (RFC-0135 Stufe 2b, D10 a) -- gezählt aus dem
Zugriffsprotokoll des Gateways.

Festgehalten wird:

- eine Zeile zählt nur, wenn das Gateway sie mit `oaap_gate` markiert hat
  UND gates.json das Tor kennt -- ein gefälschtes Feld zählt nichts;
- POST 2xx = angenommen, GET des Tors 2xx = Ping, 401 = abgelehnt
  (Anmeldung), 413 zu groß, 429 zu viele, 5xx Ziel, sonst Inhalt; Vorlage
  und Lesen nach ref zählen nicht;
- je Lieferung und Ablehnung ein Ereignis ohne Nutzlast; die Ereignisse
  passen zu den Plattformtypen des Busses;
- Tage älter als 30 und Tore, die es nicht mehr gibt, fallen heraus; der
  Ausgang wächst nicht ohne Grenze;
- „ungewohnt still“: erst ab drei Tagen mit Lieferungen;
- traffic.measure reicht die markierten Zeilen weiter, auch nach einer
  Lücke; metrics.take_sample ruft den Haken, und ein Fehler darin kostet
  die Minute nicht;
- der Zwilling liest Tor und Zugang mit denselben Regeln wie App-Assets.

Nur Standardbibliothek. Aufruf: python3 test/test_gate_stats.py
"""
import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SERVICES = os.path.join(HERE, "..", "platform", "services")
sys.path.insert(0, SERVICES)
sys.path.insert(0, os.path.join(SERVICES, "twin"))

import gate_stats as gs                                       # noqa: E402
import traffic                                                # noqa: E402
import metrics                                                # noqa: E402
import bus_model as bm                                        # noqa: E402
import assets_model as am                                     # noqa: E402
import assets_twin as at                                      # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:700]}")


NOW = 1795000000.0          # 2026-11-18T...
DAY = time.strftime("%Y-%m-%d", time.gmtime(NOW))
KNOWN = {"T1/sap-support", "T1/formular"}


def line(key, method, uri, status, ts=NOW, **extra):
    d = {"ts": ts, "status": status, "request": {"method": method, "uri": uri, "host": "x"}}
    if key is not None:
        d["oaap_gate"] = key
    d.update(extra)
    return json.dumps(d)


print("=== zählen ===")
lines = [
    line("T1/sap-support", "POST", "/gate/sap-support", 201),
    line("T1/sap-support", "POST", "/gate/sap-support?answer=text", 200),
    line("T1/sap-support", "GET", "/gate/sap-support", 200),
    line("T1/sap-support", "GET", "/gate/sap-support/template?form=fields", 200),
    line("T1/sap-support", "GET", "/gate/sap-support/ZSUP-1", 200),
    line("T1/sap-support", "GET", "/gate/sap-support", 401),
    line("T1/sap-support", "POST", "/gate/sap-support", 422),
    line("T1/sap-support", "POST", "/gate/sap-support", 413),
    line("T1/sap-support", "POST", "/gate/sap-support", 429),
    line("T1/sap-support", "POST", "/gate/sap-support", 502),
    line("T1/fremd", "POST", "/gate/fremd", 201),                   # nicht in gates.json
    line(None, "POST", "/gate/sap-support", 201),                   # nicht markiert
    line("../../etc", "POST", "/gate/x", 201),                      # Unsinn im Feld
    "kein json " + '"oaap_gate"',
]
st, ev = gs.fold(None, lines, KNOWN, NOW)
d = st["gates"]["T1/sap-support"]["days"][DAY]
ok("zwei angenommen (201 und das zweite Mal 200), ein Ping", d["delivered"] == 2 and d["ping"] == 1, d)
ok("Ablehnungen nach Grund", d["refused"] == {"anmeldung": 1, "inhalt": 1, "zu_gross": 1,
                                               "zu_viele": 1, "ziel": 1}, d)
ok("Vorlage und Lesen nach ref zählen nicht", sum(d["refused"].values()) + d["delivered"] + d["ping"] == 8)
ok("unbekanntes Tor, unmarkierte Zeile, Unsinn: nichts", set(st["gates"]) == {"T1/sap-support"}, st)
ok("je Lieferung und Ablehnung ein Ereignis (2 + 5)", len(ev) == 7, ev)
ok("... ohne Nutzlast und ohne Zugang", all(set(e["data"]) <= {"gate", "status", "reason"} for e in ev))
ok("... Mandant, Absender node, Betreff gate:<name>",
   all(e["tenant"] == "T1" and e["publisher"] == "node" and e["subject"] == "gate:sap-support"
       for e in ev))
for e in ev:
    status, why, acc = bm.check_platform_publish(dict(e, to=None))
    if status != 202:
        ok("ein Ereignis passt nicht zum Plattformtyp", False, (e, status, why))
        break
else:
    ok("jedes Ereignis passt zu seinem Plattformtyp des Busses", True)
ok("die zwei Typen sind tenant-Typen, freigebbar", all(
    bm.PLATFORM_TYPES[t]["audience"] == "tenant"
    for t in ("oaap.gate.delivery.accepted.v1", "oaap.gate.delivery.refused.v1")))
bad = bm.check_platform_publish({"publisher": "node", "type": "oaap.gate.delivery.refused.v1",
                                 "subject": "gate:x", "data": {"gate": "x", "status": 401,
                                                               "reason": "anmeldung", "body": "!"}})
ok("... und nehmen keine Nutzlast an", bad[0] == 422, bad)
g = st["gates"]["T1/sap-support"]
ok("zuletzt angenommen, Ping, abgelehnt mit Grund",
   g["last_delivery"] and g["last_ping"] and g["last_refusal_reason"] in gs.REASONS, g)

print("")
print("=== aufräumen ===")
old = time.strftime("%Y-%m-%d", time.gmtime(NOW - 40 * 86400))
st["gates"]["T1/sap-support"]["days"][old] = {"delivered": 9, "ping": 0, "refused": {}}
st["gates"]["T1/weg"] = {"days": {DAY: {"delivered": 1, "ping": 0, "refused": {}}}}
st2, ev2 = gs.fold(st, [], KNOWN | {"T1/weg"} - {"T1/weg"}, NOW)
ok("Tage älter als 30 fallen heraus", old not in st2["gates"]["T1/sap-support"]["days"])
ok("ein entferntes Tor fällt heraus", "T1/weg" not in st2["gates"] and not ev2)
q = gs.queue_events([{"at": NOW - 8 * 86400, "type": "alt"}] + ev, ev * 200, NOW)
ok("der Ausgang: höchstens 1000, nichts älter als 7 Tage",
   len(q) == gs.EVENTS_MAX and all(e.get("type") != "alt" for e in q), len(q))

print("")
print("=== Übersicht und Rhythmus ===")
s = gs.summary(st2["gates"]["T1/sap-support"], NOW)
ok("heute: 2 angenommen, 5 abgelehnt, 1 Ping",
   s["today"]["delivered"] == 2 and s["today"]["refused_n"] == 5 and s["today"]["ping"] == 1, s)
ok("ohne Zählung: Nullen, nie", gs.summary(None, NOW)["today"]["delivered"] == 0
   and gs.summary(None, NOW)["last_delivery"] == "")


def days_ago(*n):
    return {"days": {time.strftime("%Y-%m-%d", time.gmtime(NOW - k * 86400)):
                     {"delivered": 1, "ping": 0, "refused": {}} for k in n},
            "last_delivery": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - min(n) * 86400))}


ok("täglich benutzt, seit 4 Tagen still: ungewohnt still",
   "seit 4 Tagen" in gs.unusually_quiet(gs.summary(days_ago(4, 5, 6, 7), NOW), NOW))
ok("täglich benutzt, gestern zuletzt: nicht still",
   gs.unusually_quiet(gs.summary(days_ago(1, 2, 3), NOW), NOW) == "")
ok("wöchentlich benutzt, seit 8 Tagen: nicht still",
   gs.unusually_quiet(gs.summary(days_ago(8, 15, 22), NOW), NOW) == "")
ok("erst zwei Tage mit Lieferungen: kein Urteil",
   gs.unusually_quiet(gs.summary(days_ago(10, 11), NOW), NOW) == "")

print("")
print("=== traffic und metrics reichen die Zeilen weiter ===")
tmp = tempfile.mkdtemp(prefix="oaap-gate-stats-")
log = os.path.join(tmp, "access.log")
open(log, "w").close()
traffic.measure(tmp, int(NOW), proc=tmp, log_path=log)           # Position am Ende
with open(log, "a") as f:
    f.write(lines[0] + "\n" + line(None, "GET", "/", 200) + "\n")
keep = {}
traffic.measure(tmp, int(NOW) + 60, proc=tmp, log_path=log, keep=keep)
ok("traffic: nur die markierten Zeilen", len(keep.get("gate_lines") or []) == 1, keep)
with open(log, "a") as f:
    f.write(lines[2] + "\n")
keep = {}
traffic.measure(tmp, int(NOW) + 3600, proc=tmp, log_path=log, keep=keep)   # Lücke > 5 min
ok("... auch nach einer Lücke (eine Lieferung bleibt eine)", len(keep.get("gate_lines") or []) == 1, keep)

got = []
mdir = os.path.join(tmp, "m")
os.makedirs(mdir)
metrics.take_sample(mdir, tmp, now=int(NOW) + 3600, proc=tmp, sleep=lambda s: None,
                    log_path=log)                              # Position am Ende
with open(log, "a") as f:
    f.write(lines[6] + "\n")
metrics.take_sample(mdir, tmp, now=int(NOW) + 3660, proc=tmp, sleep=lambda s: None,
                    log_path=log, gates=lambda ls, now: got.append((list(ls), now)))
ok("metrics: der Haken bekommt die Zeilen dieser Minute", got and len(got[0][0]) == 1, got)


def boom(ls, now):
    raise RuntimeError("kaputt")


e = metrics.take_sample(mdir, tmp, now=int(NOW) + 3720, proc=tmp, sleep=lambda s: None,
                        log_path=log, gates=boom)
ok("... ein Fehler im Haken kostet die Minute nicht", isinstance(e, dict) and e.get("t"), e)

print("")
print("=== der Zwilling: Tor und Zugang ===")
ans = at.read_tenant_answer({"v": 1, "assets": [
    {"kind": "gate", "name": "sap-support", "state": "ok", "light": "green",
     "destination": "kunde-vorgaenge", "delivered": 3, "refused": 1,
     "last_delivery": "2026-11-26T09:00:00Z", "last_ping": "2026-11-26T08:00:00Z"},
    {"kind": "access", "name": "sap-support · SAP A4H 001", "expires": "2027-02-24T00:00:00Z",
     "gate": "sap-support"},
    {"kind": "access", "name": "verwaist", "gate": "gibtsnicht"}]})
gate_ = next(a for a in ans if a["kind"] == "gate")
acc_ = next(a for a in ans if a["name"].startswith("sap-support ·"))
ok("das Tor: Zustand, Ampel, Ziel, heutige Zahlen, zuletzt angenommen und Ping",
   gate_["values"] == {"name": "sap-support", "kind": "gate", "state": "ok", "light": "green",
                       "destination": "kunde-vorgaenge", "deliveredToday": 3, "refusedToday": 1,
                       "lastDelivery": "2026-11-26T09:00:00Z", "lastPing": "2026-11-26T08:00:00Z"},
   gate_)
ok("der Zugang öffnet das Tor und hat seinen Ablauf",
   acc_["rel"] == {"opens": [("gate", "sap-support")]} and acc_["values"]["expires"].startswith("2027"),
   acc_)
ok("ein Zugang zu einem Tor, das die Antwort nicht nennt: ohne Beziehung",
   next(a for a in ans if a["name"] == "verwaist")["rel"] == {})
dm = am.data_model()
keys = {a["key"] for a in dm["attribute_types"]}
grp = next(g for g in dm["group_types"] if g["key"] == "oaap.assetitem")
ok("das Datenmodell 0.2.0 kennt die neuen Merkmale, auch in der Gruppe",
   am.VERSION == "0.2.0" and {"assetsDeliveredToday", "assetsRefusedToday", "assetsLastDelivery",
                              "assetsLastPing"} <= keys <= set(keys)
   and "assetsLastDelivery" in grp["attributes"], grp)
ok("Tore bekommen eine Zeitreihe ihres Zustands", "gate" in am.SERIES_KINDS)

print("")
print("ALL PASS" if not fails else f"{fails} FAILURES")
sys.exit(1 if fails else 0)
