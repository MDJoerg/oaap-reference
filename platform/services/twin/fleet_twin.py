"""What the fleet writes into the twin (RFC-0085 §9, amendment A2) -- pure.

A node's status document (oaap.fleet.status 0.9), its `alive` and the
host's verdict go in; the groups of fleet_model come out, keyed by short
names and turned into registered keys by fleet_model.attrs(). Nothing here
reads a file, a clock or a database, so every rule is tested without one.

What deliberately does NOT go into a group: anything that changes every
minute. A group row is history (RFC-0031 §3.4, append-only); a value that
changes each minute belongs in a series (RFC-0086). So `alive` gives the
group only its interval and its loss counters, the broker's queue is a
series, and the metrics are series of the node object.
"""
import fleet_model as fm

DETAIL_MAX = 400


def _worst(states):
    order = {"error": 3, "warn": 2, "unknown": 1, "ok": 0}
    return max(states, key=lambda s: order.get(s, 1)) if states else ""


def _join(items, limit=DETAIL_MAX):
    text = ", ".join(str(i) for i in items if i not in (None, ""))
    return text if len(text) <= limit else text[:limit - 1] + "…"


def node_groups(doc, via=""):
    """{group key: {short name: value}} from one status document."""
    doc = doc if isinstance(doc, dict) else {}
    host = doc.get("host") or {}
    plan = host.get("reboot_plan") or {}
    adv = host.get("advisories") or {}
    img = host.get("images") or {}
    prot = host.get("protections") or {}
    core = [c for c in doc.get("core") or [] if isinstance(c, dict)]
    att = [a for a in doc.get("attention") or [] if isinstance(a, dict)]
    names = [n for n in doc.get("names") or [] if isinstance(n, dict)]
    bad = [n for n in names if n.get("state") not in ("ok", None)]
    out = {
        "oaap.software": {"platformVersion": doc.get("platform_version"),
                          "os": host.get("os"),
                          "profiles": _join(sorted(doc.get("profiles") or []))},
        "oaap.placement": {"alias": doc.get("alias") or "", "place": doc.get("place") or "",
                           "tags": _join(sorted(doc.get("tags") or [])),
                           "address": doc.get("node") or "", "via": via or ""},
        "oaap.services": {"coreServices": _join(f"{c.get('name')} {c.get('state')}"
                                                for c in sorted(core, key=lambda c: str(c.get("name")))),
                          "coreWorst": _worst([c.get("state") for c in core])},
        "oaap.attention": {"attentionCount": len(att),
                           "attentionKinds": _join(sorted({a.get("kind") for a in att if a.get("kind")})),
                           "attentionDetail": _join(f"{a.get('kind')}: {a.get('detail') or a.get('instance') or ''}"
                                                    for a in att)},
        "oaap.dns": {"publicIp": doc.get("public_ip") or "", "namesTotal": len(names),
                     "namesNotOk": len(bad),
                     "namesProblems": _join(f"{n.get('name')} {n.get('state')}" for n in bad)},
    }
    if host:
        out["oaap.hostcare"] = {
            "measured": host.get("measured"), "kernelRunning": host.get("kernel_running"),
            "kernelInstalled": host.get("kernel_installed"),
            "rebootRequired": host.get("reboot_required"),
            "autoUpdates": host.get("auto_updates"),
            "autoUpdatesLastRun": host.get("auto_updates_last_run"),
            "rebootWindow": host.get("reboot_window"),
            "rebootPlanAt": plan.get("at") or "", "rebootPlanUrgency": plan.get("urgency") or "",
            "rebootPlanState": plan.get("state") or "",
            "advisoriesOpen": adv.get("open"), "advisoriesOldest": adv.get("oldest") or None,
            "advisoriesStale": adv.get("stale"), "imagesOpen": img.get("open"),
            "protectionsCount": prot.get("count", 0), "protectionsUntil": prot.get("until") or "",
        }
    return out


def instance_rows(doc):
    """[(instance name, {group key: {short name: value}})] -- the instance
    groups of one status document, whitelisted field by field like the
    document itself (oaap.fleet.status rule 1)."""
    out = []
    for r in (doc or {}).get("instances") or []:
        if not isinstance(r, dict) or not r.get("instance"):
            continue
        out.append((str(r["instance"]), {
            "oaap.appsoftware": {"app": r.get("app") or "", "appVersion": r.get("version") or "",
                                 "instanceName": r["instance"]},
            "oaap.runtime": {"channel": r.get("channel") or "", "runState": r.get("state") or "",
                             "origin": r.get("origin") or "", "address": r.get("address") or "",
                             "autoAddress": r.get("auto_address") or "",
                             "autoState": r.get("auto_state") or ""},
        }))
    return out


def gone_runtime():
    """An instance that left the node's status: its record stays (history),
    its state says so -- nothing is deleted from the twin."""
    return {"runState": "removed"}


def liveness(alive, verdict):
    """The liveness group from the last `alive` and the host's verdict
    ({state, since}); only slow values, see the module docstring."""
    alive = alive if isinstance(alive, dict) else {}
    verdict = verdict if isinstance(verdict, dict) else {}
    return {"state": verdict.get("state") or "unknown", "since": verdict.get("since"),
            "interval": alive.get("interval"), "lostTotal": alive.get("lost"),
            "eventsDropped": alive.get("events_dropped")}


def core_states(doc):
    """{text series measure: state} for the core services of a document."""
    return {fm.TEXT_CORE_PREFIX + str(c.get("name")).replace("-", "_"): str(c.get("state") or "unknown")
            for c in (doc or {}).get("core") or []
            if isinstance(c, dict) and c.get("name")}


def metric_point(payload):
    """One metrics message (metrics.sample_lines) -> (measure, t, x) or None."""
    if not isinstance(payload, dict):
        return None
    m, t, x = payload.get("m"), payload.get("t"), payload.get("x")
    if m not in fm.METRIC_UNITS or not isinstance(t, str):
        return None
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    return m, t, x


# --- half-edges (RFC-0087) --------------------------------------------------
#
# Source keys get a prefix per kind, so that the instance sweep (`<node>/%`,
# fleet_sink._mark_gone) never takes a tenant or a destination for an
# instance: `tenant:<node>/<name>`, `dest:<node>/<tenant>/<name>`,
# `ext:<node>/<instance>/<target>`, `tunnel:<id>` -- the tunnel alone has no
# node in its key, because two nodes write it.

import re as _re

TUNNEL_ID = _re.compile(r"^t-[0-9a-f]{24}$")
_NAME = _re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
REMOVED = "removed"
# what a `through` row may say of its external end (oaap.fleet.status 0.11,
# RFC-0087 §8.1); without it the end is only `present`
EXT_STATES = ("ok", "down-tunnel", "down-target", "paused", "unknown")
EXT_DOWN = ("down-tunnel", "down-target")


def _name(v):
    return str(v) if isinstance(v, str) and _NAME.match(v) else ""


def _links(doc):
    return [lk for lk in (doc or {}).get("links") or [] if isinstance(lk, dict)]


def tenant_rows(doc, node):
    """[(source key, title, {group: values})] -- the tenants the node names
    (at `pseudonym` or `names`); a tenant at `count` is only a number."""
    out = []
    block = (doc or {}).get("tenants") or {}
    for t in block.get("items") or [] if isinstance(block, dict) else []:
        name = _name((t or {}).get("tenant"))
        if not name:
            continue
        out.append((f"tenant:{node}/{name}", name, {"oaap.tenant": {
            "tenantName": name, "level": str(t.get("level") or ""),
            "instanceCount": t.get("instances") if isinstance(t.get("instances"), int) else None}}))
    return out


def tenant_count(doc):
    block = (doc or {}).get("tenants") or {}
    c = block.get("count") if isinstance(block, dict) else None
    return c if isinstance(c, int) else None


def instance_tenants(doc):
    """{instance: outward tenant name} from the instance rows (0.10)."""
    return {str(r["instance"]): _name(r.get("tenant"))
            for r in (doc or {}).get("instances") or []
            if isinstance(r, dict) and r.get("instance") and _name(r.get("tenant"))}


def tunnel_halves(doc):
    """[(tunnel id, side, {group: values}, [instances an offer reaches])]."""
    out = []
    for lk in _links(doc):
        if lk.get("kind") != "tunnel" or not TUNNEL_ID.match(str(lk.get("id") or "")):
            continue
        side = lk.get("side")
        if side == "outer":
            offers = [_name(o) for o in lk.get("offers") or []]
            out.append((lk["id"], "outer", {"oaap.tunnel.outer": {
                "outerLabel": _name(lk.get("label")), "outerState": str(lk.get("state") or "unknown"),
                "outerSince": lk.get("since") or None,
                "outerOffers": _join(sorted(o for o in offers if o))}}, []))
        elif side == "inner":
            offers = [o for o in lk.get("offers") or [] if isinstance(o, dict) and _name(o.get("name"))]
            reached = sorted({_name(o.get("reaches")) for o in offers} - {"", "external"})
            out.append((lk["id"], "inner", {"oaap.tunnel.inner": {
                "innerLabel": _name(lk.get("label")), "innerState": str(lk.get("state") or "unknown"),
                "innerPaused": bool(lk.get("paused")),
                "innerOffers": _join(f"{o['name']} → {_name(o.get('reaches')) or 'external'}"
                                     for o in offers)}}, reached))
    return out


def destinations(doc, node):
    """[{key, title, tenant, groups, tunnel, external: (key, title, groups,
    instance) | None, bound: [instances]}] -- `via` and `through` rows."""
    out = []
    for lk in _links(doc):
        if lk.get("kind") != "destination":
            continue
        name = _name(lk.get("name"))
        if not name:
            continue
        tenant = _name(lk.get("tenant"))
        via, through = lk.get("via"), lk.get("through")
        bound = lk.get("bound")
        row = {"key": f"dest:{node}/{tenant or '-'}/{name}", "title": name, "tenant": tenant,
               "tunnel": "", "external": None,
               "bound": [b for b in (bound if isinstance(bound, list) else []) if _name(b)]}
        values = {"destName": name, "destState": "present",
                  "boundCount": len(bound) if isinstance(bound, list)
                  else (bound if isinstance(bound, int) else None)}
        if isinstance(via, dict):
            values.update(destKind="via", destOffer=_name(via.get("offer")))
            if TUNNEL_ID.match(str(via.get("tunnel") or "")):
                row["tunnel"] = via["tunnel"]
        elif isinstance(through, dict):
            inst, target = _name(through.get("instance")), _name(through.get("target"))
            values.update(destKind="through", destOffer="")
            if inst and target:
                st = through.get("state")
                row["external"] = (f"ext:{node}/{inst}/{target}", target,
                                   {"oaap.external": {"extName": target, "extKind": "vpn",
                                                      "extState": st if st in EXT_STATES
                                                      else "present"}}, inst)
        else:
            continue
        row["groups"] = {"oaap.destination": values}
        out.append(row)
    return out


def joined_state(outer, inner):
    """The tunnel's state over time (RFC-0087 §5): from the two halves the
    hub last saw -- outer: its state or None, inner: (state, paused) or None."""
    if outer is None or inner is None:
        return "half"
    if inner[1]:
        return "paused"
    if inner[0] != "ok":
        return "down-inner"
    if outer != "ok":
        return "down-outer"
    return "ok"
