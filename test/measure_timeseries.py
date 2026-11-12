#!/usr/bin/env python3
"""Zeitreihen: ein simuliertes Jahr gegen den Standardtreiber (RFC-0086 §10
Stufe 1). Keine Pruefung mit PASS/FAIL, sondern eine Messung, deren Zahlen
ins RFC gehen: Laufzeit, Groesse, Partitionen, Textzeilen.

50 Zahlenreihen und eine Statusreihe, Minutentakt, `standard`, die Uhr laeuft
Tag fuer Tag; der Job laeuft einmal je simuliertem Tag (er holt nach, was
liegen blieb -- das ist der Fall nach einem Ausfall des Knotens).

Aufruf: TS_DSN=... python3 test/measure_timeseries.py [TAGE] [REIHEN]
"""
import os
import random
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "platform", "services", "twin"))
import psycopg2                                                # noqa: E402
import timeseries as ts                                        # noqa: E402

DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 365
NSER = int(sys.argv[2]) if len(sys.argv) > 2 else 50
DAY = 86400
T0 = 1_800_000_000 // DAY * DAY
SCHEMA = "ts_measure"

conn = psycopg2.connect(os.environ["TS_DSN"])
with conn.cursor() as cur:
    cur.execute(f'DROP SCHEMA IF EXISTS "{SCHEMA}" CASCADE')
conn.commit()
d = ts.PostgresDriver(conn, SCHEMA)
d.ensure_schema()
series = [d.ensure_series(f"node-{i // 10}", f"m{i % 10}", unit="%")[0] for i in range(NSER)]
state = d.ensure_series("node-0", "liveness", kind="text", expected_s=60)[0]
rnd = random.Random(86)
spike_day = DAYS // 2


def counts():
    with conn.cursor() as cur:
        out = {}
        for t in ("raw_standard", "a5m_standard", "a1h_standard", "a1d_standard", "text_states"):
            cur.execute(f'SELECT count(*) FROM "{SCHEMA}"."{t}"')
            out[t] = cur.fetchone()[0]
        cur.execute("""SELECT count(*) FROM pg_inherits i JOIN pg_class p ON p.oid = i.inhparent
            JOIN pg_namespace n ON n.oid = p.relnamespace WHERE n.nspname = %s""", (SCHEMA,))
        out["partitions"] = cur.fetchone()[0]
    conn.commit()
    return out


start = time.time()
t_write = t_job = 0.0
written = 0
size_at_7 = None
for day in range(DAYS):
    base = T0 + day * DAY
    now = base + DAY - 1
    a = time.time()
    for k, s in enumerate(series):
        pts = [(base + 60 * i, 50 + 40 * rnd.random()) for i in range(1440)]
        if day == spike_day and k == 0:
            pts[700] = (pts[700][0], 1000.0)          # one spike, must survive to the day tier
        r = d.write(s["id"], pts, now)
        written += r["written"]
    # a status: five changes a day, one point a minute
    marks = sorted(rnd.sample(range(1, 1440), 4))
    vals, cur_v, j = [], "up", 0
    for i in range(1440):
        if j < len(marks) and i == marks[j]:
            cur_v = "degraded" if cur_v == "up" else "up"
            j += 1
        vals.append((base + 60 * i, cur_v))
    d.write(state["id"], vals, now)
    b = time.time()
    d.job(now)
    t_write += b - a
    t_job += time.time() - b
    if day == 6:
        size_at_7 = (d.size(), written)
    if day % 30 == 29 or day == DAYS - 1:
        c = counts()
        print(f"day {day + 1:>3}: {written / 1e6:6.2f} M points written, size {d.size() / 1e6:7.1f} MB, "
              f"raw {c['raw_standard']}, 5m {c['a5m_standard']}, 1h {c['a1h_standard']}, "
              f"1d {c['a1d_standard']}, text rows {c['text_states']}, partitions {c['partitions']}, "
              f"write {t_write:.0f}s job {t_job:.0f}s", flush=True)

end = T0 + DAYS * DAY
print("\n--- result ---")
print(f"days {DAYS}, series {NSER} + 1 text, points written {written}")
print(f"wall time {time.time() - start:.0f}s (write {t_write:.0f}s, job {t_job:.0f}s), "
      f"{written / max(t_write, 1e-9):.0f} points/s written")
if size_at_7:
    print(f"after 7 days: {size_at_7[0] / 1e6:.1f} MB for {size_at_7[1]} points "
          f"= {size_at_7[0] / size_at_7[1] * 1e6 / 1e6:.0f} MB per million raw points (incl. indexes, tiers)")
print(f"final size {d.size() / 1e6:.1f} MB; counters {d.counters()}")
c = counts()
print(f"final rows {c}")
day_b = d.range(series[0]["id"], T0 + spike_day * DAY, T0 + (spike_day + 1) * DAY, DAY, end)
print(f"spike day from tier {day_b['tier']}: max {day_b['buckets'][0]['max'] if day_b['buckets'] else None}")
yr = d.range(series[0]["id"], T0, end, 7 * DAY, end)
print(f"a year in weekly buckets: tier {yr['tier']}, {len(yr['buckets'])} buckets, note {yr['note']!r}")
with conn.cursor() as cur:
    cur.execute(f'DROP SCHEMA "{SCHEMA}" CASCADE')
conn.commit()
