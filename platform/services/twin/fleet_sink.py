"""The fleet service's writer into the hub's twin (RFC-0085 stage 4 data
part, amendment A2; RFC-0086 stage 2).

fleet.py collects what arrives (status documents, alive, metric points)
under its lock; every SINK_EVERY seconds its main loop hands a snapshot to
`Sink.flush`, which writes it -- outside the lock, so a slow database never
stalls the broker callbacks. What could not be written stays pending and
is tried again next time; points beyond POINTS_MAX are dropped oldest
first and counted (`points_dropped` in state.json), never silently.

Where it writes is set by the host (`oaap fleet hub twin`): /fleet/hub.json
names the tenant (`oaap-ops` by default) and the hub's own name. Without
that file nothing is written -- the fleet works as in stage 2.

Rules, all from twin_write (the API's own):
  * objects are found by their source key under the origin
    `platform:fleet` -- a node by its name, an instance as `<node>/<name>`,
    a broker by the name of the node it runs on;
  * a group is the fleet's when the fleet created it; `owner.*` are
    people's and never touched here;
  * an unchanged value writes nothing, so a status document that arrives
    again (a resubscribe, a restart) costs reads, not rows.

Half-edges (RFC-0087): a node's `links` and `tenants` (oaap.fleet.status
0.10) become tenants, tunnels, destinations and external ends. A tunnel is
ONE object for two nodes (`tunnel:<id>`), each writing its own half's group;
whatever disappears from a node's document is marked `removed` and its
relations are closed with `valid_to` -- history stays, nothing is deleted.
A node that still speaks 0.9 sends no `links`, and nothing of its links is
touched.
"""
import json
import os
import time

import psycopg2
import psycopg2.extras

import fleet_model as fm
import fleet_affected as fa
import fleet_twin as ft
import timeseries as ts
import twin_write as tw

STORE_HOST = os.environ.get("STORE_HOST", "store")
STORE_PORT = int(os.environ.get("STORE_PORT", "5432"))
SECRETS_FILE = os.environ.get("FLEET_SECRETS", "/platform-apps/twin-secrets.json")
SINK_EVERY = 30
POINTS_MAX = 50000
BY = "fleet"


def _load(path):
    try:
        with open(path, encoding="utf-8") as f:
            v = json.load(f)
        return v if isinstance(v, dict) else None
    except (OSError, ValueError):
        return None


class Sink:
    def __init__(self, fleet_dir):
        self.dir = fleet_dir
        self.ids = {}           # source key -> object id
        self.series = {}        # (object id, measure) -> series id
        # tunnel object id -> {"outer": state, "inner": (state, paused)}: the
        # halves last seen, for the joined state series (RFC-0087 §5). Not a
        # cache of the database -- it survives a failed flush.
        self.halves = {}
        self.last_error = ""

    # -- where

    def target(self):
        """(tenant id, hub name) from /fleet/hub.json, or None."""
        hub = _load(os.path.join(self.dir, "hub.json")) or {}
        tid = str(hub.get("tenant") or "")
        return (tid, str(hub.get("hub") or "")) if tid else None

    def _conns(self, tid):
        secrets = _load(SECRETS_FILE) or {}
        twin, tsc = secrets.get(f"twin_{tid}"), secrets.get(f"ts_{tid}")
        if not twin:
            raise RuntimeError(f"no twin credential for tenant {tid} -- 'oaap fleet hub twin'")
        conn = psycopg2.connect(host=STORE_HOST, port=STORE_PORT, dbname="postgres",
                                user=twin["role"], password=twin["password"],
                                options=f"-c search_path=twin_{tid}", connect_timeout=5,
                                cursor_factory=psycopg2.extras.RealDictCursor)
        drv = None
        if tsc:
            drv = ts.PostgresDriver(
                psycopg2.connect(host=STORE_HOST, port=STORE_PORT, dbname="postgres",
                                 user=tsc["role"], password=tsc["password"], connect_timeout=5),
                f"ts_{tid}")
        return conn, drv

    # -- objects

    def _object(self, conn, type_key, source_key, title, core):
        oid = self.ids.get(source_key)
        if oid is None:
            oid, _created = tw.ensure_object(conn, type_key, source_key, title, fm.ORIGIN, BY, core)
            self.ids[source_key] = oid
        return oid

    def _groups(self, conn, oid, groups):
        n = 0
        for key, values in groups.items():
            n += tw.write_group(conn, oid, key, fm.ORIGIN, BY, {"attributes": fm.attrs(values)})
        return n

    def _series(self, drv, oid, measure, kind="number", unit=""):
        k = (oid, measure)
        sid = self.series.get(k)
        if sid is None:
            s, _made = drv.ensure_series(oid, measure, kind=kind, unit=unit,
                                         expected_s=60 if kind == "number" else None)
            sid = self.series[k] = s["id"]
        return sid

    def _points(self, drv, sid, pts, now):
        drv.admit(len(pts), now)
        return drv.write(sid, pts, now)

    # -- one flush

    def flush(self, snap, now=None):
        """snap: {"status": {key: (name, via, doc)} -- new documents only,
        "nodes": {key: (name, via, last alive)} -- every node, every time: a
        node that falls silent sends nothing, so its liveness is written from
        the verdict, not from an arrival; "points": [(name, measure, t, x)],
        "brokers": {name: counters}, "bridges": {key: {entry, hub, up}},
        "verdict": {name: {state, since}}}. Returns (written rows, what is
        still pending: {"status", "points"}) -- raises nothing; the error, if
        any, is in self.last_error. A document leaves `status` only after
        its transaction committed."""
        now = now or time.time()
        where = self.target()
        if where is None:
            return 0, snap
        tid, hub = where
        rows = 0
        left = {"status": dict(snap.get("status") or {}),
                "points": list(snap.get("points") or [])}
        conn = drv = None
        try:
            conn, drv = self._conns(tid)
            with conn:
                rows += self._write_brokers(conn, drv, hub, snap, now)
                for _key, (name, via, doc) in list(left["status"].items()):
                    rows += self._write_status(conn, drv, name, via, doc, hub, now)
                verdict = snap.get("verdict") or {}
                for _key, (name, _via, alive) in (snap.get("nodes") or {}).items():
                    oid = self._object(conn, fm.NODE, name, name, "oaap.placement")
                    rows += self._groups(conn, oid, {"oaap.liveness": ft.liveness(
                        alive, verdict.get(name))})
                    if drv is not None:
                        st = (verdict.get(name) or {}).get("state") or "unknown"
                        self._points(drv, self._series(drv, oid, fm.TEXT_STATE, "text"),
                                     [(int(now) // 60 * 60, st)], now)
                if drv is not None:
                    t = int(now) // 60 * 60
                    for toid, h in list(self.halves.items()):
                        st = ft.joined_state(h.get("outer"), h.get("inner"))
                        self._points(drv, self._series(drv, toid, fm.TEXT_STATE, "text"),
                                     [(t, st)], now)
            left["status"] = {}         # committed
            self._write_affected(conn, now)
            if drv is not None and left["points"]:
                by = {}
                for name, measure, t, x in left["points"]:
                    by.setdefault((name, measure), []).append((t, x))
                with conn:
                    for (name, measure), pts in by.items():
                        oid = self._object(conn, fm.NODE, name, name, "oaap.placement")
                        sid = self._series(drv, oid, measure, unit=fm.METRIC_UNITS[measure])
                        for i in range(0, len(pts), 1000):
                            self._points(drv, sid, pts[i:i + 1000], now)
                left["points"] = []
            elif drv is None:
                left["points"] = []     # no time series for this tenant: nothing to keep them for
            self.last_error = ""
        except Exception as exc:  # noqa: BLE001 -- the next flush tries again
            self.last_error = f"{type(exc).__name__}: {str(exc)[:300]}"
            self.ids.clear()
            self.series.clear()
        finally:
            for c in (conn, getattr(drv, "conn", None)):
                try:
                    if c is not None:
                        c.close()
                except Exception:  # noqa: BLE001
                    pass
        return rows, left

    def _write_affected(self, conn, now):
        """RFC-0087 §6: /fleet/affected.json for the host's minute job --
        every tunnel that is down, with what depends on it. Written on every
        flush, empty when nothing is down, so its age says whether the fleet
        service still vouches for it; the graph is read only when a tunnel
        is down. A failure here is the flush's failure: the next one tries
        again, and a stale report keeps open matters open on the host."""
        states = {toid: ft.joined_state(h.get("outer"), h.get("inner"))
                  for toid, h in self.halves.items()}
        rels, objects = ([], {})
        if any(st in fa.DOWN for st in states.values()):
            with conn:
                rels, objects = fa.load_graph(conn)
        path = os.path.join(self.dir, "affected.json")
        doc = fa.link_report(states, rels, objects, _load(path) or {}, now)
        tmp = path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False)
        os.replace(tmp, path)

    def _write_status(self, conn, drv, name, via, doc, hub, now):
        oid = self._object(conn, fm.NODE, name, name, "oaap.placement")
        groups = ft.node_groups(doc, via)
        rows = self._groups(conn, oid, groups)
        broker = via or hub
        if broker:
            bid = self._object(conn, fm.BROKER, "broker:" + broker, broker, "oaap.broker")
            rows += tw.write_group(conn, oid, "oaap.placement", fm.ORIGIN, BY,
                                   {"relations": [{"key": fm.ak("reportsThrough"),
                                                   "target": bid}]})
        seen = set()
        for inst, igroups in ft.instance_rows(doc):
            iid = self._object(conn, fm.INSTANCE, f"{name}/{inst}", inst, "oaap.runtime")
            seen.add(f"{name}/{inst}")
            rows += self._groups(conn, iid, igroups)
            rows += tw.write_group(conn, iid, "oaap.runtime", fm.ORIGIN, BY,
                                   {"relations": [{"key": fm.ak("isPartOf"), "target": oid}]})
        rows += self._mark_gone(conn, name, seen)
        rows += self._write_links(conn, name, oid, doc, now)
        if drv is not None:
            t = int(now) // 60 * 60
            for measure, state in ft.core_states(doc).items():
                self._points(drv, self._series(drv, oid, measure, "text"), [(t, state)], now)
        return rows

    def _mark_gone(self, conn, name, seen):
        """Instances of this node the fleet created that are no longer in its
        status: state `removed`, the record and its history stay."""
        with conn.cursor() as cur:
            cur.execute("SELECT object_id, source_key FROM source_keys WHERE origin = %s "
                        "AND source_key LIKE %s", (fm.ORIGIN, name.replace("%", "") + "/%"))
            rows = cur.fetchall()
        n = 0
        for r in rows:
            if r["source_key"] not in seen:
                n += tw.write_group(conn, str(r["object_id"]), "oaap.runtime", fm.ORIGIN, BY,
                                    {"attributes": fm.attrs(ft.gone_runtime())})
        return n

    def _write_brokers(self, conn, drv, hub, snap, now):
        rows = 0
        t = int(now) // 60 * 60
        names = set((snap.get("brokers") or {}).keys()) | ({hub} if hub else set())
        for b in (snap.get("bridges") or {}).values():
            names |= {b.get("entry"), b.get("hub")}
        names.discard(None)
        names.discard("")
        bridges = list((snap.get("bridges") or {}).values())
        entries = {b.get("entry") for b in bridges}
        # a hub ELSE whose bridge comes into this node (a test bench is hub
        # and entry at once) is neither this hub nor one of its entries
        foreign = {b.get("hub") for b in bridges if b.get("hub") != hub} - entries
        for name in sorted(names):
            bid = self._object(conn, fm.BROKER, "broker:" + name, name, "oaap.broker")
            role = "hub" if name == hub else ("remote-hub" if name in foreign else "entry")
            rows += self._groups(conn, bid, {"oaap.broker": {"role": role}})
            c = (snap.get("brokers") or {}).get(name) or {}
            if drv is not None and isinstance(c.get("queued"), (int, float)):
                self._points(drv, self._series(drv, bid, "queued"), [(t, c["queued"])], now)
        for b in (snap.get("bridges") or {}).values():
            if b.get("hub") != hub or not b.get("entry"):
                continue
            eid = self._object(conn, fm.BROKER, "broker:" + b["entry"], b["entry"], "oaap.broker")
            hid = self._object(conn, fm.BROKER, "broker:" + hub, hub, "oaap.broker")
            rows += tw.write_group(conn, eid, "oaap.broker", fm.ORIGIN, BY,
                                   {"relations": [{"key": fm.ak("bridgedTo"), "target": hid}]})
            if drv is not None:
                self._points(drv, self._series(drv, eid, "bridge", "text"),
                             [(t, "up" if b.get("up") else "down")], now)
        return rows

    # -- half-edges (RFC-0087)

    def _sync_rel(self, conn, oid, group, rel_key, targets, now_iso):
        """The open relations `rel_key` of this group are exactly `targets`:
        new ones opened, missing ones closed with valid_to (never deleted)."""
        rk = fm.ak(rel_key)
        targets = sorted({str(t) for t in targets if t})
        with conn.cursor() as cur:
            cur.execute("SELECT target_id, valid_from FROM current_relations WHERE object_id = %s "
                        "AND group_key = %s AND rel_key = %s AND valid_to IS NULL",
                        (oid, group, rk))
            current = cur.fetchall()
        rels = [{"key": rk, "target": t} for t in targets]
        rels += [{"key": rk, "target": str(r["target_id"]), "valid_from": r["valid_from"],
                  "valid_to": now_iso}
                 for r in current if str(r["target_id"]) not in targets]
        return tw.write_group(conn, oid, group, fm.ORIGIN, BY, {"relations": rels})

    def _prefixed(self, conn, prefix):
        """{source key: object id} of the fleet's objects under a prefix."""
        like = prefix.replace("\\", "").replace("%", "").replace("_", "\\_") + "%"
        with conn.cursor() as cur:
            cur.execute("SELECT object_id, source_key FROM source_keys WHERE origin = %s "
                        "AND source_key LIKE %s", (fm.ORIGIN, like))
            return {r["source_key"]: str(r["object_id"]) for r in cur.fetchall()}

    def _open_from(self, conn, rel_key, target):
        """Objects with an open relation `rel_key` to `target`."""
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT object_id FROM current_relations WHERE rel_key = %s "
                        "AND target_id = %s AND valid_to IS NULL", (fm.ak(rel_key), target))
            return {str(r["object_id"]) for r in cur.fetchall()}

    def _write_links(self, conn, name, oid, doc, now):
        if not isinstance(doc, dict) or ("links" not in doc and "tenants" not in doc):
            return 0            # a 0.9 node: its links are not ours to judge
        iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
        rows = 0

        tenants, seen = {}, set()
        for key, title, groups in ft.tenant_rows(doc, name):
            xid = self._object(conn, fm.TENANT, key, title, "oaap.tenant")
            tenants[title] = xid
            seen.add(key)
            rows += self._groups(conn, xid, groups)
            rows += self._sync_rel(conn, xid, "oaap.tenant", "tenantOn", [oid], iso)
        for key, xid in self._prefixed(conn, f"tenant:{name}/").items():
            if key not in seen:
                rows += self._groups(conn, xid, {"oaap.tenant": {"level": ft.REMOVED}})
                rows += self._sync_rel(conn, xid, "oaap.tenant", "tenantOn", [], iso)

        inst_oid = {}
        for inst, _g in ft.instance_rows(doc):
            inst_oid[inst] = self._object(conn, fm.INSTANCE, f"{name}/{inst}", inst, "oaap.runtime")

        outer, inner = set(), set()
        for tid_, side, groups, reached in ft.tunnel_halves(doc):
            toid = self._object(conn, fm.TUNNEL, "tunnel:" + tid_, tid_, "oaap.tunnel." + side)
            rows += self._groups(conn, toid, groups)
            h = self.halves.setdefault(toid, {})
            g = groups["oaap.tunnel." + side]
            if side == "outer":
                outer.add(toid)
                h["outer"] = g["outerState"]
                rows += self._sync_rel(conn, toid, "oaap.tunnel.outer", "servedBy", [oid], iso)
            else:
                inner.add(toid)
                h["inner"] = (g["innerState"], g["innerPaused"])
                rows += self._sync_rel(conn, toid, "oaap.tunnel.inner", "dialedBy", [oid], iso)
                rows += self._sync_rel(conn, toid, "oaap.tunnel.inner", "reaches",
                                       [inst_oid[i] for i in reached if i in inst_oid], iso)
        if "links" in doc:
            for toid in self._open_from(conn, "servedBy", oid) - outer:
                rows += self._groups(conn, toid, {"oaap.tunnel.outer": {"outerState": ft.REMOVED}})
                rows += self._sync_rel(conn, toid, "oaap.tunnel.outer", "servedBy", [], iso)
                self.halves.get(toid, {}).pop("outer", None)
            for toid in self._open_from(conn, "dialedBy", oid) - inner:
                rows += self._groups(conn, toid, {"oaap.tunnel.inner": {"innerState": ft.REMOVED}})
                rows += self._sync_rel(conn, toid, "oaap.tunnel.inner", "dialedBy", [], iso)
                rows += self._sync_rel(conn, toid, "oaap.tunnel.inner", "reaches", [], iso)
                self.halves.get(toid, {}).pop("inner", None)
            for toid in [k for k, v in self.halves.items() if not v]:
                del self.halves[toid]

        binds, seen_d, seen_e = {}, set(), set()
        for d in ft.destinations(doc, name):
            doid = self._object(conn, fm.DESTINATION, d["key"], d["title"], "oaap.destination")
            seen_d.add(d["key"])
            rows += self._groups(conn, doid, d["groups"])
            rows += self._sync_rel(conn, doid, "oaap.destination", "definedOn", [oid], iso)
            rows += self._sync_rel(conn, doid, "oaap.destination", "destOf",
                                   [tenants[d["tenant"]]] if d["tenant"] in tenants else [], iso)
            tun = []
            if d["tunnel"]:
                tun = [self._object(conn, fm.TUNNEL, "tunnel:" + d["tunnel"], d["tunnel"],
                                    "oaap.tunnel.outer")]
            rows += self._sync_rel(conn, doid, "oaap.destination", "usesTunnel", tun, iso)
            ext = []
            if d["external"]:
                ekey, etitle, egroups, einst = d["external"]
                eoid = self._object(conn, fm.EXTERNAL, ekey, etitle, "oaap.external")
                seen_e.add(ekey)
                ext = [eoid]
                rows += self._groups(conn, eoid, egroups)
                rows += self._sync_rel(conn, eoid, "oaap.external", "behind",
                                       [inst_oid[einst]] if einst in inst_oid else [], iso)
            rows += self._sync_rel(conn, doid, "oaap.destination", "usesExternal", ext, iso)
            for b in d["bound"]:
                binds.setdefault(b, []).append(doid)
        for key, xid in self._prefixed(conn, f"dest:{name}/").items():
            if key not in seen_d:
                rows += self._groups(conn, xid, {"oaap.destination": {"destState": ft.REMOVED}})
                for rk in ("definedOn", "destOf", "usesTunnel", "usesExternal"):
                    rows += self._sync_rel(conn, xid, "oaap.destination", rk, [], iso)
        for key, xid in self._prefixed(conn, f"ext:{name}/").items():
            if key not in seen_e:
                rows += self._groups(conn, xid, {"oaap.external": {"extState": ft.REMOVED}})
                rows += self._sync_rel(conn, xid, "oaap.external", "behind", [], iso)

        it = ft.instance_tenants(doc)
        for inst, iid in inst_oid.items():
            held = [tenants[it[inst]]] if it.get(inst) in tenants else []
            rows += self._sync_rel(conn, iid, "oaap.runtime", "heldBy", held, iso)
            rows += self._sync_rel(conn, iid, "oaap.runtime", "bindsTo", binds.get(inst, []), iso)
        return rows

