"""Half-edges for the fleet status document (RFC-0087, oaap.fleet.status 0.10).

Each node reports the links it holds, as it sees them: the tunnels it
accepts (outer) and dials (inner), and the destinations that go through a
tunnel (`via`) or a tunnel app (`through`). The hub joins the halves
(services/twin/fleet_sink.py). Pure: no Flask, no files -- app.py reads
the configuration and hands it in, so every rule here is tested without a
node.

Two rules shape everything:

* **Facts, never configuration** (oaap.fleet.status rule 1): no endpoint,
  no offer address, no path, no credential. Where an offer ends is said as
  the name of an own instance, or `external`.
* **The node decides how much it gives away** (RFC-0087 §4): a ceiling set
  by the server_admin, a level per tenant at or below it, chosen by its
  tenant_admin. `count` < `pseudonym` < `names`.
"""
import hashlib
import hmac
from urllib.parse import urlsplit

LEVELS = ("count", "pseudonym", "names")
DEFAULT_CEILING = "pseudonym"
ID_PREFIX = "oaap-tunnel-id/1\n"


# --- the join key (RFC-0087 §3) --------------------------------------------

def tunnel_id(key_hash_hex):
    """The tunnel's shared, non-secret id from the connect key's SHA-256
    hex digest -- what the outer node stores. The inner node, which holds
    the key, gets the same id through tunnel_id_from_key. Not a verifier:
    a hash of the stored hash under a fixed prefix."""
    if not key_hash_hex:
        return ""
    return "t-" + hashlib.sha256((ID_PREFIX + key_hash_hex).encode()).hexdigest()[:24]


def tunnel_id_from_key(key):
    """The same id on the inner side, from the key itself."""
    return tunnel_id(hashlib.sha256(key.encode()).hexdigest()) if key else ""


# --- how much leaves the node (RFC-0087 §4) --------------------------------

def _rank(level):
    return LEVELS.index(level) if level in LEVELS else -1


def ceiling(settings):
    c = (settings or {}).get("ceiling")
    return c if c in LEVELS else DEFAULT_CEILING


def tenant_level(settings, tid):
    """The tenant's own choice, never above the node's ceiling; no choice =
    the ceiling. Without a pseudonym salt `pseudonym` cannot be honoured and
    falls to `count` -- the sparser side, never the more talkative one."""
    top = ceiling(settings)
    own = ((settings or {}).get("tenants") or {}).get(tid)
    level = own if own in LEVELS and _rank(own) <= _rank(top) else top
    if level == "pseudonym" and not (settings or {}).get("salt"):
        return "count"
    return level


def _pseudo(settings, prefix, text):
    salt = (settings or {}).get("salt") or ""
    if not salt:
        return ""
    return prefix + hmac.new(salt.encode(), text.encode(), hashlib.sha256).hexdigest()[:10]


class Disclosure:
    """What a tenant is called outside the node, at its level."""

    def __init__(self, settings, tenants):
        self.settings = settings or {}
        self.tenants = tenants or {}

    def level(self, tid):
        return tenant_level(self.settings, tid)

    def tenant(self, tid):
        """The tenant's outward name, or "" at `count`."""
        level = self.level(tid)
        if level == "names":
            return (self.tenants.get(tid) or {}).get("label") or tid
        if level == "pseudonym":
            return _pseudo(self.settings, "m-", "tenant/" + tid)
        return ""

    def thing(self, tid, prefix, name):
        """A name inside a tenant (a destination, an external target): the
        name at `names`, a stable pseudonym below it."""
        if self.level(tid) == "names":
            return name
        return _pseudo(self.settings, prefix, f"{prefix}/{tid}/{name}")


# --- the rows ---------------------------------------------------------------

def _host(url):
    try:
        return (urlsplit(url).hostname or "").lower().strip(".")
    except ValueError:
        return ""


def reaches(url, hosts):
    """Where an offer ends: the registry name of an own instance when its
    target's host is one of that instance's names, else `external`. The
    address itself never leaves (rule 1)."""
    return hosts.get(_host(url), "external")


def _registry_key(instances, tid, local, resolve):
    """A `through` target names the tunnel app by its name INSIDE the tenant
    (`ovc`, oaap.net.destinations 2.8); every other row of the document --
    and the hub's instance objects -- use the registry key (`ais-ovc`).
    Found live on oaap-demo, 2026-11-14: without this the external end had
    no `behind`."""
    for key, inst in sorted(instances.items()):
        if (resolve(inst.get("tenant")) or "") == tid and (inst.get("name") or key) == local:
            return key
    return local


def _bound(instances, tid, name, resolve):
    return sorted(n for n, inst in instances.items()
                  if (resolve(inst.get("tenant")) or "") == tid
                  and name in ((inst.get("destinations") or {}).values()))


def build(conf, state, dests, instances, tenants, settings, hosts, resolve):
    """(links, tenants block, {instance: outward tenant name}).

    conf     -- the connect configuration ({keys, connectors})
    state    -- the connect service's state.json ({tunnels, connectors})
    dests    -- {tenant id: {name: destination}}
    instances-- the registry {name: entry}
    tenants  -- {tenant id: {label, ...}}
    settings -- the disclosure file ({ceiling, salt, tenants})
    hosts    -- {host name: instance} of this node's instances
    resolve  -- tenant reference -> tenant id (absent = default)
    """
    d = Disclosure(settings, tenants)
    conf, state = conf or {}, state or {}
    st_t, st_c = state.get("tunnels") or {}, state.get("connectors") or {}
    links = []
    ids_by_label = {}

    used = {}
    for own in (dests or {}).values():
        for dd in own.values():
            via = ((dd.get("target") or {}).get("via") or "")
            if via:
                used[via.split("/")[0]] = True

    for label, rec in sorted((conf.get("keys") or {}).items()):
        tid_ = tunnel_id(rec.get("hash", ""))
        if not tid_:
            continue
        ids_by_label[label] = tid_
        s = st_t.get(label) or {}
        row = {"kind": "tunnel", "side": "outer", "id": tid_, "label": label,
               "state": "ok" if s.get("connected") else ("error" if used.get(label) else "warn"),
               "offers": sorted(str(o.get("name") if isinstance(o, dict) else o)
                                for o in s.get("offers") or [])}
        if s.get("since"):
            row["since"] = s["since"]
        tname = d.tenant(rec.get("tenant", ""))
        if tname:
            row["tenant"] = tname
        links.append(row)

    for label, c in sorted((conf.get("connectors") or {}).items()):
        s = st_c.get(label) or {}
        tid_ = s.get("tunnel_id") or ""
        if not tid_:
            continue            # no key yet, or a connect service before 0.1.281
        row = {"kind": "tunnel", "side": "inner", "id": tid_, "label": label,
               "state": "ok" if s.get("connected") else "error",
               "paused": bool(c.get("paused")),
               "offers": [{"name": o, "reaches": reaches((c.get("offers") or {})[o].get("to", ""), hosts)}
                          for o in sorted((c.get("offers") or {}).keys())]}
        if s.get("since"):
            row["since"] = s["since"]
        # never the last error's text: it names the endpoint ("Cannot
        # connect to host ...") -- configuration, rule 1
        links.append(row)

    for tid, own in sorted((dests or {}).items()):
        for name, dd in sorted(own.items()):
            target = dd.get("target") or {}
            row = None
            if target.get("via"):
                lab, _, offer = target["via"].partition("/")
                row = {"kind": "destination",
                       "via": {"tunnel": ids_by_label.get(lab, ""), "offer": offer}}
            elif target.get("through"):
                local, _, tname = target["through"].partition("/")
                ext = d.thing(tid, "x-", tname)
                if not ext:
                    continue
                row = {"kind": "destination",
                       "through": {"instance": _registry_key(instances or {}, tid, local, resolve),
                                   "target": ext}}
            if row is None:
                continue        # direct: its far end is configuration (RFC-0087 §2 rule 2)
            dname = d.thing(tid, "d-", name)
            if not dname:
                continue        # count without salt: nothing nameable leaves
            row["name"] = dname
            tname = d.tenant(tid)
            if tname:
                row["tenant"] = tname
            bound = _bound(instances or {}, tid, name, resolve)
            row["bound"] = bound if d.level(tid) != "count" else len(bound)
            links.append(row)

    per = {}
    inst_tenant = {}
    for n, inst in (instances or {}).items():
        tid = resolve(inst.get("tenant")) or ""
        per[tid] = per.get(tid, 0) + 1
        tname = d.tenant(tid) if tid else ""
        if tname:
            inst_tenant[n] = tname
    items = []
    for tid in sorted(tenants or {}):
        tname = d.tenant(tid)
        if tname:
            items.append({"tenant": tname, "level": d.level(tid), "instances": per.get(tid, 0)})
    block = {"count": len(tenants or {}), "ceiling": ceiling(settings), "items": items}
    return links, block, inst_tenant
