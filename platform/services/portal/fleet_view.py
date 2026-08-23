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

SCHEMA = "oaap.fleet.status/0.1"

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
                   dns_rows, pending_names):
    """Assemble the versioned status document (RFC-0021 §1)."""
    return {
        "schema": SCHEMA,
        "node": node,
        "platform_version": version,
        "profiles": list(profiles or []),
        "time": now_iso,
        "core": [{"name": c.get("name", "").lower(),
                  "state": normalize_state(c.get("state", ""))}
                 for c in core],
        "instances": instances,
        "attention": attention_items(core, instances, dns_rows,
                                     pending_names),
    }
