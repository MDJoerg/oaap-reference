"""The tenant reader's writer (RFC-0088 stage 1): asks each app's `assets`
door and writes what it names into the twin of the instance's own tenant.

Run by the host every five minutes, inside the twin's container, which
already holds what this needs: the tenants' twin and time-series
credentials (/platform-apps/twin-secrets.json, read-only), the write rules
(twin_write) and the platform network to the gateway's internal endpoint
(`/a/<instance>`, the same the status document uses):

    docker exec -i oaap-twin-1 python assets_run.py < plan.json

The plan (JSON on stdin) is the host's -- it knows the registry, the
tenants' setting (D2) and which instance sleeps:
    {"now": epoch, "tenants": {tid: {name: {"app": id, "ask": bool}}}}

One JSON line on stdout: {"tenants": {tid: {"ok", "error", "rows", "items",
"instances": {name: ok|paused|unknown}}}}. Never prints a credential.

Rules (the fleet's, fleet_sink): objects found by source key under origin
`platform:assets`; an unchanged value writes nothing; what an answering app
no longer names is `removed` and its relations closed, history stays; an
app that sleeps (`paused`) or gives no usable answer (`unknown`) changes
none of its assets; an app gone from the plan is `removed` with them.
"""
import json
import os
import sys
import time

import urllib.error
import urllib.request

import psycopg2
import psycopg2.extras

import assets_model as am
import assets_twin as at
import timeseries as ts
import twin_write as tw

STORE_HOST = os.environ.get("STORE_HOST", "store")
STORE_PORT = int(os.environ.get("STORE_PORT", "5432"))
SECRETS_FILE = "/platform-apps/twin-secrets.json"
GATEWAY = os.environ.get("ASSETS_GATEWAY", "http://gateway:8099")
ASK_TIMEOUT = 3
BY = "assets"


def _secrets():
    try:
        with open(SECRETS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def ask(instance):
    """The tenant reader's list, or None (no usable answer)."""
    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    try:
        op = urllib.request.build_opener(_NoRedirect)
        with op.open(f"{GATEWAY}/a/{instance}", timeout=ASK_TIMEOUT) as r:
            if r.status != 200:
                return None
            return at.read_tenant_answer(json.loads(r.read(1024 * 1024).decode("utf-8")))
    except (urllib.error.URLError, OSError, ValueError):
        return None


class Writer:
    def __init__(self, conn, drv, now):
        self.conn, self.drv, self.now = conn, drv, now
        self.iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
        self.ids = {}
        self.rows = 0

    def obj(self, type_key, key, title, core):
        oid = self.ids.get(key)
        if oid is None:
            oid, _made = tw.ensure_object(self.conn, type_key, key, title, am.ORIGIN, BY, core)
            self.ids[key] = oid
        return oid

    def group(self, oid, group, values):
        self.rows += tw.write_group(self.conn, oid, group, am.ORIGIN, BY,
                                    {"attributes": am.attrs(values)})

    def sync_rel(self, oid, rel, targets):
        """The open relations `rel` of oaap.assetitem are exactly `targets`."""
        rk = am.ak(rel)
        targets = sorted({str(t) for t in targets if t})
        with self.conn.cursor() as cur:
            cur.execute("SELECT target_id, valid_from FROM current_relations WHERE object_id = %s "
                        "AND group_key = %s AND rel_key = %s AND valid_to IS NULL",
                        (oid, "oaap.assetitem", rk))
            current = cur.fetchall()
        rels = [{"key": rk, "target": t} for t in targets]
        rels += [{"key": rk, "target": str(r["target_id"]), "valid_from": r["valid_from"],
                  "valid_to": self.iso} for r in current if str(r["target_id"]) not in targets]
        self.rows += tw.write_group(self.conn, oid, "oaap.assetitem", am.ORIGIN, BY,
                                    {"relations": rels})

    def known(self):
        """{source key: object id} of everything this origin wrote."""
        with self.conn.cursor() as cur:
            cur.execute("SELECT object_id, source_key FROM source_keys WHERE origin = %s",
                        (am.ORIGIN,))
            return {r["source_key"]: str(r["object_id"]) for r in cur.fetchall()}

    def remove_item(self, oid):
        self.group(oid, "oaap.assetitem", {"state": am.REMOVED})
        for rk in at.REL_KEYS:
            self.sync_rel(oid, rk, [])

    def point(self, oid, state):
        if self.drv is None:
            return
        s, _made = self.drv.ensure_series(oid, am.TEXT_STATE, kind="text", unit="",
                                          expected_s=None)
        self.drv.admit(1, self.now)
        self.drv.write(s["id"], [(int(self.now) // 60 * 60, state)], self.now)

    def instance(self, name, app, answer):
        """answer: the reader's list, or the state "paused"/"unknown"."""
        aoid = self.obj(am.APP, at.app_key(name), name, "oaap.assetapp")
        state = answer if isinstance(answer, str) else "ok"
        self.group(aoid, "oaap.assetapp", {"instance": name, "app": app, "appState": state})
        if isinstance(answer, str):
            return 0
        oids = {}
        for it in answer:
            key = at.item_key(name, it["kind"], it["name"])
            oid = self.obj(am.ITEM, key, it["name"], "oaap.assetitem")
            oids[(it["kind"], it["name"])] = oid
            self.group(oid, "oaap.assetitem", it["values"])
            self.sync_rel(oid, "assetOf", [aoid])
        for it in answer:
            oid = oids[(it["kind"], it["name"])]
            for rk in at.REL_KEYS[1:]:
                self.sync_rel(oid, rk, [oids[t] for t in it["rel"].get(rk, []) if t in oids])
            if it["kind"] in am.SERIES_KINDS:
                self.point(oid, it["values"].get("state", "unknown"))
        seen = {at.item_key(name, k, n) for k, n in oids}
        for key, oid in self.known().items():
            if key.startswith(name + "/") and key not in seen:
                self.remove_item(oid)
        return len(answer)

    def gone_apps(self, keep):
        """Apps of this tenant no longer in the plan: removed, with their assets."""
        known = self.known()
        for key, oid in known.items():
            if "/" in key or key in keep:
                continue
            self.group(oid, "oaap.assetapp", {"appState": am.REMOVED})
            for k2, o2 in known.items():
                if k2.startswith(key + "/"):
                    self.remove_item(o2)


def run_tenant(tid, insts, now, secrets_map):
    res = {"ok": False, "error": "", "rows": 0, "items": 0, "instances": {}}
    twin, tsc = secrets_map.get(f"twin_{tid}"), secrets_map.get(f"ts_{tid}")
    if not twin:
        res["error"] = "no twin credential"
        return res
    answers = {}
    for name, spec in sorted(insts.items()):
        if isinstance(spec.get("answer"), dict):
            # the node's own assets (RFC-0135 stage 2b: gates), same rules
            answers[name] = at.read_tenant_answer(spec["answer"]) or "unknown"
        else:
            answers[name] = (ask(name) if spec.get("ask") else "paused") or "unknown"
        res["instances"][name] = answers[name] if isinstance(answers[name], str) else "ok"
    conn = drv = None
    try:
        conn = psycopg2.connect(host=STORE_HOST, port=STORE_PORT, dbname="postgres",
                                user=twin["role"], password=twin["password"],
                                options=f"-c search_path=twin_{tid}", connect_timeout=5,
                                cursor_factory=psycopg2.extras.RealDictCursor)
        if tsc:
            drv = ts.PostgresDriver(
                psycopg2.connect(host=STORE_HOST, port=STORE_PORT, dbname="postgres",
                                 user=tsc["role"], password=tsc["password"], connect_timeout=5),
                f"ts_{tid}")
        w = Writer(conn, drv, now)
        with conn:
            for name, answer in answers.items():
                res["items"] += w.instance(name, str(insts[name].get("app") or ""), answer)
            w.gone_apps(set(insts))
        res["rows"], res["ok"] = w.rows, True
    except Exception as exc:  # noqa: BLE001 -- said in the result, the next run tries again
        res["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
    finally:
        for c in (conn, getattr(drv, "conn", None)):
            try:
                if c is not None:
                    c.close()
            except Exception:  # noqa: BLE001
                pass
    return res


def main():
    plan = json.load(sys.stdin)
    now = float(plan.get("now") or time.time())
    secrets_map = _secrets()
    out = {tid: run_tenant(tid, insts, now, secrets_map)
           for tid, insts in sorted((plan.get("tenants") or {}).items())}
    print(json.dumps({"tenants": out}))


if __name__ == "__main__":
    main()
