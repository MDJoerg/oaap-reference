"""Recipient lists and notifications (RFC-0080, oaap.core.notifications 0.1).

Pure functions only: the catalogue, the checks a list and a route must
pass, which lists hear a type, who a list resolves to, which matters open
and close, the hourly limits, and the words of a message. appctl keeps the
files, the outbox, the mailbox and the sending; the portal reads the same
catalogue from here, so the page and the host cannot disagree about which
types exist.
"""

import re
from datetime import datetime, timedelta, timezone

SCHEMA = "0.1"

CATEGORIES = ("critical", "health", "security", "operations")
CATEGORY_WORDS = {"critical": "Kritisch", "health": "Gesundheit",
                  "security": "Sicherheit", "operations": "Betrieb"}

# scope: "node" -- only the node hears it; "tenant" -- the tenant it is
# about hears it, and the node only when its routing names the type
# explicitly ("+ node" in the RFC); "both" -- node and tenants each hear it
# under their own routing (a node backup holds every tenant's data, a
# reboot stops every tenant's apps).
# matter: True -- a condition with a beginning and an end (open/close,
# critical daily); False -- an event, mailed once.
CATALOGUE = {
    "backup.failed": {"category": "critical", "scope": "both", "matter": True,
                      "title": "Backup fehlgeschlagen",
                      "subject": "[{knoten}] Backup fehlgeschlagen"},
    # "both" since RFC-0081: a tenant with its own archive can miss one too.
    "backup.missing": {"category": "critical", "scope": "both", "matter": True,
                       "title": "Kein erfolgreiches Backup seit über 26 Stunden",
                       "subject": "[{knoten}] Kein aktuelles Backup"},
    "disk.full": {"category": "health", "scope": "node", "matter": True,
                  "title": "Platte fast voll",
                  "subject": "[{knoten}] Platte {kurz}"},
    "advisory.host": {"category": "security", "scope": "node", "matter": True,
                      "title": "Sicherheitsupdates für den Server offen",
                      "subject": "[{knoten}] Sicherheitsupdates offen"},
    "advisory.image": {"category": "security", "scope": "tenant", "matter": True,
                       "title": "Sicherheitskorrekturen im Abbild einer Instanz offen",
                       "subject": "[{mandant}] Abbild von {instanz}: Sicherheitskorrekturen offen"},
    "reboot.planned": {"category": "operations", "scope": "both", "matter": False,
                       "title": "Neustart des Servers geplant",
                       "subject": "[{knoten}] Neustart geplant: {kurz}"},
    "reboot.started": {"category": "operations", "scope": "both", "matter": False,
                       "title": "Der Server startet jetzt neu",
                       "subject": "[{knoten}] Neustart läuft"},
    "reboot.cancelled": {"category": "operations", "scope": "both", "matter": False,
                         "title": "Geplanter Neustart abgesagt",
                         "subject": "[{knoten}] Neustart abgesagt"},
    "bulk.halted": {"category": "operations", "scope": "tenant", "matter": False,
                    "title": "Ein Lauf im Store wurde angehalten",
                    "subject": "[{mandant}] Lauf angehalten: {kurz}"},
    "rebuild.failed": {"category": "operations", "scope": "tenant", "matter": False,
                       "title": "Neubau einer Instanz fehlgeschlagen",
                       "subject": "[{mandant}] Neubau fehlgeschlagen: {instanz}"},
    "confirm.pending": {"category": "operations", "scope": "tenant", "matter": False,
                        "title": "Eine Auslieferung wartet auf Bestätigung",
                        "subject": "[{mandant}] Bestätigung nötig: {instanz}"},
    # Stage 2 (RFC-0080 §4.2)
    "instance.down": {"category": "health", "scope": "tenant", "matter": True,
                      "title": "Eine Produktiv-Instanz ist ausgefallen",
                      "subject": "[{mandant}] Instanz ausgefallen: {instanz}"},
    "login.locked": {"category": "security", "scope": "tenant", "matter": False,
                     "title": "Eine Anmeldung wurde nach Fehlversuchen gesperrt",
                     "subject": "[{mandant}] Anmeldung gesperrt: {kurz}"},
    # RFC-0084: an instance hands out a secret file when asked directly.
    # Critical: the gateway refuses the path, but the file is where any other
    # way in (a second proxy, a copy of the app) would serve it. Each scope
    # its own -- the tenant whose instance it is and the node's owners.
    "exposure.found": {"category": "critical", "scope": "both", "matter": True,
                       "title": "Eine Instanz gibt eine Geheimnisdatei heraus",
                       "subject": "[{knoten}] Geheimnisdatei abrufbar: {instanz}"},
    # RFC-0083: the weekly report itself. "both": the node's report goes to
    # the node's lists, a tenant's report to the tenant's -- each queued for
    # its own scope.
    "report.weekly": {"category": "operations", "scope": "both", "matter": False,
                      "title": "Wochenbericht",
                      "subject": "[{knoten}] Wochenbericht {kurz}"},
}

# RFC-0083 2.3: how a routing line delivers. "now" -- a mail at once (and,
# like every mail, a line in the weekly report); "report" -- no mail, only
# the report. (The RFC's "both" is "now": the report lists every mail.)
DELIVERIES = ("now", "report")
DELIVERY_WORDS = {"now": "sofort", "report": "nur im Wochenbericht"}
# Never only in the report: a failed backup that waits until Monday is the
# failure this platform exists to prevent. Enforced when a line is set AND
# when it is read (a category line set to "report" does not carry these).
NEVER_REPORT_ONLY = frozenset({"backup.failed", "backup.missing", "disk.full",
                               "instance.down", "report.weekly", "exposure.found"})


def delivery_for(scope, ntype):
    """"now" | "report" for one type in one scope (type line over category)."""
    if ntype in NEVER_REPORT_ONLY:
        return "now"
    routes = scope.get("routes") or {}
    t_line = routes.get(ntype) or {}
    c_line = routes.get(CATALOGUE.get(ntype, {}).get("category", "")) or {}
    d = t_line.get("delivery") or c_line.get("delivery") or "now"
    return d if d in DELIVERIES else "now"

# Own RFC: named so the page can say it exists and waits.
LATER = {
    "intrusion.suspected": "eigenes RFC: heute erkennt nichts einen Einbruch",
}

# {ort}: the node's place in the fleet (RFC-0082), e.g. de/datacenter1/group3
PLACEHOLDERS = ("knoten", "mandant", "instanz", "typ", "kurz", "ort")
ROLES = {"server_admin": "Server-Admins", "tenant_admin": "Mandanten-Admins"}
FIELDS = ("to", "cc", "bcc")
FIELD_WORDS = {"to": "An", "cc": "Cc", "bcc": "Bcc"}
CHANNELS = ("mail",)

TEST_SUBJECT = "[{knoten}] Probe des Verteilers {kurz}"

NODE_MANDATORY = "Serverbesitzer"
TENANT_MANDATORY = "Mandantenverwaltung"

MAX_RECIPIENTS = 25            # D9
TENANT_PER_HOUR = 20           # D9
NODE_PER_HOUR = 60             # the operator's own lists; a ceiling, not a policy
TESTS_PER_HOUR = 5             # D9
REMIND_HOURS = 24              # D6: critical again every day while open
RETRY_SECONDS = 3600           # an unsent notification is tried for an hour
LOG_KEEP = 200
DISK_WARN, DISK_CRIT = 90.0, 95.0     # D7
BACKUP_STALE_HOURS = 26
INSTANCE_DOWN_MINUTES = 5      # RFC-0080 §4.2: unhealthy longer than this
DOWN_SEEN_GAP = 180            # an observation older than this restarts the clock
# identity's own numbers (services/identity: _LOCK_THRESHOLD, _LOCK_WINDOW);
# test_notify reads them there, so the two cannot drift apart unnoticed.
LOCK_THRESHOLD, LOCK_WINDOW = 5, 300
LOCK_REPORT_HOURS = 1          # one mail per name and hour, however long it is hammered

LIST_NAME_RE = r"[A-Za-zÄÖÜäöüß0-9][A-Za-zÄÖÜäöüß0-9 ._-]{0,39}"
ADDR_RE = r"[^@\s<>\"',;]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"


def mandatory_name(is_node):
    return NODE_MANDATORY if is_node else TENANT_MANDATORY


def default_scope(is_node):
    """A scope nobody configured: the mandatory list with the admin role."""
    role = "server_admin" if is_node else "tenant_admin"
    return {"lists": {mandatory_name(is_node): {
        "entries": [{"channel": "mail", "to": "to", "role": role}],
        "subject_prefix": "", "mandatory": True}},
        "routes": {}}


def scope_of(stored, is_node):
    """The stored scope completed: the mandatory list always exists."""
    sc = {"lists": dict((stored or {}).get("lists") or {}),
          "routes": dict((stored or {}).get("routes") or {})}
    name = mandatory_name(is_node)
    if name not in sc["lists"]:
        sc["lists"][name] = default_scope(is_node)["lists"][name]
    sc["lists"][name]["mandatory"] = True
    return sc


def types_for(is_node):
    """The types a scope can route, in catalogue order."""
    out = []
    for t, d in CATALOGUE.items():
        if is_node or d["scope"] in ("tenant", "both"):
            out.append(t)
    return out


# --------------------------------------------------------------- checks

def entry_refusal(e, is_node):
    if not isinstance(e, dict):
        return "an entry is an object"
    if e.get("channel", "mail") not in CHANNELS:
        return f"channel '{e.get('channel')}' is not built yet (only mail)"
    if e.get("to") not in FIELDS:
        return "an entry goes to To, Cc or Bcc"
    has_a, has_r = bool(e.get("address")), bool(e.get("role"))
    if has_a == has_r:
        return "an entry is an address or a role, exactly one of them"
    if has_a and not re.fullmatch(ADDR_RE, e["address"]):
        return f"'{e['address']}' is not an e-mail address"
    if has_r:
        want = "server_admin" if is_node else "tenant_admin"
        if e["role"] != want:
            return (f"a {'node' if is_node else 'tenant'}'s list names the role "
                    f"{want} only")
    return ""


def list_refusal(name, lst, is_node):
    """Why this list cannot be kept, or ""."""
    if not re.fullmatch(LIST_NAME_RE, name or ""):
        return ("a list's name is 1-40 letters, digits, spaces, '.', '_' or '-', "
                "starting with a letter or digit")
    entries = (lst or {}).get("entries")
    if not isinstance(entries, list) or not entries:
        return "a list has at least one entry"
    addrs = [e.get("address", "").lower() for e in entries if isinstance(e, dict)]
    if len([a for a in addrs if a]) > MAX_RECIPIENTS:
        return f"a list holds at most {MAX_RECIPIENTS} addresses"
    for e in entries:
        bad = entry_refusal(e, is_node)
        if bad:
            return bad
    if not any(e.get("to") == "to" for e in entries):
        return "a list needs at least one To entry"
    seen = set()
    for e in entries:
        k = e.get("address", "").lower() or "role:" + e.get("role", "")
        if k in seen:
            return f"'{e.get('address') or e.get('role')}' stands twice in the list"
        seen.add(k)
    pre = (lst or {}).get("subject_prefix") or ""
    if len(pre) > 40 or any(ord(c) < 32 for c in pre):
        return "the subject prefix is at most 40 printable characters"
    return ""


def template_refusal(tpl):
    if tpl is None or tpl == "":
        return ""
    if len(tpl) > 160 or any(ord(c) < 32 for c in tpl):
        return "a subject is at most 160 printable characters"
    for name in re.findall(r"\{([^{}]*)\}", tpl):
        if name not in PLACEHOLDERS:
            return ("unknown placeholder {" + name + "} -- known: "
                    + ", ".join("{" + p + "}" for p in PLACEHOLDERS))
    if re.sub(r"\{[a-z]+\}", "", tpl).count("{") or re.sub(r"\{[a-z]+\}", "", tpl).count("}"):
        return "a brace without a placeholder"
    return ""


def route_refusal(key, route, scope, is_node):
    """Why this routing line cannot be kept, or ""."""
    if key not in CATEGORIES and key not in types_for(is_node):
        return f"'{key}' is neither a category nor a type this scope hears"
    names = (route or {}).get("lists") or []
    if not isinstance(names, list):
        return "a route names lists"
    for n in names:
        if n not in scope["lists"]:
            return f"there is no list '{n}'"
    d = (route or {}).get("delivery") or "now"
    if d not in DELIVERIES:
        return "delivery is one of: " + ", ".join(DELIVERIES)
    if d == "report" and (key == "critical" or key in NEVER_REPORT_ONLY):
        return (f"'{key}' is always mailed at once -- a critical matter never waits for "
                "the weekly report")
    return template_refusal((route or {}).get("subject") or "")


# --------------------------------------------------------------- routing

def route_for(scope, ntype, is_node, from_tenant=False):
    """-> (list names, subject template). The type's line wins over its
    category's; nothing routed goes to the mandatory list. At the node a
    tenant's event is heard only through an explicit type line."""
    d = CATALOGUE[ntype]
    routes = scope.get("routes") or {}
    t_line = routes.get(ntype) or {}
    if from_tenant:
        names = [n for n in t_line.get("lists") or [] if n in scope["lists"]]
        return names, (t_line.get("subject") or d["subject"])
    c_line = routes.get(d["category"]) or {}
    names = [n for n in (t_line.get("lists") or c_line.get("lists") or [])
             if n in scope["lists"]]
    subject = t_line.get("subject") or c_line.get("subject") or d["subject"]
    if not names:
        names = [mandatory_name(is_node)]
    return names, subject


def expand(tpl, values):
    return re.sub(r"\{([a-z]+)\}",
                  lambda m: str(values.get(m.group(1), "")) if m.group(1) in PLACEHOLDERS
                  else m.group(0), tpl or "").strip()


def resolve(lists, scope, people_of_role):
    """-> {"to": [...], "cc": [...], "bcc": [...]}: every address once, in
    its strongest field (To over Cc over Bcc), at most MAX_RECIPIENTS.
    `people_of_role(role)` returns the addresses that hold it now."""
    rank = {"to": 0, "cc": 1, "bcc": 2}
    best = {}
    order = []
    for name in lists:
        for e in (scope["lists"].get(name) or {}).get("entries") or []:
            if e.get("channel", "mail") != "mail":
                continue
            addrs = ([e["address"]] if e.get("address")
                     else list(people_of_role(e.get("role", "")) or []))
            for a in addrs:
                k = a.strip().lower()
                if not k:
                    continue
                if k not in best:
                    order.append(k)
                    best[k] = (e["to"], a.strip())
                elif rank[e["to"]] < rank[best[k][0]]:
                    best[k] = (e["to"], best[k][1])
    out = {"to": [], "cc": [], "bcc": []}
    for k in order[:MAX_RECIPIENTS]:
        f, a = best[k]
        out[f].append(a)
    return out


def subject_with_prefix(lists, scope, subject):
    pre = ""
    for n in lists:
        pre = (scope["lists"].get(n) or {}).get("subject_prefix") or ""
        if pre:
            break
    return (pre.strip() + " " + subject).strip() if pre else subject


# --------------------------------------------------------------- matters

def _parse(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def matter_ops(current, stored, now):
    """current: {key: {"type", "kurz", "text", ...}} -- the conditions true
    now in one scope; stored: {key: {"type", "opened", "last_sent", ...}}.
    -> (ops, new_stored); ops are ("open"|"remind"|"close", key, record)."""
    ops, new = [], {}
    for key, c in sorted(current.items()):
        old = stored.get(key)
        if not old:
            rec = dict(c, opened=now.isoformat(), last_sent=now.isoformat())
            ops.append(("open", key, rec))
            new[key] = rec
            continue
        # The short word stays the opening one: "Behoben: Platte zu 96 %
        # voll" names the matter that ended, not the last minute's reading.
        rec = dict(old, **{k: v for k, v in c.items() if k != "kurz"})
        last = _parse(old.get("last_sent")) or now
        if (CATALOGUE.get(c["type"], {}).get("category") == "critical"
                and now - last >= timedelta(hours=REMIND_HOURS)):
            rec["last_sent"] = now.isoformat()
            ops.append(("remind", key, rec))
        new[key] = rec
    for key, old in sorted(stored.items()):
        if key not in current:
            ops.append(("close", key, old))
    return ops, new


def within_limit(stamps, now, per_hour):
    """-> (allowed, kept stamps) for one more sending."""
    cut = now - timedelta(hours=1)
    kept = [s for s in stamps or [] if (_parse(s) or cut) > cut]
    return len(kept) < per_hour, kept


# ---------------------------------------------------------- stage 2 facts

def clean(text, n=64):
    """A word that came from outside (a login name someone typed) as it
    may stand in a subject: no control characters, bounded."""
    return "".join(ch for ch in str(text or "") if ch.isprintable()).strip()[:n]


def service_trouble(row):
    """One service row of apps/instance-state.json -> what is wrong, or ""."""
    st = row.get("state") or "absent"
    if st == "absent":
        return "Container fehlt"
    if st != "running":
        why = f"Container {st}"
        if row.get("oom"):
            why += " (Speicher erschöpft)"
        elif row.get("exit_code"):
            why += f", Exit-Code {row['exit_code']}"
        return why
    if row.get("health") == "unhealthy":
        return "Gesundheitsprüfung schlägt fehl"
    return ""


def instance_trouble(entry):
    """An instance's entry of the state view -> what is wrong, or "".
    Standby, waking and maintenance are someone's decision, not a fault."""
    if (entry or {}).get("mode", "running") != "running" or (entry or {}).get("maintenance"):
        return ""
    rows = (entry or {}).get("services") or []
    bad = [(r.get("service") or "", service_trouble(r)) for r in rows]
    bad = [(s, w) for s, w in bad if w]
    if not bad:
        return ""
    if len(rows) == 1:
        return bad[0][1]
    return "; ".join(f"{s or 'Hauptdienst'}: {w}" for s, w in bad)


def down_conditions(view, production, since, now):
    """view: {name: state entry}; production: {name: tenant id} -- the
    production instances; since: {name: {"since", "seen"}} from the last
    minute. -> ({name: (tid, condition)} for those down longer than
    INSTANCE_DOWN_MINUTES, new since). An instance whose last observation
    is older than DOWN_SEEN_GAP starts its clock again: the minute job
    paused (a backup), so the earlier "down" says nothing about now."""
    out, new = {}, {}
    for name, tid in sorted(production.items()):
        why = instance_trouble(view.get(name))
        if name not in view or not why:
            continue
        old = since.get(name) or {}
        seen = _parse(old.get("seen"))
        start = _parse(old.get("since"))
        if not start or not seen or (now - seen).total_seconds() > DOWN_SEEN_GAP:
            start = now
        new[name] = {"since": start.isoformat(), "seen": now.isoformat()}
        if now - start >= timedelta(minutes=INSTANCE_DOWN_MINUTES):
            out[name] = (tid, {"type": "instance.down", "instanz": name, "kurz": "",
                               "text": f"Die Instanz „{name}“ ist seit "
                                       f"{start.strftime('%H:%M')} UTC nicht gesund: {why}."})
    return out, new


def lock_subject(key):
    """A key of identity's login-throttle.json -> (kind, client, name).
    kind: login (client|name), key (key|<id>), keyclient (keyclient|client)."""
    head, _sep, rest = str(key).partition("|")
    if head == "key":
        return "key", "", clean(rest)
    if head == "keyclient":
        return "keyclient", clean(rest), ""
    return "login", clean(head), clean(rest)


def lock_episodes(table, now_epoch, reported):
    """table: identity's {key: [epoch, ...]}; reported: {key: epoch}.
    -> ([(key, failures)], new reported). A key counts as locked when it
    has LOCK_THRESHOLD failures within LOCK_WINDOW -- identity's own rule --
    and is reported once per LOCK_REPORT_HOURS."""
    keep = {k: t for k, t in (reported or {}).items()
            if isinstance(t, (int, float)) and now_epoch - t < LOCK_REPORT_HOURS * 3600}
    out = []
    for key, hits in sorted((table or {}).items()):
        if not isinstance(hits, list):
            continue
        recent = [t for t in hits if isinstance(t, (int, float)) and 0 <= now_epoch - t < LOCK_WINDOW]
        if len(recent) >= LOCK_THRESHOLD and key not in keep:
            out.append((key, len(recent)))
            keep[key] = now_epoch
    return out, keep


# --------------------------------------------------------------- words

def body(ntype, op, values, text, link=""):
    """The plain German body. op: event | open | remind | close | test."""
    d = CATALOGUE.get(ntype) or {"title": ntype}
    if op == "test":
        lines = [f"Probe des Verteilers „{values.get('kurz', '')}“", ""]
    else:
        head = {"close": "Behoben: ", "remind": "Besteht weiterhin: "}.get(op, "")
        lines = [head + d["title"], ""]
    if values.get("mandant"):
        lines.append(f"Mandant: {values['mandant']}")
    server = values.get("knoten", "")
    if values.get("extern") and values["extern"] != server:
        server += f" ({values['extern']})"     # RFC-0082 D4: both names
    lines.append(f"Server: {server}")
    if values.get("ort"):
        lines.append(f"Ort: {values['ort']}")
    if values.get("instanz"):
        lines.append(f"Instanz: {values['instanz']}")
    if text:
        lines += ["", "Zuvor gemeldet:" if op == "close" else "", text]
        if not lines[-2]:
            del lines[-2]
    if op == "remind":
        lines += ["", "Diese Erinnerung kommt einmal täglich, bis die Lage behoben ist."]
    if op == "close":
        lines += ["", "Die Lage ist nicht mehr gegeben. Es folgt keine weitere Mail dazu."]
    if link:
        lines += ["", f"Im Portal: {link}"]
    lines += ["", "--",
              "Diese Mail verschickt die OAAP-Plattform (Benachrichtigungen, RFC-0080).",
              "Wer sie bekommt, steht im Portal unter Nachrichten → Verteiler."]
    return "\n".join(lines) + "\n"


def subject_for(op, subject):
    return {"close": "Behoben: ", "remind": "Erinnerung: "}.get(op, "") + subject


def now_utc():
    return datetime.now(timezone.utc).replace(microsecond=0)
