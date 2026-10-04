#!/usr/bin/env python3
"""Instanzen gleichzeitig befragen (fleet_view.probe_all).

Anlass: oaapx01, 04.10.2026 -- eine einzige ueberlastete App (ihre Pruefung
dauerte ~1,8 s) machte `/fleet/status` fuer den ganzen Knoten 1,9 s lang,
weil 22 Pruefungen nacheinander liefen. Geprueft wird, was das verhindert
und was dabei am ehesten still schiefgeht:

    - die Antwort dauert so lang wie die langsamste Pruefung, nicht wie die
      Summe
    - eine Pruefung, die nicht fertig wird, kostet nur IHRE Zeile (err) und
      haelt die Runde nach dem Budget nicht auf
    - eine Pruefung, die wirft, ist eine err-Zeile, kein Absturz der Seite
    - jede gefragte Instanz bekommt eine Zeile, in der gefragten Reihenfolge
    - beide Aufrufer (Gesundheitsseite, Flottendokument) benutzen es

Braucht kein Docker und kein Flask.

Aufruf: python3 test/test_probe_all.py
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services", "portal"))

import fleet_view as fv                                        # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:400]}")


def slow_ok(name, inst):
    time.sleep(inst.get("s", 0.3))
    return ("ok", "Gesund", "HTTP 200")


items = [(f"app{i:02d}", {"s": 0.3}) for i in range(20)]

print("=== gleichzeitig statt nacheinander ===")
t = time.time()
rows = fv.probe_all(items, slow_ok)
dt = time.time() - t
ok("20 Pruefungen zu je 0,3 s dauern ~1 s, nicht 6 s (nacheinander)", dt < 2.5, dt)
ok("jede Instanz hat ihre Zeile, in der gefragten Reihenfolge",
   list(rows) == [n for n, _ in items] and all(r[0] == "ok" for r in rows.values()),
   list(rows)[:3])

print("\n=== eine haengende Pruefung ===")
mixed = items[:5] + [("haengt", {"s": 5.0})] + items[5:8]
t = time.time()
rows = fv.probe_all(mixed, slow_ok, budget=0.8)
dt = time.time() - t
ok("die Runde endet nach dem Budget, nicht nach der haengenden Pruefung",
   dt < 1.6, dt)
ok("die haengende Instanz bekommt eine err-Zeile mit Grund",
   rows["haengt"][0] == "err" and "rechtzeitig" in rows["haengt"][1], rows["haengt"])
ok("alle anderen sind unveraendert ok",
   all(r[0] == "ok" for n, r in rows.items() if n != "haengt") and len(rows) == 9)

print("\n=== eine Pruefung, die wirft ===")


def boom(name, inst):
    if name == "kaputt":
        raise ValueError("kein HTTP")
    return ("ok", "Gesund", "HTTP 200")


rows = fv.probe_all([("a", {}), ("kaputt", {}), ("b", {})], boom)
ok("die werfende Instanz ist eine err-Zeile mit dem Fehlertyp",
   rows["kaputt"] == ("err", "Nicht erreichbar", "ValueError"), rows["kaputt"])
ok("die Nachbarn sind unberuehrt", rows["a"][0] == "ok" and rows["b"][0] == "ok")

print("\n=== Ecken ===")
ok("keine Instanzen: leere Antwort", fv.probe_all([], boom) == {})
ok("eine Instanz geht auch", fv.probe_all([("x", {})], boom)["x"][0] == "ok")

print("\n=== die Aufrufer ===")
src = open(os.path.join(HERE, "..", "platform", "services", "portal", "app.py"),
           encoding="utf-8").read()
ok("Gesundheitsseite und Flottendokument rufen probe_all",
   src.count("fleet_view.probe_all(insts, _instance_probe)") == 2)
ok("keine Schleife ruft _instance_probe mehr nacheinander",
   "= _instance_probe(name, inst)" not in src)

print("\nFAILS:", fails)
sys.exit(1 if fails else 0)
