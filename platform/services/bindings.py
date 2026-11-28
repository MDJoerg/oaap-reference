"""RFC-0143: people bound to an instance -- the rule in ONE file.

The host (`appctl`: the spool worker's `may()`, the notifications) and the
portal (which buttons to show, the page "Meine Instanzen") both need the
same answer to "what may this person do to this instance, and which of its
matters does this person hear?". Two copies of that answer would one day
disagree, and then the more generous one wins -- so it lives here, beside
`place.py`, and both import it. The host still judges every request again;
the portal only decides what to offer.

Operating roles only -- what a person may do TO an instance; business
rights inside the app stay `oaap.core.authorization` (RFC-0045 §1).
Pure functions: the caller passes the instances, the users and its
`resolve_tenant`.
"""

ROLES = ("observer", "caretaker", "owner")
TITLES = {"observer": "Beobachter", "caretaker": "Betreuer",
          "owner": "Verantwortlich"}
# The portal and the CLI accept the German names as well (D7)
ALIASES = {"beobachter": "observer", "betreuer": "caretaker",
           "verantwortlich": "owner"}

# What each role may do, as spool actions (§1). `site-check:run` is
# "Jetzt prüfen"; `site-check` without it switches the check on or off.
# `people:self` is a person switching their OWN notifications (§5, D4) --
# every bound person may, an observer too (to switch one on).
# Never by a binding: remove, install, rename, configuration, secrets,
# routes, visibility, addresses, domains -- and binding others (D3).
ACTIONS = {"observer": frozenset({"people:self"})}
ACTIONS["caretaker"] = ACTIONS["observer"] | frozenset({
    "maintenance", "login-required", "restart", "wake", "ai-fetchers",
    "site-check:run"})
ACTIONS["owner"] = ACTIONS["caretaker"] | frozenset({
    "promote", "rollback", "site-check", "site-watch"})

# §5 / D4: the matters about an instance its bound people hear by mail --
# Betreuer and Verantwortliche by default, Beobachter only if they switch
# one on; each person may switch their own off per type. The short-link
# types of RFC-0144 join here later.
NOTIFY_TYPES = ("instance.down", "site.google", "site.notfound", "site.check")
NOTIFY_TITLES = {"instance.down": "Instanz ausgefallen",
                 "site.google": "Google meldet ein Problem",
                 "site.notfound": "Viele „nicht gefunden“",
                 "site.check": "Seiten-Check"}
# The recipient's name in the notification log (the tenant's lists keep
# their own names; this one is no list anybody edits)
RECIPIENT = "Zuständige der Instanz"


def role(word):
    """`observer`/`caretaker`/`owner` from either language, or ''."""
    w = (word or "").strip().lower()
    w = ALIASES.get(w, w)
    return w if w in ROLES else ""


def action(name, req=None, actor=""):
    """The name `ACTIONS` judges for a spool request: the site check's
    "Jetzt prüfen" is a caretaker's, switching it is an owner's (§1); a
    person switching their own notifications is `people:self`."""
    req = req or {}
    if name == "site-check" and req.get("op") == "run":
        return "site-check:run"
    if (name == "instance-people" and req.get("op") == "notify" and actor
            and str(req.get("person") or "") == actor):
        return "people:self"
    return name


def people_of(inst):
    """The instance's bindings `{person, role, since, by[, notify]}`;
    anything else in the list is ignored, never guessed at."""
    return [p for p in ((inst or {}).get("people") or [])
            if isinstance(p, dict) and p.get("person") and p.get("role") in ROLES]


def rank(r):
    return ROLES.index(r) if r in ROLES else -1


def production_of(insts, key, resolve):
    """The production instance a test instance or a rehearsal belongs to
    (D2, stage 0 M2): a rehearsal names it (`rehearsal.of`), a test
    instance is named by the production instance it was promoted into
    (`source.promoted_from`). Same tenant only. (None, None) otherwise."""
    insts = insts or {}
    inst = insts.get(key) or {}
    tid = resolve(inst.get("tenant"))
    reh = inst.get("rehearsal") if isinstance(inst.get("rehearsal"), dict) else {}
    if reh.get("of"):
        p = insts.get(reh["of"])
        if p is not None and resolve(p.get("tenant")) == tid:
            return reh["of"], p
        return None, None
    if (inst.get("channel") or "production") != "test":
        return None, None
    for k, i in sorted(insts.items()):
        if (k != key and ((i.get("source") or {}).get("promoted_from") == key)
                and resolve(i.get("tenant")) == tid):
            return k, i
    return None, None


def binding_of(insts, key, person, resolve):
    """(binding, via): the person's strongest binding on the instance --
    its own, or the production instance's it belongs to (D2). The binding
    is the record's dict; ({}, '') if none."""
    best, via = {}, ""
    pkey, pinst = production_of(insts, key, resolve)
    for k, i in ((key, (insts or {}).get(key)), (pkey, pinst)):
        for p in people_of(i):
            if p["person"] == person and rank(p["role"]) > rank(best.get("role")):
                best, via = p, k
    return best, via


def _account(users, person):
    return next((u for u in users or [] if u.get("username") == (person or "")), None)


def judge(actor, key, act, insts, users, resolve):
    """RFC-0143 §4: may `actor` do `act` (see `action`) to the instance
    `key`? -> (yes, why). `why` is what the audit says: `role:
    server_admin`, `role: tenant_admin`, `bound: caretaker` -- or, for a
    no, the sentence. Judged from identity's store and the registry, never
    from a request: a disabled person, or one moved to another tenant,
    loses every binding at once, whatever the record still lists (§2)."""
    inst = (insts or {}).get(key)
    if inst is None:
        return False, "unknown instance"
    u = _account(users, actor)
    if not u or not u.get("active", True):
        return False, "no active account of this node"
    roles = set(u.get("roles") or [])
    if "server_admin" in roles:
        return True, "role: server_admin"
    if resolve(u.get("tenant")) != resolve(inst.get("tenant")):
        return False, "not a person of the instance's tenant"
    if "tenant_admin" in roles:
        return True, "role: tenant_admin"
    b, _via = binding_of(insts, key, actor, resolve)
    if not b:
        return False, "no binding on this instance"
    if act in ACTIONS[b["role"]]:
        return True, f"bound: {b['role']}"
    return False, f"a binding as {TITLES[b['role']]} does not allow '{act}'"


def hears(binding, ntype):
    """D4: does this binding hear `ntype` by mail? The person's own switch
    wins; without one, Betreuer and Verantwortliche hear, Beobachter not."""
    if ntype not in NOTIFY_TYPES:
        return False
    own = (binding.get("notify") or {}) if isinstance(binding.get("notify"), dict) else {}
    if isinstance(own.get(ntype), bool):
        return own[ntype]
    return binding.get("role") in ("caretaker", "owner")


def listeners(insts, key, ntype, users, resolve):
    """§5: who hears this matter about the instance `key` -> [(person,
    address, role)], each person once, by their strongest binding (D2:
    the production instance's people hear about its test instance too).
    Only active accounts of the instance's tenant with an address; a
    server_admin or tenant_admin bound as well hears it here too (their
    lists are the tenant's business, this is the binding's)."""
    inst = (insts or {}).get(key)
    if inst is None or ntype not in NOTIFY_TYPES:
        return []
    tid = resolve(inst.get("tenant"))
    pkey, pinst = production_of(insts, key, resolve)
    seen, out = set(), []
    for p in people_of(inst) + people_of(pinst):
        person = p["person"]
        if person in seen:
            continue
        seen.add(person)
        b, _via = binding_of(insts, key, person, resolve)
        u = _account(users, person)
        if (not b or not u or not u.get("active", True) or not u.get("email")
                or resolve(u.get("tenant")) != tid or not hears(b, ntype)):
            continue
        out.append((person, u["email"], b["role"]))
    return out


def mine(insts, person, resolve):
    """"Meine Instanzen": [(key, binding, via)] -- every instance the
    person is bound to, directly or through its production instance
    (D2), in key order."""
    out = []
    for k in sorted(insts or {}):
        b, via = binding_of(insts, k, person, resolve)
        if b:
            out.append((k, b, via))
    return out
