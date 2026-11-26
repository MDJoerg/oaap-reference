"""The portal's page „Tore“ (RFC-0135 stage 2) -- the pure part.

What the page shows of one tenant's gates: address, process and whether its
`vorgaenge` instance is there, the accesses with expiry and last use, and a
light per gate (§7). Plain functions over the files the portal already reads
(gates.json, the registry, the keys identity lets this caller see), so they
are tested without a running portal. Changing anything is the host's
(appctl.gate_job); this module decides nothing.
"""
from datetime import datetime, timedelta, timezone

CASES_APP_ID = "vorgaenge"        # appctl.CASES_APP_ID
SCOPE_PREFIX = "oaap.gate:"       # gates.SCOPE_PREFIX
WARN_DAYS = 14                    # §7: yellow when a key runs out within 14 days
CASE_KINDS = (("internal", "intern"), ("question", "Frage"), ("bug", "Fehler"),
              ("idea", "Idee"), ("change", "Änderung"))


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def case_target(instances, tid, proc, resolve):
    """The registry key of the `vorgaenge` instance a process delivers to, or
    "" -- the same rule as appctl.gate_target: same tenant, never a rehearsal,
    the named instance or else the tenant's production inbox."""
    if not proc or proc.get("kind", "case") != "case":
        return ""
    cands = [(k, i) for k, i in sorted((instances or {}).items())
             if i.get("app_id") == CASES_APP_ID and not i.get("rehearsal")
             and resolve(i.get("tenant")) == tid]
    if proc.get("instance"):
        cands = [c for c in cands if c[0] == proc["instance"]]
    else:
        cands = [c for c in cands if c[1].get("channel") == "production"]
    return cands[0][0] if cands else ""


def access_rows(keys, tid, name, now=None):
    """The accesses of one gate, from the keys this caller may see: the keys
    limited to `oaap.gate:<tenant>/<name>`. Newest first; a revoked or expired
    key stays in the list (what happened stays readable)."""
    now = now or datetime.now(timezone.utc)
    soon = _iso(now + timedelta(days=WARN_DAYS))
    stamp = _iso(now)
    scope = f"{SCOPE_PREFIX}{tid}/{name}"
    rows = []
    for k in keys or []:
        if k.get("instance") != scope:
            continue
        expired = bool(k.get("expires")) and k["expires"] <= stamp
        state = ("revoked" if k.get("revoked") else "expired" if expired
                 else "soon" if (k.get("expires") or "") <= soon else "valid")
        rows.append({"id": k.get("id", ""), "principal": k.get("principal", ""),
                     "label": k.get("label", ""), "expires": (k.get("expires") or "")[:10],
                     "last_used": (k.get("last_used") or "")[:16].replace("T", " "),
                     "state": state})
    rows.sort(key=lambda r: (r["state"] in ("revoked", "expired"), r["principal"]))
    return rows


def light(target, accesses):
    """(colour, sentence) of one gate (§7). Red: the target is gone or no
    access is valid. Yellow: a key runs out within 14 days. Green otherwise.
    Refusals and the usual rhythm come with the gate's own counts (stage 2b)."""
    live = [a for a in accesses if a["state"] in ("valid", "soon")]
    if not target:
        return "red", "kein Ziel: keine Vorgänge-Instanz für diesen Prozess"
    if not live:
        return "red", "kein gültiger Zugang"
    if any(a["state"] == "soon" for a in live):
        return "yellow", f"ein Schlüssel läuft in weniger als {WARN_DAYS} Tagen ab"
    return "green", "bereit"


def last_use(accesses):
    """The latest use of any access ('' when never) -- a ping counts as a use."""
    used = sorted((a["last_used"] for a in accesses if a["last_used"]), reverse=True)
    return used[0] if used else ""


def tenant_view(doc, tid, instances, keys, resolve, url_of, now=None):
    """Everything the page shows of one tenant: processes and gates."""
    doc = doc or {}
    procs = {p["name"]: p for p in (doc.get("processes") or {}).values()
             if p.get("tenant") == tid}
    proc_rows = []
    for name, p in sorted(procs.items()):
        target = case_target(instances, tid, p, resolve)
        proc_rows.append({"name": name, "queue": p.get("queue") or "eingang",
                          "kind": p.get("case_kind") or "internal",
                          "instance": p.get("instance") or "", "label": p.get("label") or "",
                          "target": target, "gates": []})
    by_proc = {r["name"]: r for r in proc_rows}
    gate_rows = []
    for g in sorted((g for g in (doc.get("gates") or {}).values() if g.get("tenant") == tid),
                    key=lambda g: g["name"]):
        proc = procs.get(g.get("process") or "")
        target = case_target(instances, tid, proc, resolve)
        acc = access_rows(keys, tid, g["name"], now)
        colour, why = light(target, acc)
        row = {"name": g["name"], "user": g.get("user") or g["name"],
               "process": g.get("process") or "", "process_missing": proc is None,
               "target": target, "url": url_of(g["name"]),
               "fields": [f.get("name", "") + ("*" if f.get("required") else "")
                          for f in g.get("fields") or []],
               "accesses": acc, "light": colour, "why": why, "last_use": last_use(acc)}
        gate_rows.append(row)
        if g.get("process") in by_proc:
            by_proc[g["process"]]["gates"].append(g["name"])
    return {"processes": proc_rows, "gates": gate_rows}


def sm59(url, user):
    """The SM59 (type G) parameters for an access, from the gate's address."""
    host, _, path = (url[len("https://"):] if url.startswith("https://") else "").partition("/")
    return {"host": host, "port": "443", "path": "/" + path if path else "", "user": user}
