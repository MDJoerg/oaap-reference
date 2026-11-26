"""The portal's page „Tore“ (RFC-0135 stage 2) -- the pure part.

What the page shows of one tenant's gates: address, process and whether its
`vorgaenge` instance is there, the accesses with expiry and last use, and a
light per gate (§7). Plain functions over the files the portal already reads
(gates.json, the registry, the keys identity lets this caller see), so they
are tested without a running portal. Changing anything is the host's
(appctl.gate_job); this module decides nothing.
"""
from datetime import datetime, timedelta, timezone

import gate_stats  # the counts (stage 2b); beside this file in the image

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


def light(target, accesses, counts=None, now=None):
    """(colour, sentence) of one gate (§7). Red: the target is gone or no
    access is valid. Yellow: a key runs out within 14 days, refusals today,
    or a gate used regularly is unusually quiet (stage 2b, gate_stats).
    Green otherwise."""
    live = [a for a in accesses if a["state"] in ("valid", "soon")]
    if not target:
        return "red", "kein Ziel: keine Vorgänge-Instanz für diesen Prozess"
    if not live:
        return "red", "kein gültiger Zugang"
    if any(a["state"] == "soon" for a in live):
        return "yellow", f"ein Schlüssel läuft in weniger als {WARN_DAYS} Tagen ab"
    if counts:
        n = counts["today"]["refused_n"]
        if n:
            return "yellow", f"heute {n} abgelehnt"
        quiet = gate_stats.unusually_quiet(counts, now)
        if quiet:
            return "yellow", quiet
        if counts["last_delivery"]:
            return "green", "in Gebrauch"
    return "green", "bereit"


def last_use(accesses):
    """The latest use of any access ('' when never) -- a ping counts as a use."""
    used = sorted((a["last_used"] for a in accesses if a["last_used"]), reverse=True)
    return used[0] if used else ""


def counts_row(c):
    """The page's line of a gate's counts (stage 2b)."""
    def refused(r):
        return ", ".join(f"{gate_stats.REASON_TITLES.get(k, k)} {n}"
                         for k, n in sorted(r.items(), key=lambda x: -x[1]))
    t, w = c["today"], c["window"]
    return {"today": f"{t['delivered']} angenommen, {t['refused_n']} abgelehnt"
                     + (f" ({refused(t['refused'])})" if t["refused_n"] else "")
                     + f", {t['ping']} Ping",
            "window": f"{w['delivered']} angenommen, {w['refused_n']} abgelehnt"
                      + (f" ({refused(w['refused'])})" if w["refused_n"] else "")
                      + f", {w['ping']} Ping",
            "last_delivery": c["last_delivery"], "last_ping": c["last_ping"],
            "last_refusal": c["last_refusal"] + (
                f" ({gate_stats.REASON_TITLES.get(c['last_refusal_reason'], '')})"
                if c["last_refusal"] and c["last_refusal_reason"] else "")}


def _when(ts):
    return (ts or "")[:16].replace("T", " ")


KIND_TITLES = {"gate-access": "Zugang zu einem Tor"}
VIA_TITLES = {"code": "Programm (Code)", "link": "Mensch (Link)"}


def coupling_rows(couplings, tid, gate=None):
    """The coupling tickets of one tenant (RFC-0136 §5), newest first, as
    the pages show them; identity already said which the caller may see."""
    rows = []
    for c in couplings or []:
        if c.get("tenant") != tid or (gate is not None and c.get("gate") != gate):
            continue
        st = c.get("state") or "open"
        rows.append({"id": c.get("id", ""), "kind_title": KIND_TITLES.get(c.get("kind"), c.get("kind")),
                     "gate": c.get("gate", ""), "label": c.get("label", ""),
                     "issuer": c.get("issuer", ""), "issued": _when(c.get("issued")),
                     "until": _when(c.get("until")), "state": st,
                     "state_title": STATE_TITLES.get(st, st),
                     "redeemed": _when(c.get("redeemed")),
                     "redeemed_from": c.get("redeemed_from", ""),
                     "via_title": VIA_TITLES.get(c.get("via"), c.get("via") or ""),
                     "key_id": c.get("key_id", ""), "principal": c.get("principal", ""),
                     "ended": _when(c.get("withdrawn") or c.get("until"))})
    rows.sort(key=lambda r: r["issued"], reverse=True)
    return rows


STATE_TITLES = {"open": "wartet auf Einlösung", "redeemed": "eingelöst",
                "expired": "abgelaufen", "withdrawn": "zurückgezogen"}  # coupling.STATE_TITLES


def coupling_view(couplings, tid):
    rows = coupling_rows(couplings, tid)
    return {"open": [r for r in rows if r["state"] == "open"],
            "redeemed": [r for r in rows if r["state"] == "redeemed"],
            "ended": [r for r in rows if r["state"] in ("expired", "withdrawn")]}


def tenant_view(doc, tid, instances, keys, resolve, url_of, now=None, stats=None,
                couplings=None):
    """Everything the page shows of one tenant: processes and gates.
    `stats` is gate-stats.json (stage 2b) or None; `couplings` the tickets
    identity lets the caller see (RFC-0136) -- an open one is an access
    „wartet auf Einlösung“."""
    doc = doc or {}
    sg = (stats or {}).get("gates") if isinstance((stats or {}).get("gates"), dict) else {}
    epoch = now.timestamp() if isinstance(now, datetime) else None
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
        counts = gate_stats.summary(sg.get(f"{tid}/{g['name']}"), epoch)
        colour, why = light(target, acc, counts, epoch)
        pending = [c for c in coupling_rows(couplings, tid, g["name"]) if c["state"] == "open"]
        if pending and why == "kein gültiger Zugang":
            why += " — eine Kopplung wartet auf Einlösung"
        row = {"name": g["name"], "user": g.get("user") or g["name"],
               "process": g.get("process") or "", "process_missing": proc is None,
               "target": target, "url": url_of(g["name"]),
               "fields": [f.get("name", "") + ("*" if f.get("required") else "")
                          for f in g.get("fields") or []],
               "field_rows": field_rows(g.get("fields") or []),
               "accesses": acc, "light": colour, "why": why, "last_use": last_use(acc),
               "counts": counts_row(counts), "pending": pending}
        gate_rows.append(row)
        if g.get("process") in by_proc:
            by_proc[g["process"]]["gates"].append(g["name"])
    return {"processes": proc_rows, "gates": gate_rows}


FIELD_TYPES = (("text", "Text"), ("number", "Zahl"), ("timestamp", "Zeitpunkt (UTC)"),
               ("date", "Datum"), ("yesno", "Ja/Nein"), ("choice", "Auswahl"))
FIELD_TARGETS = (("ref", "Referenz"), ("title", "Titel"), ("text", "Text"),
                 ("tech", "technisches Feld"))
BLANK_ROWS = 2


def field_rows(fields):
    """The rows of the field editor (stage 2b): the gate's fields as the
    form shows them, then BLANK_ROWS empty ones to add a field."""
    rows = []
    for f in fields:
        rows.append({"name": f.get("name", ""), "type": f.get("type", "text"),
                     "max": f.get("max", "") if f.get("type", "text") == "text" else "",
                     "required": bool(f.get("required")), "to": f.get("to", "tech"),
                     "choices": ", ".join(f.get("choices") or []),
                     "description": f.get("description", ""), "sample": f.get("sample", "")})
    rows += [{"name": "", "type": "text", "max": "", "required": False, "to": "tech",
              "choices": "", "description": "", "sample": ""} for _ in range(BLANK_ROWS)]
    return rows


def fields_from_form(get, n):
    """The field list from the editor's form (`get(key)` -> str or None)
    -> list for gates.check_fields, which the host runs. Empty rows and
    rows ticked "entfernen" fall out; nothing else is judged here."""
    out = []
    for i in range(min(int(n), 60)):
        name = (get(f"f{i}_name") or "").strip().lower()
        if not name or get(f"f{i}_drop"):
            continue
        f = {"name": name[:40], "type": (get(f"f{i}_type") or "text").strip()[:12],
             "required": bool(get(f"f{i}_required")), "to": (get(f"f{i}_to") or "tech")[:8],
             "description": (get(f"f{i}_description") or "").strip()[:200]}
        mx = (get(f"f{i}_max") or "").strip()
        if mx and f["type"] == "text":
            f["max"] = mx[:6]
        ch = [c.strip() for c in (get(f"f{i}_choices") or "").split(",") if c.strip()]
        if ch:
            f["choices"] = ch[:30]
        sm = (get(f"f{i}_sample") or "").strip()
        if sm:
            f["sample"] = sm[:200]
        out.append(f)
    return out


def sm59(url, user):
    """The SM59 (type G) parameters for an access, from the gate's address."""
    host, _, path = (url[len("https://"):] if url.startswith("https://") else "").partition("/")
    return {"host": host, "port": "443", "path": "/" + path if path else "", "user": user}
