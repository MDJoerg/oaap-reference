"""The user list's filter (I-55): one rule for the portal and `oaap user list`.

Pure: records in, records out. The records are `identity.public_user`
dicts, already limited to what the caller may see -- this module never
widens a list, it only narrows one.
"""

STATES = {
    "active": "aktiv",
    "inactive": "deaktiviert",
    "scheduled": "mit Ablaufdatum",
    "inbox": "ohne Rolle (Eingang)",
    "password": "muss Passwort ändern",
}
KINDS = {"human": "Person", "machine": "Maschine"}
PAGE_SIZE = 100


def state_of(u):
    """Every state word that applies to one record (a list: a person can be
    active AND scheduled AND without a role)."""
    out = ["active" if u.get("active", True) else "inactive"]
    if u.get("active", True) and (u.get("deactivate_at") or u.get("delete_at")):
        out.append("scheduled")
    if not (u.get("roles") or []):
        out.append("inbox")
    if u.get("must_change_password"):
        out.append("password")
    return out


def normalise(args, tenants=()):
    """The filter state from request/CLI arguments -- unknown values dropped,
    so a link with a typo shows everything rather than nothing."""
    get = args.get
    f = {"q": str(get("q") or "").strip().lower()[:80],
         "tenant": str(get("tenant") or "").strip(),
         "state": str(get("state") or "").strip(),
         "role": str(get("role") or "").strip().lower(),
         "kind": str(get("kind") or "").strip()}
    if f["state"] not in STATES:
        f["state"] = ""
    if f["kind"] not in KINDS:
        f["kind"] = ""
    if f["tenant"] and tenants and f["tenant"] not in tenants:
        f["tenant"] = ""
    return f


def matches(u, f, tenant_of=lambda u: u.get("tenant") or ""):
    """Whether one record survives the filter state `f`.

    `q` looks in user name, display name and e-mail; `tenant` compares
    with `tenant_of(u)` (the caller decides whether that is an id or a
    label); `role` must be one of the record's roles.
    """
    if f.get("q"):
        hay = " ".join([u.get("username") or "", u.get("display_name") or "",
                        u.get("email") or ""]).lower()
        if f["q"] not in hay:
            return False
    if f.get("tenant") and tenant_of(u) != f["tenant"]:
        return False
    if f.get("state") and f["state"] not in state_of(u):
        return False
    if f.get("role") and f["role"] not in (u.get("roles") or []):
        return False
    if f.get("kind") and (u.get("kind") or "human") != f["kind"]:
        return False
    return True


def page_of(rows, page, size=PAGE_SIZE):
    """(rows on this page, page number actually shown, number of pages)."""
    pages = max(1, -(-len(rows) // size))
    try:
        p = int(page)
    except (TypeError, ValueError):
        p = 1
    p = min(max(1, p), pages)
    return rows[(p - 1) * size:p * size], p, pages
