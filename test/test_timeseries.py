#!/usr/bin/env python3
"""Zeitreihen: der Pruefkatalog fuer JEDEN Treiber (RFC-0086 §6), hier gegen
den Standardtreiber (reines PostgreSQL, platform/services/twin/timeseries.py).

Braucht eine echte PostgreSQL 16: TS_DSN="host=... dbname=... user=...
password=...". Ohne TS_DSN meldet die Datei SKIP und endet mit 2 -- ein
uebersprungener Katalog ist kein gruener.

Geprueft wird, was am ehesten still schiefginge:

    ein doppelt gesendeter Punkt (QoS-1-Wiederholung) verdoppelt nichts;
    Minimum und Maximum ueberleben jede Verdichtung;
    eine Luecke bleibt eine Luecke;
    ein spaeter Punkt rechnet seinen Abschnitt neu -- auch eine Stufe hoeher;
    die Stufe passt zu Schritt und Aufbewahrung, nie mehr als 2000 Abschnitte;
    Partitionen gehen puenktlich, und nur die eigener Richtlinie;
    Text: gleicher Wert verlaengert, neuer oeffnet, Luecke nach drei Takten,
    65 Zeichen abgelehnt, ein seit Jahren gueltiger Zustand bleibt;
    ein Mandant sieht die Reihen eines anderen nicht;
    Export und Import geben dieselben Antworten;
    das Minutenkontingent gilt ueber Verbindungen hinweg, eine geloeschte
    Reihe steht im Protokoll.

Aufruf: TS_DSN=... python3 test/test_timeseries.py
"""
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services", "twin"))

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + ("" if cond or detail == "" else f"  -- {detail}"))


DSN = os.environ.get("TS_DSN", "")
if not DSN:
    print("SKIP  TS_DSN fehlt -- der Katalog braucht eine echte PostgreSQL.")
    sys.exit(2)

import psycopg2                                                # noqa: E402
import timeseries as ts                                        # noqa: E402

DAY = 86400
T0 = 1_800_000_000 // DAY * DAY          # a midnight in 2027, the simulated clock's start


def fresh(schema, **kw):
    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    conn.commit()
    d = ts.PostgresDriver(conn, schema, **kw)
    d.ensure_schema()
    return d


def raises(fn, text=""):
    try:
        fn()
    except ts.TimeseriesError as e:
        return text in str(e)
    return False


print("=== Reihen anlegen ===")
d = fresh("ts_conf_a", max_series=3)
s1, new1 = d.ensure_series("obj-1", "cpu", unit="%")
s1b, new1b = d.ensure_series("obj-1", "cpu", unit="%")
ok("anlegen oder finden: zweimal dieselbe Reihe", new1 and not new1b and s1["id"] == s1b["id"])
ok("Art und Richtlinie stehen fest",
   raises(lambda: d.ensure_series("obj-1", "cpu", kind="text"), "fixed at creation")
   and raises(lambda: d.ensure_series("obj-1", "cpu", policy="long"), "fixed at creation"))
ok("Etiketten gehoeren zur Identitaet",
   d.ensure_series("obj-1", "cpu", labels={"core": "0"})[0]["id"] != s1["id"])
ok("ohne Objekt keine Reihe", raises(lambda: d.ensure_series("", "cpu"), "twin object"))
ok("unbekannte Richtlinie abgelehnt", raises(lambda: d.ensure_series("o", "x", policy="forever"), "policy"))
ok("ungueltige Groesse abgelehnt", raises(lambda: d.ensure_series("o", "CPU Load"), "measure"))
ok("zu viele Etiketten abgelehnt",
   raises(lambda: d.ensure_series("o", "x", labels={f"k{i}": "v" for i in range(9)}), "labels"))
d.ensure_series("obj-2", "mem")
ok("Grenze je Mandant: die vierte Reihe wird mit Grund abgelehnt",
   raises(lambda: d.ensure_series("obj-3", "disk"), "limit"))
ok("... und laesst keine offene Transaktion zurueck (sie hielte Sperren)",
   d.conn.get_transaction_status() == psycopg2.extensions.TRANSACTION_STATUS_IDLE,
   d.conn.get_transaction_status())

print("\n=== Zahlen schreiben: idempotent, Grenzen ===")
d = fresh("ts_conf_a")
s, _ = d.ensure_series("node-a", "cpu")
now = T0 + DAY
pts = [(T0 + 60 * i, float(i % 50)) for i in range(1440)]
r1 = d.write(s["id"], pts, now)
r2 = d.write(s["id"], [(t, 999.0) for t, _ in pts[:100]], now)
ok("1440 Punkte geschrieben", r1["written"] == 1440 and r1["duplicate"] == 0, r1)
ok("dieselben Zeiten noch einmal: 100 Doppel, kein zweiter Wert", r2["written"] == 0 and r2["duplicate"] == 100, r2)
ok("... und der alte Wert gilt", d.range(s["id"], T0, T0 + 60, 60, now)["buckets"][0]["max"] == 0.0)
ok("die Doppel sind gezaehlt", d.counters().get("duplicate") == 100, d.counters())
r = d.write(s["id"], [(now - 8 * DAY, 1.0)], now)
ok("aelter als die Rohaufbewahrung (7 Tage): abgelehnt mit Grund",
   r["refused"] == 1 and "retention" in r["reasons"][0], r)
ok("mehr als eine Stunde in der Zukunft: abgelehnt", d.write(s["id"], [(now + 7200, 1.0)], now)["refused"] == 1)
ok("ein Wert, der keine Zahl ist, wird abgelehnt", raises(lambda: d.write(s["id"], [(now, "hoch")], now), "numbers"))
ok("NaN wird abgelehnt", raises(lambda: d.write(s["id"], [(now, float("nan"))], now), "finite"))
ok("ISO-Zeiten gehen, ohne Zone nicht",
   d.write(s["id"], [(ts.iso(now - 30), 3.0)], now)["written"] == 1
   and d.latest(s["id"])["t"] == ts.iso(now - 30)
   and raises(lambda: d.write(s["id"], [("2027-01-16T00:00:00", 1.0)], now), "zone"))
ok("mehr als 10000 Punkte in einem Aufruf: abgelehnt",
   raises(lambda: d.write(s["id"], [(now, 1.0)] * 10001, now), "10000"))

print("\n=== Verdichten: Min/Max ueberleben, Luecken bleiben ===")
d = fresh("ts_conf_a")
s, _ = d.ensure_series("node-a", "cpu")
pts = [(T0 + 60 * i, 10.0) for i in range(1440) if not (600 <= i < 630)]   # 30 min Luecke
pts = [(t, 99.0) if t == T0 + 60 * 300 else (t, 1.0) if t == T0 + 60 * 900 else (t, x) for t, x in pts]
d.write(s["id"], pts, T0 + DAY - 60)
d.job(T0 + 3 * DAY + 3 * 3600)     # ein Tagesabschnitt wartet zwei Tage (Gnadenfrist, §4.2)
five = d.range(s["id"], T0, T0 + DAY, 300, T0 + 2 * DAY)
ok("5-Minuten-Stufe gewaehlt", five["tier"] == "5m", five["tier"])
ok("eine Luecke von 30 Minuten: sechs Abschnitte fehlen, keiner ist erfunden",
   len(five["buckets"]) == 288 - 6, len(five["buckets"]))
ok("Spitze 99 und Tal 1 stehen in ihren 5-Minuten-Abschnitten",
   max(b["max"] for b in five["buckets"]) == 99.0 and min(b["min"] for b in five["buckets"]) == 1.0)
day = d.range(s["id"], T0, T0 + DAY, DAY, T0 + 2 * DAY)
b = day["buckets"][0]
ok("... und im Tagesabschnitt (raw -> 5m -> 1h -> 1d)", day["tier"] == "1d" and b["max"] == 99.0 and b["min"] == 1.0, day)
expected_mean = sum(x for _, x in pts) / len(pts)
ok("der Tagesmittelwert ist nach Anzahl gewichtet, nicht ein Mittel der Mittel",
   abs(b["mean"] - expected_mean) < 1e-9 and b["count"] == len(pts), (b, expected_mean))

print("\n=== Spaete Punkte ===")
late_t = T0 + 60 * 615 + 7          # mitten in der Luecke
r = d.write(s["id"], [(late_t, 500.0)], T0 + 3 * DAY + 3 * 3600)
ok("ein spaeter Punkt in verdichtetem Bereich wird angenommen und gezaehlt", r["written"] == 1 and r["late"] == 1, r)
d.roll_up(T0 + 3 * DAY + 3 * 3600 + 60)
five = d.range(s["id"], T0, T0 + DAY, 300, T0 + 2 * DAY)
ok("... sein 5-Minuten-Abschnitt ist neu berechnet", len(five["buckets"]) == 288 - 5
   and any(x["max"] == 500.0 for x in five["buckets"]))
d.roll_up(T0 + 3 * DAY + 3 * 3600 + 120)
d.roll_up(T0 + 3 * DAY + 3 * 3600 + 180)
day = d.range(s["id"], T0, T0 + DAY, DAY, T0 + 2 * DAY)
ok("... und die Stufen darueber auch (1h, 1d)", day["buckets"][0]["max"] == 500.0, day["buckets"])

print("\n=== Frische Punkte: die Stufe ist noch nicht verdichtet ===")
d = fresh("ts_conf_a")
sf, _ = d.ensure_series("node-a", "fresh")
now = T0 + 5 * DAY + 3600
d.write(sf["id"], [(now - 3600 + 60 * i, float(i % 7)) for i in range(60)], now)
before = d.range(sf["id"], now - 3600, now, 300, now)
ok("ohne Job: alle zwoelf 5-min-Abschnitte, aus den Rohpunkten",
   before["tier"] == "5m" and len(before["buckets"]) == 12
   and all(b["count"] == 5 for b in before["buckets"]), before)
d.job(now)
after = d.range(sf["id"], now - 3600, now, 300, now)
ok("nach dem Job dieselbe Antwort (der Schnitt an der Marke ist nahtlos)",
   [(b["t"], round(b["mean"], 9), b["min"], b["max"], b["count"]) for b in before["buckets"]]
   == [(b["t"], round(b["mean"], 9), b["min"], b["max"], b["count"]) for b in after["buckets"]],
   (before["buckets"][-3:], after["buckets"][-3:]))
odd = d.range(sf["id"], now - 3600, now, 420, now)
ok("ein Schritt, der nicht auf der Marke liegt: kein Punkt doppelt, keiner verloren",
   sum(b["count"] for b in odd["buckets"]) == 60, odd)

print("\n=== Stufenwahl ===")
now = T0 + 800 * DAY
ch = d.choose_tier
ok("Schritt 5 min, 20 Tage zurueck: 5m", ch("standard", now - 20 * DAY, now - 19 * DAY, 300, now)[0] == "5m")
t, _, note = ch("standard", now - 20 * DAY, now - 20 * DAY + 3600, 60, now)
ok("Schritt 1 min, 20 Tage zurueck: roh ist weg -> 5m, mit Hinweis", t == "5m" and "retention" in note, (t, note))
t, _, note = ch("standard", now - 500 * DAY, now - 499 * DAY, 3600, now)
ok("Schritt 1 h, 500 Tage zurueck: 1h ist weg -> 1d, mit Hinweis", t == "1d" and note, (t, note))
t, _, note = ch("standard", now - 6 * DAY, now, 60, now)
ok("6 Tage im Minutenschritt waeren 8640 Abschnitte -> groeber, mit Hinweis", t != "raw" and "2000" in note, (t, note))
t, _, _ = ch("short", now - 6 * DAY, now - 5 * DAY, 3600, now)
ok("'short' kennt kein 1h: Schritt 1 h -> 5m", t == "5m", t)
rg = d.range(s["id"], T0, T0 + 30 * DAY, 60, T0 + 2 * DAY)
ok("eine Antwort hat nie mehr als 2000 Abschnitte", len(rg["buckets"]) <= 2000 and (30 * DAY) / rg["step"] <= 2000, rg["step"])

print("\n=== Aufbewahrung: Partitionen fallen puenktlich, je Richtlinie ===")
d = fresh("ts_conf_a")
st, _ = d.ensure_series("node-a", "cpu", policy="standard")
lg, _ = d.ensure_series("node-a", "cpu_long", policy="long")
for day_i in range(12):
    base = T0 + day_i * DAY
    for ser in (st, lg):
        d.write(ser["id"], [(base + 3600 * h, float(h)) for h in range(24)], base + DAY - 1)
d.job(T0 + 12 * DAY + 60)


def parts(table):
    with d.conn.cursor() as cur:
        cur.execute("""SELECT count(*) FROM pg_inherits i JOIN pg_class p ON p.oid = i.inhparent
            JOIN pg_namespace n ON n.oid = p.relnamespace WHERE n.nspname = %s AND p.relname = %s""",
                    (d.schema, table))
        n = cur.fetchone()[0]
    d.conn.commit()
    return n


with d.conn.cursor() as cur:
    cur.execute(f'SELECT min(t) FROM "{d.schema}".raw_standard')
    oldest_std = cur.fetchone()[0].timestamp()
    cur.execute(f'SELECT min(t) FROM "{d.schema}".raw_long')
    oldest_long = cur.fetchone()[0].timestamp()
d.conn.commit()
ok("'standard' haelt 7 Tage roh: die Tage davor sind als Partition gefallen",
   oldest_std >= T0 + 12 * DAY + 60 - 7 * DAY - DAY and oldest_std > T0 + 3 * DAY, ts.iso(oldest_std))
ok("'long' (31 Tage roh) hat noch alles -- eigene Tabelle, eigene Partitionen", oldest_long == T0, ts.iso(oldest_long))
ok("das Fallen ist gezaehlt", d.counters().get("partitions_dropped", 0) >= 4, d.counters())
ok("die verdichteten Abschnitte der gefallenen Tage sind noch da",
   len(d.range(st["id"], T0, T0 + DAY, 3600, T0 + 12 * DAY)["buckets"]) == 24)
ok("ein naechster Lauf legt Partitionen fuer morgen an", parts("raw_standard") >= 8)

print("\n=== Text: Zustaende als Wechsel ===")
d = fresh("ts_conf_a")
tx, _ = d.ensure_series("node-a", "liveness", kind="text", expected_s=60)
day_pts = [(T0 + 60 * i, "up" if not (300 <= i < 320) else "silent") for i in range(1440)]
r = d.write(tx["id"], day_pts, T0 + DAY)
with d.conn.cursor() as cur:
    cur.execute(f'SELECT count(*) FROM "{d.schema}".text_states')
    rows = cur.fetchone()[0]
d.conn.commit()
ok("ein Tag im Minutentakt mit einem Ausfall: drei Zeilen, nicht 1440", rows == 3, rows)
ok("... gleicher Wert verlaengert, neuer Wert oeffnet", r["written"] == 3 and r["extended"] == 1437, r)
_, iv = d.intervals(tx["id"], T0, T0 + DAY, T0 + DAY)
ok("Abschnitte lesen: up, silent (20 min), up",
   [i["value"] for i in iv] == ["up", "silent", "up"] and iv[1]["to"] - iv[1]["from"] == 1200, iv)
r = d.write(tx["id"], [(T0 + 60 * 1439 + 200, "up")], T0 + DAY + 300)
_, iv = d.intervals(tx["id"], T0, T0 + DAY + 600, T0 + DAY + 300)
ok("Luecke nach drei Takten: gleicher Wert, trotzdem ein neuer Abschnitt, dazwischen nichts",
   r["written"] == 1 and len(iv) == 4 and iv[2]["to"] == T0 + 60 * 1439, iv[2:])
ok("65 Zeichen werden abgelehnt, nie gekuerzt", raises(lambda: d.write(tx["id"], [(T0 + DAY, "x" * 65)], T0 + DAY), "64"))
ok("64 Zeichen gehen", d.write(tx["id"], [(T0 + DAY + 400, "y" * 64)], T0 + DAY + 400)["written"] == 1)
ok("kein Zeilenumbruch", raises(lambda: d.write(tx["id"], [(T0 + DAY, "a\nb")], T0 + DAY), "line breaks"))
try:
    with d.conn.cursor() as cur:
        cur.execute(f'INSERT INTO "{d.schema}".text_states VALUES (%s, now(), %s, now())', (tx["id"], "z" * 65))
    d.conn.commit()
    ok("die Spalte selbst lehnt 65 Zeichen ab", False)
except psycopg2.Error:
    d.conn.rollback()
    ok("die Spalte selbst lehnt 65 Zeichen ab", True)
r = d.write(tx["id"], [(T0 + 60 * 100, "up")], T0 + DAY + 500)
ok("ein schon abgedeckter Zeitpunkt mit gleichem Wert ist ein Doppel", r["duplicate"] == 1, r)
r = d.write(tx["id"], [(T0 + 60 * 700, "degraded")], T0 + DAY + 500)
_, iv = d.intervals(tx["id"], T0, T0 + DAY, T0 + DAY + 500)
ok("ein spaeter anderer Wert teilt den Abschnitt, in den er faellt",
   r["late"] == 1 and [i["value"] for i in iv][:5] == ["up", "silent", "up", "degraded", "up"], [i["value"] for i in iv])
bk = d.range(tx["id"], T0, T0 + DAY, 3600, T0 + DAY + 500)["buckets"]
ok("je Stunde: Wert am Ende, Zahl der Wechsel, laengster Wert",
   bk[5]["changes"] == 2 and bk[5]["longest"] == "up" and bk[5]["at_end"] == "up"
   and bk[0]["changes"] == 1 and bk[3]["changes"] == 0, bk[5])

print("\n=== Text: Aufbewahrung nach 'zuletzt gesehen' ===")
d = fresh("ts_conf_a")
old, _ = d.ensure_series("node-a", "mode", kind="text", policy="short")
d.write(old["id"], [(T0, "alt")], T0)
d.write(old["id"], [(T0 + DAY, "lang")], T0 + DAY)
for k in range(2, 30):
    d.write(old["id"], [(T0 + k * DAY, "lang")], T0 + k * DAY)
d.expire(T0 + 30 * DAY)
_, iv = d.intervals(old["id"], T0, T0 + 30 * DAY, T0 + 30 * DAY)
ok("'short' haelt Text 7 Tage: der alte Zustand ist weg, der seit 29 Tagen gueltige bleibt",
   [i["value"] for i in iv] == ["lang"] and iv[0]["from"] == T0 + DAY, iv)

print("\n=== Mandantengrenze ===")
da = fresh("ts_conf_a")
db = fresh("ts_conf-b")
sa, _ = da.ensure_series("node-a", "cpu")
ok("Mandant B sieht keine Reihe von A", db.list() == [] and raises(lambda: db.get_series(sa["id"]), "no series"))
ok("ein ungueltiger Schemaname wird abgelehnt", raises(lambda: ts.PostgresDriver(da.conn, "twin_x"), "schema"))

print("\n=== Export und Import ===")
da = fresh("ts_conf_a")
n1, _ = da.ensure_series("node-a", "cpu")
t1, _ = da.ensure_series("node-a", "state", kind="text")
da.write(n1["id"], [(T0 + 60 * i, float(i)) for i in range(600)], T0 + DAY)
da.write(t1["id"], [(T0, "a"), (T0 + 600, "b")], T0 + DAY)
da.job(T0 + DAY)
buf = io.StringIO()
lines = da.export(buf)
db = fresh("ts_conf-b")
db.import_(io.StringIO(buf.getvalue()))
sb = {(x["object"], x["measure"]): x for x in db.list()}
ra = da.range(n1["id"], T0, T0 + DAY, 300, T0 + DAY)["buckets"]
rb = db.range(sb[("node-a", "cpu")]["id"], T0, T0 + DAY, 300, T0 + DAY)["buckets"]
ia = da.intervals(t1["id"], T0, T0 + DAY, T0 + DAY)[1]
ib = db.intervals(sb[("node-a", "state")]["id"], T0, T0 + DAY, T0 + DAY)[1]
ok("Rundreise: dieselben Abschnitte und dieselben Zustaende", lines > 600 and ra == rb and ia == ib, (len(ra), len(rb)))
ok("ein zweiter Import verdoppelt nichts",
   db.import_(io.StringIO(buf.getvalue())) and
   db.range(sb[("node-a", "cpu")]["id"], T0, T0 + DAY, 300, T0 + DAY)["buckets"] == rb)

print("\n=== Minutenkontingent (ueber Verbindungen) ===")
da = fresh("ts_conf_a")
d2 = ts.PostgresDriver(psycopg2.connect(DSN), "ts_conf_a")   # the second worker
ok("innerhalb des Kontingents", da.admit(15000, T0, limit=20000) is True)
ok("die zweite Verbindung zaehlt gegen dieselbe Minute",
   raises(lambda: d2.admit(6000, T0 + 30, limit=20000), "limit"))
ok("nach der Ablehnung ist keine Transaktion offen",
   d2.conn.get_transaction_status() == psycopg2.extensions.TRANSACTION_STATUS_IDLE)
ok("die abgelehnte Menge zaehlt nicht mit", d2.admit(5000, T0 + 59, limit=20000) is True)
ok("eine neue Minute beginnt bei null", da.admit(20000, T0 + 60, limit=20000) is True)
ok("ein einzelner Aufruf ueber der Grenze", raises(lambda: da.admit(20001, T0 + 120, limit=20000), "limit"))
ok("abgelehnte Punkte gezaehlt", da.counters().get("rate_limited") == 6000, da.counters())
da.expire(T0 + 7200)
with da.conn.cursor() as cur:
    cur.execute('SELECT count(*) FROM "ts_conf_a".quota')
    left = cur.fetchone()[0]
da.conn.commit()
ok("alte Minuten raeumt der Job weg", left == 0, left)
d2.conn.close()

print("\n=== Loeschen mit Protokoll ===")
da = fresh("ts_conf_a")
sa, _ = da.ensure_series("node-a", "cpu")
da.write(sa["id"], [(T0 + 60 * i, 1.0) for i in range(30)], T0 + 3600)
da.drop_series(sa["id"], by="instance:probe")
with da.conn.cursor() as cur:
    cur.execute('SELECT who, action, series_id, detail FROM "ts_conf_a".audit')
    rows = cur.fetchall()
da.conn.commit()
ok("die Reihe ist weg", raises(lambda: da.get_series(sa["id"]), "no series"))
ok("das Protokoll nennt wer, was und wie viele Punkte",
   len(rows) == 1 and rows[0][0] == "instance:probe" and rows[0][1] == "series.deleted"
   and rows[0][2] == sa["id"] and rows[0][3]["raw_points"] == 30
   and rows[0][3]["measure"] == "cpu", rows)

for sch in ("ts_conf_a", "ts_conf-b"):
    with da.conn.cursor() as cur:
        cur.execute(f'DROP SCHEMA IF EXISTS "{sch}" CASCADE')
    da.conn.commit()

print(f"\n{'OK' if not fails else 'FEHLER'}: {fails} Fehler")
sys.exit(1 if fails else 0)
