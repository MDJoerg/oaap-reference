"""oaap.data.timeseries 0.1 -- the default driver: plain PostgreSQL (RFC-0086).

Values over time, attached to a twin object: numbers kept in tiers that
shrink with age, and short texts (at most 64 characters) kept as their
changes. Nothing here reads a request or a file: a psycopg2 connection, a
schema name and -- everywhere it matters -- `now` go in. `now` is a
parameter so that a year can be simulated in minutes (RFC-0086 §10 stage
1); the service passes the wall clock.

Layout of one tenant's schema `ts_<tenant-id>` (RFC-0086 §5, with the two
departures recorded in the spec):

    series                  one row per series
    raw_<policy>            points, partitioned by DAY
    a5m_/a1h_/a1d_<policy>  rolled-up buckets, partitioned by MONTH
    text_states             one row per state interval, NOT partitioned
    marks                   per (policy, tier): rolled up to here
    dirty                   buckets a late point invalidated
    counters                per day and name: duplicates, refused, dropped

Tables are per POLICY because retention is dropping partitions: a day of
`standard` (7 days raw) and a day of `long` (31 days raw) cannot share a
partition. Text states are not partitioned because an interval belongs to
the time it was LAST SEEN, not to the time it began -- a state that has held
for two years must not go with the partition of its first day. They are
few (RFC-0086 §4.4), and `DELETE` by `last_seen` is cheap at that size.

Licence: PostgreSQL only, no extension.
"""
import datetime as _dt
import json
import re
import unicodedata

import psycopg2
import psycopg2.extras

VERSION = "0.1"
DAY = 86400
TIERS = {"5m": 300, "1h": 3600, "1d": DAY}
TIER_ORDER = ["5m", "1h", "1d"]
RAW_RESOLUTION = 60          # nominal: the sampler's minute (RFC-0051)

# RFC-0086 §4.1 -- named, fixed; a new one is a change of the spec.
POLICIES = {
    "standard": {"raw": 7 * DAY, "5m": 31 * DAY, "1h": 400 * DAY, "1d": 3650 * DAY},
    "short":    {"raw": 2 * DAY, "5m": 7 * DAY},
    "long":     {"raw": 31 * DAY, "5m": 400 * DAY, "1h": 3650 * DAY, "1d": 3650 * DAY},
}
KINDS = ("number", "text")
TEXT_MAX = 64
GAP_FACTOR = 3               # a text gap after three expected intervals
MAX_BATCH = 10000
MAX_BUCKETS = 2000
MAX_LABELS = 8
DEFAULT_MAX_SERIES = 10000
FUTURE_SLACK = 3600          # a point more than an hour ahead is refused
MEASURE_RE = re.compile(r"^[a-z][a-z0-9_.]{0,62}$")
LABEL_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
SCHEMA_RE = re.compile(r"^ts_[a-z0-9_]{1,59}$")


class TimeseriesError(ValueError):
    """A request this driver refuses -- the message says why, for a person."""


class NotFound(TimeseriesError):
    pass


# --- helpers -------------------------------------------------------------

def _ts(epoch):
    return _dt.datetime.fromtimestamp(int(epoch), _dt.timezone.utc)


def _epoch(t):
    if isinstance(t, (int, float)):
        return float(t)
    return t.timestamp()


def parse_time(v):
    """Seconds since the epoch from a number or an ISO-8601 UTC string."""
    if isinstance(v, bool):
        raise TimeseriesError("a time is a number of seconds or an ISO-8601 string")
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            d = _dt.datetime.fromisoformat(s)
        except ValueError:
            raise TimeseriesError(f"not a time: {v!r}") from None
        if d.tzinfo is None:
            raise TimeseriesError(f"a time needs its zone (UTC 'Z'): {v!r}")
        return d.timestamp()
    raise TimeseriesError(f"not a time: {v!r}")


def iso(epoch):
    return _ts(epoch).strftime("%Y-%m-%dT%H:%M:%SZ") if epoch is not None else None


def _floor(epoch, size):
    return int(epoch) // size * size


def check_text(value):
    """The rule of RFC-0086 §2.1, in one place: at most 64 code points, no
    control characters, no line breaks; refused with a reason, never cut."""
    if not isinstance(value, str):
        raise TimeseriesError("a text series takes text")
    if value == "":
        raise TimeseriesError("an empty text is not a state")
    if len(value) > TEXT_MAX:
        raise TimeseriesError(f"at most {TEXT_MAX} characters, this one has "
                              f"{len(value)} -- a longer text is an event, "
                              "not a state")
    if any(unicodedata.category(c).startswith("C") for c in value):
        raise TimeseriesError("no control characters or line breaks in a state")
    return value


def check_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TimeseriesError("a number series takes numbers")
    v = float(value)
    if v != v or v in (float("inf"), float("-inf")):
        raise TimeseriesError("not a finite number")
    return v


def check_labels(labels):
    labels = labels or {}
    if not isinstance(labels, dict) or len(labels) > MAX_LABELS:
        raise TimeseriesError(f"labels: a map of at most {MAX_LABELS} pairs")
    for k, v in labels.items():
        if not isinstance(k, str) or not LABEL_RE.match(k):
            raise TimeseriesError(f"label name {k!r}: [a-z][a-z0-9_]{{0,31}}")
        if not isinstance(v, str) or not 0 < len(v) <= 64:
            raise TimeseriesError(f"label {k!r}: a text of 1 to 64 characters")
    return dict(sorted(labels.items()))


def tiers_of(policy):
    return [t for t in TIER_ORDER if t in POLICIES[policy]]


def text_retention(policy):
    """Text intervals live as long as the policy's longest tier (§4.4)."""
    return max(POLICIES[policy].values())


# --- the driver ----------------------------------------------------------

class PostgresDriver:
    """One tenant's series in one schema. Every public method takes the
    connection's transaction and commits it -- one call, one transaction."""

    name = "postgres"

    def __init__(self, conn, schema, max_series=DEFAULT_MAX_SERIES):
        if not SCHEMA_RE.match(schema or ""):
            raise TimeseriesError(f"not a time-series schema name: {schema!r}")
        self.conn = conn
        self.schema = schema
        self.max_series = max_series
        self._parts = set()

    # -- schema --

    def _q(self, name):
        return f'"{self.schema}"."{name}"'

    def ensure_schema(self):
        """Create the tables if they are missing. Idempotent."""
        s = self._q
        with self.conn.cursor() as cur:
            cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{self.schema}"')
            cur.execute(f"""CREATE TABLE IF NOT EXISTS {s('series')} (
                id bigserial PRIMARY KEY,
                object_id text NOT NULL,
                measure text NOT NULL,
                labels jsonb NOT NULL DEFAULT '{{}}',
                kind text NOT NULL CHECK (kind IN ('number','text')),
                unit text NOT NULL DEFAULT '',
                policy text NOT NULL CHECK (policy IN ('standard','short','long')),
                expected_s integer CHECK (expected_s IS NULL OR expected_s > 0),
                created timestamptz NOT NULL DEFAULT now(),
                UNIQUE (object_id, measure, labels))""")
            for pol in POLICIES:
                cur.execute(f"""CREATE TABLE IF NOT EXISTS {s('raw_' + pol)} (
                    series_id bigint NOT NULL, t timestamptz NOT NULL,
                    x double precision NOT NULL,
                    PRIMARY KEY (series_id, t)) PARTITION BY RANGE (t)""")
                for tier in tiers_of(pol):
                    cur.execute(f"""CREATE TABLE IF NOT EXISTS {s(f'a{tier}_{pol}')} (
                        series_id bigint NOT NULL, t timestamptz NOT NULL,
                        mean double precision NOT NULL,
                        min double precision NOT NULL,
                        max double precision NOT NULL,
                        count integer NOT NULL,
                        PRIMARY KEY (series_id, t)) PARTITION BY RANGE (t)""")
            cur.execute(f"""CREATE TABLE IF NOT EXISTS {s('text_states')} (
                series_id bigint NOT NULL, t_from timestamptz NOT NULL,
                value varchar({TEXT_MAX}) NOT NULL,
                last_seen timestamptz NOT NULL,
                PRIMARY KEY (series_id, t_from),
                CHECK (last_seen >= t_from))""")
            cur.execute(f"""CREATE TABLE IF NOT EXISTS {s('marks')} (
                policy text NOT NULL, tier text NOT NULL, upto timestamptz NOT NULL,
                PRIMARY KEY (policy, tier))""")
            cur.execute(f"""CREATE TABLE IF NOT EXISTS {s('dirty')} (
                policy text NOT NULL, tier text NOT NULL, series_id bigint NOT NULL,
                t timestamptz NOT NULL, PRIMARY KEY (policy, tier, series_id, t))""")
            cur.execute(f"""CREATE TABLE IF NOT EXISTS {s('counters')} (
                day date NOT NULL, name text NOT NULL, n bigint NOT NULL,
                PRIMARY KEY (day, name))""")
        self.conn.commit()

    def _part_name(self, table, start, size):
        d = _ts(start)
        return f"{table}_{d.strftime('%Y%m%d') if size == 'day' else d.strftime('%Y%m')}"

    @staticmethod
    def _part_end(rel, table):
        """The exclusive end of a partition, read from the name this driver
        gave it -- not from PostgreSQL's text of the bound, which is written
        in the SESSION's time zone."""
        suffix = rel[len(table) + 1:] if rel.startswith(table + "_") else ""
        try:
            if len(suffix) == 8:
                d = _dt.datetime.strptime(suffix, "%Y%m%d").replace(tzinfo=_dt.timezone.utc)
                return (d + _dt.timedelta(days=1)).timestamp()
            if len(suffix) == 6:
                d = _dt.datetime.strptime(suffix, "%Y%m").replace(tzinfo=_dt.timezone.utc)
                return _dt.datetime(d.year + (d.month == 12), d.month % 12 + 1, 1,
                                    tzinfo=_dt.timezone.utc).timestamp()
        except ValueError:
            return None
        return None

    @staticmethod
    def _bounds(epoch, size):
        d = _ts(epoch)
        if size == "day":
            lo = _dt.datetime(d.year, d.month, d.day, tzinfo=_dt.timezone.utc)
            hi = lo + _dt.timedelta(days=1)
        else:
            lo = _dt.datetime(d.year, d.month, 1, tzinfo=_dt.timezone.utc)
            hi = _dt.datetime(d.year + (d.month == 12), d.month % 12 + 1, 1,
                              tzinfo=_dt.timezone.utc)
        return lo, hi

    def _ensure_part(self, cur, table, epoch, size):
        lo, hi = self._bounds(epoch, size)
        name = self._part_name(table, lo.timestamp(), size)
        if name in self._parts:
            return
        cur.execute("SAVEPOINT p")
        try:
            cur.execute(f"""CREATE TABLE IF NOT EXISTS {self._q(name)}
                PARTITION OF {self._q(table)} FOR VALUES FROM (%s) TO (%s)""", (lo, hi))
            cur.execute("RELEASE SAVEPOINT p")
        except psycopg2.errors.DuplicateTable:
            cur.execute("ROLLBACK TO SAVEPOINT p")   # a concurrent writer made it
        self._parts.add(name)

    def ensure_partitions(self, now, ahead_days=2):
        """The job's first step: today's and the next days' partitions."""
        with self.conn.cursor() as cur:
            for pol in POLICIES:
                for d in range(ahead_days + 1):
                    self._ensure_part(cur, "raw_" + pol, now + d * DAY, "day")
                for tier in tiers_of(pol):
                    self._ensure_part(cur, f"a{tier}_{pol}", now, "month")
                    self._ensure_part(cur, f"a{tier}_{pol}", now + 31 * DAY, "month")
        self.conn.commit()

    def _count(self, cur, now, name, n):
        if n:
            cur.execute(f"""INSERT INTO {self._q('counters')} (day, name, n)
                VALUES (%s, %s, %s) ON CONFLICT (day, name)
                DO UPDATE SET n = {self._q('counters')}.n + EXCLUDED.n""",
                        (_ts(now).date(), name, n))

    # -- series --

    def ensure_series(self, object_id, measure, kind="number", labels=None,
                      unit="", policy="standard", expected_s=None):
        """Create or find. A found series must agree on kind and policy --
        both are fixed at creation (RFC-0086 §2)."""
        if not isinstance(object_id, str) or not object_id or len(object_id) > 200:
            raise TimeseriesError("a series belongs to a twin object: object id missing")
        if not isinstance(measure, str) or not MEASURE_RE.match(measure):
            raise TimeseriesError(f"measure {measure!r}: [a-z][a-z0-9_.]{{0,62}}")
        if kind not in KINDS:
            raise TimeseriesError(f"kind: one of {', '.join(KINDS)}")
        if policy not in POLICIES:
            raise TimeseriesError(f"policy: one of {', '.join(POLICIES)} "
                                  "(a new one is a change of the spec)")
        if expected_s is not None and (isinstance(expected_s, bool)
                                       or not isinstance(expected_s, int)
                                       or not 0 < expected_s <= DAY):
            raise TimeseriesError("expected interval: whole seconds, 1 to 86400")
        if not isinstance(unit, str) or len(unit) > 32:
            raise TimeseriesError("unit: at most 32 characters")
        labels = check_labels(labels)
        with self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"""SELECT * FROM {self._q('series')}
                WHERE object_id = %s AND measure = %s AND labels = %s::jsonb""",
                        (object_id, measure, json.dumps(labels)))
            row = cur.fetchone()
            if row:
                if row["kind"] != kind or row["policy"] != policy:
                    raise TimeseriesError(
                        f"this series exists as {row['kind']}/{row['policy']}; "
                        "kind and policy are fixed at creation")
                self.conn.commit()
                return self._public(row), False
            cur.execute(f"SELECT count(*) AS n FROM {self._q('series')}")
            if cur.fetchone()["n"] >= self.max_series:
                raise TimeseriesError(f"this tenant has {self.max_series} series, "
                                      "the limit -- ask the operator for more")
            cur.execute(f"""INSERT INTO {self._q('series')}
                (object_id, measure, labels, kind, unit, policy, expected_s)
                VALUES (%s, %s, %s::jsonb, %s, %s, %s, %s) RETURNING *""",
                        (object_id, measure, json.dumps(labels), kind, unit,
                         policy, expected_s))
            row = cur.fetchone()
        self.conn.commit()
        return self._public(row), True

    @staticmethod
    def _public(row):
        return {"id": row["id"], "object": row["object_id"], "measure": row["measure"],
                "labels": row["labels"], "kind": row["kind"], "unit": row["unit"],
                "policy": row["policy"], "expected_s": row["expected_s"],
                "created": iso(_epoch(row["created"]))}

    def get_series(self, series_id):
        with self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"SELECT * FROM {self._q('series')} WHERE id = %s", (series_id,))
            row = cur.fetchone()
        self.conn.commit()
        if not row:
            raise NotFound(f"no series {series_id} in this tenant")
        return self._public(row)

    def list(self, object_id=None):
        with self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if object_id:
                cur.execute(f"SELECT * FROM {self._q('series')} WHERE object_id = %s "
                            "ORDER BY measure, id", (object_id,))
            else:
                cur.execute(f"SELECT * FROM {self._q('series')} ORDER BY object_id, measure, id")
            rows = cur.fetchall()
        self.conn.commit()
        return [self._public(r) for r in rows]

    def drop_series(self, series_id):
        s = self.get_series(series_id)
        pol = s["policy"]
        with self.conn.cursor() as cur:
            cur.execute(f"DELETE FROM {self._q('raw_' + pol)} WHERE series_id = %s", (series_id,))
            for tier in tiers_of(pol):
                cur.execute(f"DELETE FROM {self._q(f'a{tier}_{pol}')} WHERE series_id = %s",
                            (series_id,))
            cur.execute(f"DELETE FROM {self._q('text_states')} WHERE series_id = %s", (series_id,))
            cur.execute(f"DELETE FROM {self._q('dirty')} WHERE series_id = %s", (series_id,))
            cur.execute(f"DELETE FROM {self._q('series')} WHERE id = %s", (series_id,))
        self.conn.commit()
        return s

    # -- writing --

    def _marks(self, cur, policy):
        cur.execute(f"SELECT tier, upto FROM {self._q('marks')} WHERE policy = %s", (policy,))
        return {r[0]: _epoch(r[1]) for r in cur.fetchall()}

    def write(self, series_id, points, now):
        """Append points [(t, x), ...]. Idempotent: a time already held is a
        duplicate, counted, never a second value. Answers counts."""
        if not isinstance(points, (list, tuple)):
            raise TimeseriesError("points: a list of [time, value]")
        if len(points) > MAX_BATCH:
            raise TimeseriesError(f"at most {MAX_BATCH} points per call")
        s = self.get_series(series_id)
        parsed = []
        for p in points:
            if isinstance(p, dict):
                p = (p.get("t"), p.get("x"))
            if not isinstance(p, (list, tuple)) or len(p) != 2:
                raise TimeseriesError("a point is [time, value]")
            t = parse_time(p[0])
            x = check_text(p[1]) if s["kind"] == "text" else check_number(p[1])
            parsed.append((t, x))
        if s["kind"] == "text":
            return self._write_text(s, parsed, now)
        return self._write_numbers(s, parsed, now)

    def _write_numbers(self, s, parsed, now):
        pol = s["policy"]
        cutoff = now - POLICIES[pol]["raw"]
        res = {"written": 0, "duplicate": 0, "refused": 0, "late": 0, "reasons": []}
        ok = []
        for t, x in parsed:
            if t < cutoff:
                res["refused"] += 1
                if not res["reasons"]:
                    res["reasons"].append(f"older than the raw retention of "
                                          f"'{pol}' ({POLICIES[pol]['raw'] // DAY} days)")
            elif t > now + FUTURE_SLACK:
                res["refused"] += 1
                res["reasons"].append("more than an hour in the future")
            else:
                ok.append((t, x))
        with self.conn.cursor() as cur:
            for day in {int(t) // DAY for t, _ in ok}:
                self._ensure_part(cur, "raw_" + pol, day * DAY, "day")
            inserted = []
            if ok:
                rows = psycopg2.extras.execute_values(
                    cur, f"""INSERT INTO {self._q('raw_' + pol)} (series_id, t, x)
                    VALUES %s ON CONFLICT DO NOTHING RETURNING t""",
                    [(s["id"], _ts(t) if t == int(t) else
                      _dt.datetime.fromtimestamp(t, _dt.timezone.utc), x) for t, x in ok],
                    page_size=1000, fetch=True)
                inserted = [_epoch(r[0]) for r in rows]
            res["written"] = len(inserted)
            res["duplicate"] = len(ok) - len(inserted)
            marks = self._marks(cur, pol)
            m5 = marks.get("5m")
            late = sorted({_floor(t, 300) for t in inserted if m5 is not None and t < m5})
            if late:
                psycopg2.extras.execute_values(
                    cur, f"""INSERT INTO {self._q('dirty')} (policy, tier, series_id, t)
                    VALUES %s ON CONFLICT DO NOTHING""",
                    [(pol, "5m", s["id"], _ts(b)) for b in late])
                res["late"] = sum(1 for t in inserted if t < m5)
            self._count(cur, now, "duplicate", res["duplicate"])
            self._count(cur, now, "refused", res["refused"])
            self._count(cur, now, "late", res["late"])
        self.conn.commit()
        res["reasons"] = res["reasons"][:3]
        return res

    def _write_text(self, s, parsed, now):
        pol = s["policy"]
        exp = s["expected_s"]
        raw_cutoff = now - POLICIES[pol]["raw"]
        res = {"written": 0, "extended": 0, "duplicate": 0, "refused": 0,
               "late": 0, "reasons": []}
        q = self._q("text_states")
        with self.conn.cursor() as cur:
            for t, v in sorted(parsed, key=lambda p: p[0]):
                if t > now + FUTURE_SLACK:
                    res["refused"] += 1
                    res["reasons"].append("more than an hour in the future")
                    continue
                tt = _dt.datetime.fromtimestamp(t, _dt.timezone.utc)
                cur.execute(f"""SELECT t_from, value, last_seen FROM {q}
                    WHERE series_id = %s ORDER BY t_from DESC LIMIT 1""", (s["id"],))
                last = cur.fetchone()
                if last is None or t > _epoch(last[2]):
                    gap = (last is not None and exp is not None
                           and t - _epoch(last[2]) > GAP_FACTOR * exp)
                    if last is not None and last[1] == v and not gap:
                        cur.execute(f"UPDATE {q} SET last_seen = %s WHERE series_id = %s "
                                    "AND t_from = %s", (tt, s["id"], last[0]))
                        res["extended"] += 1
                    else:
                        cur.execute(f"INSERT INTO {q} (series_id, t_from, value, last_seen) "
                                    "VALUES (%s, %s, %s, %s)", (s["id"], tt, v, tt))
                        res["written"] += 1
                    continue
                # at or before the last time seen: a duplicate or a late point
                cur.execute(f"""SELECT t_from, value, last_seen FROM {q}
                    WHERE series_id = %s AND t_from <= %s ORDER BY t_from DESC LIMIT 1""",
                            (s["id"], tt))
                cov = cur.fetchone()
                inside = cov is not None and t <= _epoch(cov[2])
                if inside and cov[1] == v:
                    res["duplicate"] += 1
                    continue
                if t < raw_cutoff:
                    res["refused"] += 1
                    if not res["reasons"]:
                        res["reasons"].append(f"a late point older than the raw "
                                              f"retention of '{pol}'")
                    continue
                res["late"] += 1
                if inside:
                    # split the interval it falls into: A | v at t | A again
                    a_from, a_val, a_seen = cov
                    after = tt + _dt.timedelta(microseconds=1)
                    if _epoch(cov[0]) == t:
                        cur.execute(f"UPDATE {q} SET value = %s, last_seen = %s WHERE "
                                    "series_id = %s AND t_from = %s", (v, tt, s["id"], a_from))
                    else:
                        cur.execute(f"UPDATE {q} SET last_seen = %s WHERE series_id = %s "
                                    "AND t_from = %s",
                                    (tt - _dt.timedelta(microseconds=1), s["id"], a_from))
                        cur.execute(f"INSERT INTO {q} VALUES (%s, %s, %s, %s)",
                                    (s["id"], tt, v, tt))
                    if _epoch(a_seen) > t:
                        cur.execute(f"INSERT INTO {q} VALUES (%s, %s, %s, %s) "
                                    "ON CONFLICT DO NOTHING", (s["id"], after, a_val, a_seen))
                else:
                    cur.execute(f"INSERT INTO {q} VALUES (%s, %s, %s, %s) "
                                "ON CONFLICT DO NOTHING", (s["id"], tt, v, tt))
                res["written"] += 1
            self._count(cur, now, "duplicate", res["duplicate"])
            self._count(cur, now, "refused", res["refused"])
            self._count(cur, now, "late", res["late"])
        self.conn.commit()
        res["reasons"] = res["reasons"][:3]
        return res

    # -- the job --

    def roll_up(self, now):
        """Compute every bucket whose interval closed at least two intervals
        ago, tier by tier, and recompute the buckets late points touched.
        Answers how many buckets were written per policy and tier."""
        out = {}
        with self.conn.cursor() as cur:
            for pol in POLICIES:
                src = "raw_" + pol
                src_is_raw = True
                marks = self._marks(cur, pol)
                for tier in tiers_of(pol):
                    size = TIERS[tier]
                    dst = f"a{tier}_{pol}"
                    limit = _floor(now - 2 * size, size)
                    lo = marks.get(tier)
                    if lo is None:
                        cur.execute(f"SELECT min(t) FROM {self._q(src)}")
                        first = cur.fetchone()[0]
                        lo = _floor(_epoch(first), size) if first is not None else limit
                    n = 0
                    if limit > lo:
                        for m in range(int(lo) // DAY * DAY, limit + 31 * DAY, 28 * DAY):
                            if m < limit:
                                self._ensure_part(cur, dst, max(m, lo), "month")
                        self._ensure_part(cur, dst, limit - 1, "month")
                        n += self._aggregate(cur, src, dst, size, src_is_raw,
                                             _ts(lo), _ts(limit), None)
                        cur.execute(f"""INSERT INTO {self._q('marks')} (policy, tier, upto)
                            VALUES (%s, %s, %s) ON CONFLICT (policy, tier)
                            DO UPDATE SET upto = EXCLUDED.upto""", (pol, tier, _ts(limit)))
                    # buckets late points invalidated
                    cur.execute(f"""DELETE FROM {self._q('dirty')} WHERE policy = %s
                        AND tier = %s RETURNING series_id, t""", (pol, tier))
                    for sid, bt in cur.fetchall():
                        b = _epoch(bt)
                        self._ensure_part(cur, dst, b, "month")
                        cur.execute(f"DELETE FROM {self._q(dst)} WHERE series_id = %s AND t = %s",
                                    (sid, bt))
                        n += self._aggregate(cur, src, dst, size, src_is_raw,
                                             bt, _ts(b + size), sid)
                        nxt = TIER_ORDER.index(tier) + 1
                        if nxt < len(TIER_ORDER) and TIER_ORDER[nxt] in POLICIES[pol]:
                            up = TIER_ORDER[nxt]
                            if marks.get(up) is not None and b < marks[up]:
                                cur.execute(f"""INSERT INTO {self._q('dirty')}
                                    VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING""",
                                            (pol, up, sid, _ts(_floor(b, TIERS[up]))))
                    out[f"{pol}/{tier}"] = n
                    src, src_is_raw = dst, False
        self.conn.commit()
        return out

    def _aggregate(self, cur, src, dst, size, src_is_raw, lo, hi, series_id):
        bucket = f"to_timestamp(floor(extract(epoch FROM t) / {size}) * {size})"
        if src_is_raw:
            sel = (f"SELECT series_id, {bucket} AS b, avg(x), min(x), max(x), count(*) "
                   f"FROM {self._q(src)}")
        else:
            sel = (f"SELECT series_id, {bucket} AS b, sum(mean * count) / sum(count), "
                   f"min(min), max(max), sum(count) FROM {self._q(src)}")
        where = "WHERE t >= %s AND t < %s" + (" AND series_id = %s" if series_id else "")
        args = (lo, hi, series_id) if series_id else (lo, hi)
        cur.execute(f"""INSERT INTO {self._q(dst)} (series_id, t, mean, min, max, count)
            {sel} {where} GROUP BY series_id, b
            ON CONFLICT (series_id, t) DO UPDATE SET mean = EXCLUDED.mean,
            min = EXCLUDED.min, max = EXCLUDED.max, count = EXCLUDED.count""", args)
        return cur.rowcount

    def expire(self, now):
        """Drop partitions past their retention; delete text intervals last
        seen before theirs. Never a DELETE on points. Answers counts."""
        dropped = []
        with self.conn.cursor() as cur:
            for pol, ret in POLICIES.items():
                for tier, keep in ret.items():
                    table = "raw_" + pol if tier == "raw" else f"a{tier}_{pol}"
                    cur.execute("""SELECT c.relname
                        FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid
                        JOIN pg_class p ON p.oid = i.inhparent
                        JOIN pg_namespace n ON n.oid = p.relnamespace
                        WHERE n.nspname = %s AND p.relname = %s""", (self.schema, table))
                    for (rel,) in cur.fetchall():
                        hi = self._part_end(rel, table)
                        if hi is not None and hi <= now - keep:
                            cur.execute(f"DROP TABLE {self._q(rel)}")
                            self._parts.discard(rel)
                            dropped.append(rel)
            texts = 0
            for pol in POLICIES:
                cur.execute(f"""DELETE FROM {self._q('text_states')} ts USING {self._q('series')} s
                    WHERE s.id = ts.series_id AND s.policy = %s AND ts.last_seen < %s""",
                            (pol, _ts(now - text_retention(pol))))
                texts += cur.rowcount
            self._count(cur, now, "partitions_dropped", len(dropped))
            self._count(cur, now, "text_states_expired", texts)
        self.conn.commit()
        return {"partitions_dropped": dropped, "text_states_expired": texts}

    def job(self, now):
        """The minutely platform job (RFC-0086 §5), in one call."""
        self.ensure_partitions(now)
        r = self.roll_up(now)
        e = self.expire(now)
        return {"rolled_up": r, **e}

    # -- reading --

    def latest(self, series_id):
        s = self.get_series(series_id)
        with self.conn.cursor() as cur:
            if s["kind"] == "text":
                cur.execute(f"""SELECT last_seen, value FROM {self._q('text_states')}
                    WHERE series_id = %s ORDER BY t_from DESC LIMIT 1""", (series_id,))
            else:
                cur.execute(f"""SELECT t, x FROM {self._q('raw_' + s['policy'])}
                    WHERE series_id = %s ORDER BY t DESC LIMIT 1""", (series_id,))
            row = cur.fetchone()
        self.conn.commit()
        return None if row is None else {"t": iso(_epoch(row[0])), "x": row[1]}

    def choose_tier(self, policy, frm, to, step, now):
        """RFC-0086 §4.3. Answers (tier, resolution, note)."""
        cands = [("raw", RAW_RESOLUTION)] + [(t, TIERS[t]) for t in tiers_of(policy)]
        ret = POLICIES[policy]
        # what the step alone asks for: the coarsest tier not coarser than it
        wanted = ([c for c in cands if c[1] <= step] or cands[:1])[-1]
        covering = [c for c in cands if frm >= now - ret[c[0]]]
        note = ""
        if not covering:
            tier, res = cands[-1]
            note = f"'{tier}' is the longest tier of '{policy}'; older data is gone"
        else:
            fine = [c for c in covering if c[1] <= step]
            tier, res = fine[-1] if fine else covering[0]
            if (tier, res) != wanted:
                note = (f"'from' lies beyond the retention of '{wanted[0]}'; "
                        f"answered from '{tier}'")
        while (to - frm) / max(step, res) > MAX_BUCKETS:
            i = cands.index((tier, res))
            if i + 1 >= len(cands):
                break
            tier, res = cands[i + 1]
            note = f"more than {MAX_BUCKETS} buckets asked; answered from '{tier}'"
        return tier, res, note

    def range(self, series_id, frm, to, step, now):
        """Numbers: buckets of mean/min/max/count at `step`, from the tier
        §4.3 picks. Texts: the state intervals clipped to [from, to], or per
        step the value at its end, the changes in it and the value held
        longest."""
        s = self.get_series(series_id)
        frm, to = parse_time(frm), parse_time(to)
        if to <= frm:
            raise TimeseriesError("'to' must lie after 'from'")
        if step is not None:
            step = int(step)
            if step <= 0:
                raise TimeseriesError("step: seconds, more than 0")
        if s["kind"] == "text":
            return self._range_text(s, frm, to, step, now)
        if step is None:
            step = max(RAW_RESOLUTION, int((to - frm) // MAX_BUCKETS) or RAW_RESOLUTION)
        tier, res, note = self.choose_tier(s["policy"], frm, to, step, now)
        step = max(step, res)
        while (to - frm) / step > MAX_BUCKETS:
            step *= 2
        table = "raw_" + s["policy"] if tier == "raw" else f"a{tier}_{s['policy']}"
        bucket = f"floor(extract(epoch FROM t) / {step}) * {step}"
        if tier == "raw":
            agg = "avg(x), min(x), max(x), count(*)"
        else:
            agg = "sum(mean * count) / sum(count), min(min), max(max), sum(count)"
        with self.conn.cursor() as cur:
            cur.execute(f"""SELECT {bucket} AS b, {agg} FROM {self._q(table)}
                WHERE series_id = %s AND t >= %s AND t < %s GROUP BY b ORDER BY b""",
                        (series_id, _ts(_floor(frm, step)), _ts(to)))
            rows = cur.fetchall()
        self.conn.commit()
        return {"series": s["id"], "kind": "number", "tier": tier, "step": step,
                "note": note,
                "buckets": [{"t": iso(float(b)), "mean": mean, "min": mn, "max": mx,
                             "count": int(c)} for b, mean, mn, mx, c in rows]}

    def intervals(self, series_id, frm, to, now):
        """The state intervals of a text series overlapping [frm, to):
        [{from, to, value, open}] with gaps left out (§4.4)."""
        s = self.get_series(series_id)
        exp = s["expected_s"]
        with self.conn.cursor() as cur:
            cur.execute(f"""(SELECT t_from, value, last_seen FROM {self._q('text_states')}
                    WHERE series_id = %s AND t_from < %s AND t_from >= %s ORDER BY t_from)
                UNION ALL
                (SELECT t_from, value, last_seen FROM {self._q('text_states')}
                    WHERE series_id = %s AND t_from < %s ORDER BY t_from DESC LIMIT 1)
                UNION ALL
                (SELECT t_from, value, last_seen FROM {self._q('text_states')}
                    WHERE series_id = %s AND t_from >= %s ORDER BY t_from LIMIT 1)""",
                        (series_id, _ts(to), _ts(frm), series_id, _ts(frm), series_id, _ts(to)))
            rows = sorted({(_epoch(a), b, _epoch(c)) for a, b, c in cur.fetchall()})
        self.conn.commit()
        out = []
        for i, (f, v, seen) in enumerate(rows):
            nxt = rows[i + 1][0] if i + 1 < len(rows) else None
            gap_after = exp is not None and nxt is not None and nxt - seen > GAP_FACTOR * exp
            is_open = nxt is None and (exp is None or now - seen <= GAP_FACTOR * exp)
            if nxt is not None and not gap_after:
                end = nxt
            elif is_open:
                end = max(now, seen)
            else:
                end = seen
            if end < frm or f >= to:
                continue
            out.append({"from": max(f, frm), "to": min(end, to), "value": v,
                        "start": f, "open": bool(is_open)})
        return s, out

    def _range_text(self, s, frm, to, step, now):
        s, iv = self.intervals(s["id"], frm, to, now)
        if step is None:
            return {"series": s["id"], "kind": "text",
                    "intervals": [{"from": iso(i["from"]), "to": iso(i["to"]),
                                   "value": i["value"],
                                   "open": i["open"] and i["to"] >= min(to, now)}
                                  for i in iv]}
        while (to - frm) / step > MAX_BUCKETS:
            step *= 2
        buckets = []
        b = _floor(frm, step)
        while b < to:
            e = b + step
            held, changes, at_end = {}, 0, None
            for i in iv:
                overlap = min(i["to"], e) - max(i["from"], b)
                if overlap > 0:
                    held[i["value"]] = held.get(i["value"], 0) + overlap
                if b <= i["start"] < e:
                    changes += 1          # a state began in this bucket
                if i["from"] < e and (i["to"] >= e or i["open"]):
                    at_end = i["value"]   # the state just before the bucket's end
            buckets.append({"t": iso(b), "at_end": at_end, "changes": changes,
                            "longest": max(held, key=held.get) if held else None})
            b = e
        return {"series": s["id"], "kind": "text", "step": step, "buckets": buckets}

    # -- counters, size --

    def counters(self, since_day=None):
        with self.conn.cursor() as cur:
            cur.execute(f"SELECT name, sum(n) FROM {self._q('counters')} "
                        + ("WHERE day >= %s " if since_day else "") + "GROUP BY name",
                        (since_day,) if since_day else None)
            rows = dict(cur.fetchall())
        self.conn.commit()
        return {k: int(v) for k, v in rows.items()}

    def size(self):
        """Bytes of the schema: tables, partitions and indexes."""
        with self.conn.cursor() as cur:
            cur.execute("""SELECT coalesce(sum(pg_total_relation_size(c.oid)), 0)
                FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = %s AND c.relkind = 'r'""", (self.schema,))
            b = int(cur.fetchone()[0])
        self.conn.commit()
        return b

    # -- export / import (driver-neutral, JSON lines) --

    def export(self, out):
        """Every series with its raw points, buckets and text intervals, one
        JSON object per line. The default driver's backup uses pg_dump of
        the schema (RFC-0086 §6); this form is what moves data between
        DRIVERS, and the conformance suite's round trip."""
        n = 0
        for s in self.list():
            out.write(json.dumps({"series": s}) + "\n")
            pol = s["policy"]
            with self.conn.cursor() as cur:
                if s["kind"] == "text":
                    cur.execute(f"SELECT t_from, value, last_seen FROM {self._q('text_states')} "
                                "WHERE series_id = %s ORDER BY t_from", (s["id"],))
                    for f, v, seen in cur:
                        out.write(json.dumps({"s": s["id"], "text": [_epoch(f), v, _epoch(seen)]}) + "\n")
                        n += 1
                else:
                    cur.execute(f"SELECT t, x FROM {self._q('raw_' + pol)} WHERE series_id = %s "
                                "ORDER BY t", (s["id"],))
                    for t, x in cur:
                        out.write(json.dumps({"s": s["id"], "raw": [_epoch(t), x]}) + "\n")
                        n += 1
                    for tier in tiers_of(pol):
                        cur.execute(f"SELECT t, mean, min, max, count FROM "
                                    f"{self._q(f'a{tier}_{pol}')} WHERE series_id = %s ORDER BY t",
                                    (s["id"],))
                        for row in cur:
                            out.write(json.dumps({"s": s["id"], "tier": tier,
                                                  "b": [_epoch(row[0])] + list(row[1:])}) + "\n")
                            n += 1
            self.conn.commit()
        return n

    def import_(self, lines):
        """The other half of export: series are matched by object, measure
        and labels; points go in idempotently."""
        idmap, pols, n = {}, {}, 0
        with self.conn.cursor() as cur:
            for line in lines:
                d = json.loads(line)
                if "series" in d:
                    s = d["series"]
                    new, _ = self.ensure_series(s["object"], s["measure"], s["kind"],
                                                s["labels"], s["unit"], s["policy"],
                                                s["expected_s"])
                    idmap[s["id"]], pols[s["id"]] = new["id"], s["policy"]
                    continue
                sid, pol = idmap[d["s"]], pols[d["s"]]
                if "raw" in d:
                    t, x = d["raw"]
                    self._ensure_part(cur, "raw_" + pol, t, "day")
                    cur.execute(f"INSERT INTO {self._q('raw_' + pol)} VALUES (%s, %s, %s) "
                                "ON CONFLICT DO NOTHING",
                                (sid, _dt.datetime.fromtimestamp(t, _dt.timezone.utc), x))
                elif "tier" in d:
                    t = d["b"][0]
                    tbl = f"a{d['tier']}_{pol}"
                    self._ensure_part(cur, tbl, t, "month")
                    cur.execute(f"INSERT INTO {self._q(tbl)} VALUES (%s, %s, %s, %s, %s, %s) "
                                "ON CONFLICT DO NOTHING",
                                [sid, _dt.datetime.fromtimestamp(t, _dt.timezone.utc)] + d["b"][1:])
                elif "text" in d:
                    f, v, seen = d["text"]
                    cur.execute(f"INSERT INTO {self._q('text_states')} VALUES (%s, %s, %s, %s) "
                                "ON CONFLICT DO NOTHING",
                                (sid, _dt.datetime.fromtimestamp(f, _dt.timezone.utc), v,
                                 _dt.datetime.fromtimestamp(seen, _dt.timezone.utc)))
                n += 1
        self.conn.commit()
        return n


def _rolls_back(fn):
    """A refused or failed call must not leave its transaction open: an
    'idle in transaction' connection keeps its locks, and the next DDL of
    any writer waits forever (found by the conformance suite, 2026-11-12)."""
    import functools

    @functools.wraps(fn)
    def wrapper(self, *a, **kw):
        try:
            return fn(self, *a, **kw)
        except BaseException:
            try:
                self.conn.rollback()
            except psycopg2.Error:
                pass
            raise
    return wrapper


for _name in ("ensure_schema", "ensure_partitions", "ensure_series", "get_series",
              "list", "drop_series", "write", "roll_up", "expire", "job", "latest",
              "range", "intervals", "counters", "size", "export", "import_"):
    setattr(PostgresDriver, _name, _rolls_back(getattr(PostgresDriver, _name)))
