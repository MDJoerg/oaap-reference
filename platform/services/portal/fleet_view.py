"""Fleet status document (RFC-0021, spec `oaap.fleet.status` 0.1).

The rules live here, Flask-free, so they are testable without a node:
which bearer key is valid, which facts an instance row may carry, and
what earns a place on the `attention` list. `app.py` only wires this to
the request and to the same probes the health page uses.

The one rule that shapes everything: the document carries FACTS about
the landscape and NEVER credentials — no tokens, no digests, no config
values, no source URLs (a source URL has already leaked a PAT once).
`instance_row` is therefore a whitelist, not a copy.
"""
import hashlib
import hmac

SCHEMA = "oaap.fleet.status/0.2"

# The document's state vocabulary. The health page grew two spellings
# ("err" from probes, "error" from the worker); consumers get one.
_STATES = {"ok": "ok", "warn": "warn", "err": "error", "error": "error",
           "unknown": "unknown"}


def normalize_state(state):
    return _STATES.get(state, "unknown")


def valid_key(presented, keys):
    """Return the label of the matching fleet key, or "".

    `keys` is the stored map {label: {"digest": sha256-hex, ...}}.
    Every stored digest is compared (constant-time each); the label is
    returned so a later audit can say WHO read, not just that someone
    did.
    """
    if not presented:
        return ""
    digest = hashlib.sha256(presented.encode()).hexdigest()
    found = ""
    for label, entry in sorted(keys.items()):
        # No early exit — the loop's work must not depend on where a
        # match sits.
        if hmac.compare_digest(entry.get("digest", ""), digest):
            found = label
    return found


def instance_row(name, inst, state):
    """One instance as facts — a whitelist of safe fields.

    `inst` is the raw registry entry and may contain a source URL with
    embedded credentials; only the source TYPE ever leaves this
    function.
    """
    row = {
        "instance": name,
        "app": inst.get("app_name", name),
        "version": inst.get("version", "?"),
        "channel": inst.get("channel", "production"),
        "state": normalize_state(state),
    }
    if inst.get("promoted_from"):
        origin = "promoted"
    else:
        origin = (inst.get("source") or {}).get("type", "")
    if origin:
        row["origin"] = origin
    address = inst.get("address") or ""
    if address:
        row["address"] = address
    return row


def name_row(row):
    """One published name as facts (schema 0.2) — again a whitelist.

    The health page's dns_check rows carry a German `what` label; the
    document gets a normalized kind plus the owning instance, so a
    consumer never has to parse prose.
    """
    what = row.get("what", "")
    if what == "Knoten":
        kind, instance = "node", ""
    elif what.endswith("(Alias)"):
        kind = "alias"
        instance = what[len("Instanz "):-len(" (Alias)")].strip()
    elif what.startswith("Instanz "):
        kind, instance = "instance", what[len("Instanz "):].strip()
    else:
        kind, instance = "unknown", ""
    out = {"name": row.get("name", ""), "kind": kind,
           "state": normalize_state(row.get("state", ""))}
    if instance:
        out["instance"] = instance
    resolved = row.get("resolved", "")
    if resolved and resolved != "–":
        out["resolved"] = resolved
    return out


def attention_items(core, instances, dns_rows, pending_names):
    """What needs a human — the machine-readable "Bestätigung offen".

    Consumers must tolerate kinds they do not know (RFC-0021); this
    list is the open end of the schema.
    """
    items = []
    for c in core:
        if normalize_state(c.get("state", "")) == "error":
            items.append({"kind": "core_service_down",
                          "detail": c.get("name", "")})
    for r in dns_rows or []:
        if r.get("state") == "warn":
            items.append({"kind": "dns_drift",
                          "detail": f"{r.get('name', '')}: points elsewhere"})
        elif r.get("state") == "err":
            items.append({"kind": "dns_unresolved",
                          "detail": r.get("name", "")})
    for name in sorted(pending_names or []):
        items.append({"kind": "confirmation_pending", "instance": name})
    for row in instances:
        if row.get("state") == "error":
            items.append({"kind": "instance_unhealthy",
                          "instance": row.get("instance", "")})
    return items


def build_document(node, version, profiles, now_iso, core, instances,
                   dns_rows, pending_names, public_ip=""):
    """Assemble the versioned status document (RFC-0021 §1).

    Schema 0.2 adds `names` (published names with the node's own DNS
    verdicts) and `public_ip` — both additive, consumers of 0.1 keep
    working (spec rule: additive changes bump the minor).
    """
    doc = {
        "schema": SCHEMA,
        "node": node,
        "platform_version": version,
        "profiles": list(profiles or []),
        "time": now_iso,
        "core": [{"name": c.get("name", "").lower(),
                  "state": normalize_state(c.get("state", ""))}
                 for c in core],
        "instances": instances,
        "names": [name_row(r) for r in dns_rows or []],
        "attention": attention_items(core, instances, dns_rows,
                                     pending_names),
    }
    if public_ip:
        doc["public_ip"] = public_ip
    return doc
