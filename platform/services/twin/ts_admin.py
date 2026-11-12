"""oaap.data.timeseries 0.1 -- the host's door into the driver (RFC-0086 §5).

Run INSIDE the twin container (`docker exec oaap-twin-1 python ts_admin.py
...`), because only this image has the driver and psycopg2, and only this
container has the secrets file the schema roles live in. appctl calls it;
nobody else does.

    ensure <schema>   tables and partitions of one ts_<tenant-id> schema
    job               the minutely job for every ts_* schema in the secrets
    show [<schema>]   one JSON line per schema: series, size, counters

`job` never exits non-zero: the unit it runs in also refreshes the
portal's container view, and a store that does not answer this minute
must not cost that (the rule of RFC-0051 and RFC-0052). What failed is
said on stderr, one line per schema, and lands in the journal.
"""
import datetime
import json
import os
import sys
import time

import psycopg2

import timeseries as ts

SECRETS_FILE = "/platform-apps/twin-secrets.json"
STORE_HOST = os.environ.get("STORE_HOST", "store")
STORE_PORT = int(os.environ.get("STORE_PORT", "5432"))


def _secrets():
    try:
        with open(SECRETS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def schemas():
    return sorted(k for k in _secrets() if k.startswith("ts_") and ts.SCHEMA_RE.match(k))


def driver(schema):
    creds = _secrets().get(schema)
    if creds is None:
        raise SystemExit(f"{schema}: no credential in the secrets file -- "
                         "'oaap data timeseries enable' writes it")
    conn = psycopg2.connect(host=STORE_HOST, port=STORE_PORT, dbname="postgres",
                            user=creds["role"], password=creds["password"],
                            connect_timeout=5)
    return ts.PostgresDriver(conn, schema)


def cmd_ensure(schema):
    d = driver(schema)
    try:
        d.ensure_schema()
        d.ensure_partitions(time.time())
    finally:
        d.conn.close()
    print(json.dumps({"schema": schema, "ensured": True}))


def cmd_job():
    now = time.time()
    for schema in schemas():
        t0 = time.monotonic()
        try:
            d = driver(schema)
            try:
                # Idempotent and cheap; it is what brings a schema made by an
                # older build up to this one's tables, without a migration step.
                d.ensure_schema()
                r = d.job(now)
            finally:
                d.conn.close()
            line = {"schema": schema, "ms": int((time.monotonic() - t0) * 1000),
                    "rolled_up": r["rolled_up"],
                    "partitions_dropped": len(r["partitions_dropped"]),
                    "text_states_expired": r["text_states_expired"]}
            if line["partitions_dropped"] or line["text_states_expired"]:
                print(json.dumps(line))
        except Exception as exc:  # noqa: BLE001 -- see the module docstring
            print(f"timeseries: job for {schema} skipped "
                  f"({type(exc).__name__}: {exc})", file=sys.stderr)


def cmd_show(only=None):
    for schema in ([only] if only else schemas()):
        try:
            d = driver(schema)
        except (SystemExit, psycopg2.Error) as exc:
            print(json.dumps({"schema": schema, "error": str(exc)}))
            continue
        try:
            series = d.list()
            week = (datetime.datetime.now(datetime.timezone.utc).date()
                    - datetime.timedelta(days=6))
            out = {"schema": schema, "series": len(series),
                   "numbers": sum(s["kind"] == "number" for s in series),
                   "texts": sum(s["kind"] == "text" for s in series),
                   "objects": len({s["object"] for s in series}),
                   "bytes": d.size(), "counters_7d": d.counters(week)}
            with d.conn.cursor() as cur:
                cur.execute(f'SELECT upto FROM "{schema}".marks WHERE tier = %s '
                            "ORDER BY upto DESC LIMIT 1", ("5m",))
                row = cur.fetchone()
                out["rolled_up_to"] = ts.iso(row[0].timestamp()) if row else None
            d.conn.commit()
        except Exception as exc:  # noqa: BLE001
            out = {"schema": schema, "error": f"{type(exc).__name__}: {exc}"}
        finally:
            d.conn.close()
        print(json.dumps(out))


def main(argv):
    if not argv:
        raise SystemExit(__doc__)
    if argv[0] == "ensure" and len(argv) == 2:
        if not ts.SCHEMA_RE.match(argv[1]):
            raise SystemExit(f"not a time-series schema name: {argv[1]!r}")
        return cmd_ensure(argv[1])
    if argv[0] == "job":
        return cmd_job()
    if argv[0] == "show":
        return cmd_show(argv[1] if len(argv) > 1 else None)
    raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
