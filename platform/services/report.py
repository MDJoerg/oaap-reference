"""The weekly report (RFC-0083): the daily ledger's reductions, the report's
Markdown, and a small Markdown renderer for the mail and the portal.

Pure: every function takes what it reads. The acting (the minute job, the
files, the outbox) is in appctl.py (`ledger_minute`, `report_minute`).

Why a ledger at all: the sources forget faster than a week -- node metrics
end at 31 days, the brake's counts at 24 hours, the backup keeps only its
last run. One reduced line per scope and day, kept three years, is the only
store a weekly (and a yearly) picture can be drawn from.
"""
import html
import json
import os
import re
import time

LEDGER_KEEP_DAYS = 3 * 366
LEDGER_EVENTS_MAX = 100
LEDGER_MAILS_MAX = 200
REPORT_KEEP_DAYS = 366

# The tenant-log actions a report names (prefixes). Everything else in the
# log (keys, IdP plumbing, egress decisions ...) is detail for the log page.
EVENT_PREFIXES = ("reboot.", "backup.", "node.identity", "tenant.create", "tenant.remove",
                  "tenant.rename", "tenant.adopted", "instance.rename", "instance.purge",
                  "instance.rebuild", "instance.sideload", "instance.restarted",
                  "disk.", "rehearsal.create", "rehearsal.expire")

EVENT_WORDS = {
    "reboot.planned": "Neustart geplant", "reboot.started": "Neustart",
    "reboot.done": "Zurück nach Neustart", "reboot.cancelled": "Neustart abgesagt",
    "backup.class": "Backup-Art des Knotens geändert", "backup.tenant": "Backup-Art eines Mandanten",
    "backup.excluded": "Mandant aus dem Backup genommen",
    "backup.included": "Mandant wieder im Backup", "node.identity": "Name im Verbund geändert",
    "tenant.create": "Mandant angelegt", "tenant.remove": "Mandant entfernt",
    "tenant.rename": "Mandant umbenannt", "tenant.adopted": "Mandant übernommen",
    "instance.rename": "Instanz umbenannt", "instance.purge": "Instanz gelöscht",
    "instance.rebuild": "Instanz neu gebaut", "instance.sideload": "Instanz von Hand eingespielt",
    "instance.restarted": "Instanz neu gestartet", "rehearsal.create": "Generalprobe angelegt",
    "rehearsal.expire": "Generalprobe abgelaufen",
}

SERIES_WORDS = {"cpu": ("CPU", "%"), "mem": ("Speicher", "%"), "disk": ("Platte", "%"),
                "req": ("Anfragen", "/min"), "lat": ("Antwortzeit", "ms"),
                "conn": ("Verbindungen", "")}

DAY_NAMES = ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")


# ------------------------------------------------------------------ ledger

def is_event(action):
    return any(action == p or action.startswith(p) for p in EVENT_PREFIXES)


def day_events(log_entries, start, end):
    """Tenant-log entries of one scope in [start, end) (epoch) -> short rows."""
    out = []
    for e in log_entries:
        t = _epoch(e.get("when"))
        if t is None or not (start <= t < end):
            continue
        a = str(e.get("action") or "")
        if not is_event(a):
            continue
        out.append({"t": e.get("when"), "a": a,
                    "s": str(e.get("subject") or "")[:80],
                    "r": str(e.get("result") or "ok")[:20],
                    "d": str(e.get("detail") or "")[:200]})
    out.sort(key=lambda x: _epoch(x["t"]) or 0)
    return out[-LEDGER_EVENTS_MAX:]


def day_mails(log_lines, scope, start, end):
    """notify-log lines of one scope in [start, end) -> {t, type, op, r}."""
    out = []
    for e in log_lines:
        if e.get("tenant") != scope:
            continue
        t = _epoch(e.get("when"))
        if t is None or not (start <= t < end):
            continue
        out.append({"t": e.get("when"), "type": e.get("type", ""), "op": e.get("op", ""),
                    "r": e.get("result", "")})
    return out[-LEDGER_MAILS_MAX:]


def day_metrics(entries, start, end, aggregate):
    """Rolled-up metric entries (30 min tier) in [start, end) -> {series:
    [n, mean, min, max]}. `aggregate` is metrics._aggregate (one rule)."""
    rows = [e for e in entries if start <= e.get("t", -1) < end]
    out = {}
    for s in SERIES_WORDS:
        a = aggregate(rows, s)
        if a:
            out[s] = a
    return out


def braked_bucket(v):
    """The identity's bucket format (identity/app.py _braked_bucket): a dict
    since RFC-0083, a bare count before. Read here, never written."""
    if isinstance(v, dict):
        return {"n": int(v.get("n") or 0), "c": list(v.get("c") or []),
                "m": {str(k): int(x) for k, x in (v.get("m") or {}).items()},
                "full": bool(v.get("full"))}
    return {"n": int(v or 0), "c": [], "m": {}, "full": False}


def day_brake(data, start, end):
    """{instance: {count, clients, clients_more, minutes, peak_count,
    peak_at}} over the hour buckets inside [start, end)."""
    out = {}
    for scope, buckets in (data or {}).items():
        tags, more, minutes, n, peak = set(), False, 0, 0, (0, 0)
        for h, v in (buckets or {}).items():
            if not str(h).isdigit() or not (start <= int(h) * 3600 < end):
                continue
            b = braked_bucket(v)
            n += b["n"]
            tags.update(b["c"])
            more = more or b["full"]
            minutes += len(b["m"])
            for m, k in b["m"].items():
                if k > peak[0]:
                    peak = (k, int(h) * 3600 + int(m) * 60)
        if n:
            out[scope] = {"count": n, "clients": len(tags), "clients_more": more,
                          "minutes": minutes, "peak_count": peak[0], "peak_at": peak[1]}
    return out


def ledger_line(day, scope, **parts):
    """One day of one scope. Empty parts are left out, None means 'not
    measured' and is kept -- the report says so instead of showing 0."""
    line = {"v": 1, "day": day, "scope": scope}
    for k, v in parts.items():
        if v is not None and v != {} and v != []:
            line[k] = v
        elif v is None:
            line.setdefault("unmeasured", []).append(k)
    return line


def read_ledger(path):
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for raw in f:
                try:
                    e = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(e, dict) and e.get("day"):
                    out.append(e)
    except OSError:
        return []
    # one line per day: a rewritten day replaces the earlier one
    by_day = {}
    for e in out:
        by_day[e["day"]] = e
    return [by_day[d] for d in sorted(by_day)]


def trim_lines(lines, today, keep_days=LEDGER_KEEP_DAYS):
    cut = time.strftime("%Y-%m-%d", time.localtime(_day_epoch(today) - keep_days * 86400))
    return [e for e in lines if e["day"] >= cut]


# ------------------------------------------------------------------ report

def week_of(day):
    """'2026-11-12' -> ('2026-W46', monday '2026-11-09', sunday '2026-11-15')."""
    t = time.strptime(day, "%Y-%m-%d")
    y, w, wd = _iso(t)
    mon = _day_epoch(day) - (wd - 1) * 86400
    return (f"{y}-W{w:02d}", time.strftime("%Y-%m-%d", time.localtime(mon)),
            time.strftime("%Y-%m-%d", time.localtime(mon + 6 * 86400)))


def _iso(t):
    import datetime as _dt
    return _dt.date(t.tm_year, t.tm_mon, t.tm_mday).isocalendar()


def build(lines, week_days, title, scope_is_node=True, history=None, tenant_names=None):
    """The Markdown of one report. `lines` the ledger lines of the week
    (any subset of `week_days`), `history` all ledger lines for the growth
    section. Summary first, details below; an empty section says so."""
    week = {e["day"]: e for e in lines}
    have = [d for d in week_days if d in week]
    events, mails, brake, backups = [], [], {}, []
    guarded, exposed = {}, {}
    for d in have:
        e = week[d]
        events += e.get("events") or []
        mails += e.get("mails") or []
        for inst, b in (e.get("brake") or {}).items():
            agg = brake.setdefault(inst, {"count": 0, "clients": 0, "more": False, "days": 0,
                                          "peak_count": 0, "peak_at": 0})
            agg["count"] += b.get("count", 0)
            agg["clients"] = max(agg["clients"], b.get("clients", 0))
            agg["more"] = agg["more"] or b.get("clients_more", False)
            agg["days"] += 1
            if b.get("peak_count", 0) > agg["peak_count"]:
                agg["peak_count"], agg["peak_at"] = b["peak_count"], b.get("peak_at", 0)
        if e.get("backup"):
            backups.append((d, e["backup"]))
        for inst, b in (e.get("guard") or {}).items():
            g = guarded.setdefault(inst, {"count": 0, "clients": 0, "more": False, "days": 0})
            g["count"] += b.get("count", 0)
            g["clients"] = max(g["clients"], b.get("clients", 0))
            g["more"] = g["more"] or b.get("clients_more", False)
            g["days"] += 1
        for inst, paths in (e.get("exposed") or {}).items():
            exposed[inst] = paths
    sent = [m for m in mails if m.get("r") == "sent"]
    reboots = [e for e in events if e["a"] == "reboot.started"]
    out = [f"# {title}", ""]
    if len(have) < len(week_days):
        out += [f"_Für {len(have)} von {len(week_days)} Tagen liegen Aufzeichnungen vor._", ""]

    # --- summary
    out += ["## Auf einen Blick", ""]
    summary = []
    if reboots:
        kern = _kernel_of(events)
        summary.append(f"{len(reboots)} Neustart" + ("s" if len(reboots) > 1 else "")
                       + (f" (Kernel {kern})" if kern else ""))
    if backups:
        good = [b for _, b in backups if b.get("state") == "ok"]
        last = backups[-1][1]
        summary.append(f"Backup: {len(good)} von {len(backups)} gelungen"
                       + (f", zuletzt {fmt_bytes(last.get('bytes'))}"
                          + (f" in {last['total_seconds']} s" if last.get("total_seconds") else "")
                          if last.get("state") == "ok" and last.get("bytes") else ""))
    elif scope_is_node:
        summary.append("Backup: kein Lauf aufgezeichnet")
    crit = [m for m in sent if m.get("op") in ("open", "event", "remind")]
    summary.append(f"{len(sent)} Meldung" + ("" if len(sent) == 1 else "en") + " verschickt"
                   + (f" ({len(crit)} Lagen/Ereignisse)" if crit and len(crit) != len(sent) else ""))
    disk = _last_value(week, have, "metrics", "disk")
    growth = _growth(history or lines, have[-1] if have else None, 7)
    if disk is not None:
        summary.append(f"Platte: {disk:.0f} % belegt"
                       + (f" ({fmt_delta(growth)} diese Woche)" if growth is not None else ""))
    if brake:
        total = sum(b["count"] for b in brake.values())
        who = max(brake.values(), key=lambda b: b["count"])
        summary.append(f"{total} gebremste Anfragen"
                       + (f", größter Teil von {_clients(who)}" if who["clients"] else ""))
    if exposed:
        summary.append(f"**{len(exposed)} Instanz" + ("en geben" if len(exposed) > 1 else " gibt")
                       + " auf direkte Anfrage eine Geheimnisdatei heraus** (siehe unten)")
    if guarded:
        total = sum(g["count"] for g in guarded.values())
        summary.append(f"{total} Anfragen nach Geheimnisdateien abgewiesen")
    if not events and not sent and not brake and not guarded and not exposed:
        summary.append("Keine besonderen Ereignisse")
    out += [f"- {s}" for s in summary] + [""]

    # --- events
    out += ["## Ereignisse", ""]
    if events:
        out += ["| Wann | Was | Einzelheiten |", "|---|---|---|"]
        for e in events:
            out.append(f"| {fmt_when(e['t'])} | {EVENT_WORDS.get(e['a'], e['a'])}"
                       + (f" ({e['s']})" if e.get("s") and e["s"] not in ("node", "") else "")
                       + f" | {_cell(e.get('d'))} |")
    else:
        out.append("Keine Ereignisse.")
    out.append("")

    # --- mails
    out += ["## Verschickte Meldungen", ""]
    shown = [m for m in mails if m.get("r") in ("sent", "report", "nobody", "failed", "limited")]
    if shown:
        out += ["| Wann | Art | Vorgang | Ergebnis |", "|---|---|---|---|"]
        for m in shown:
            out.append(f"| {fmt_when(m['t'])} | {m.get('type', '')} | {OP_WORDS.get(m.get('op'), m.get('op', ''))}"
                       f" | {RESULT_WORDS.get(m.get('r'), m.get('r', ''))} |")
    else:
        out.append("Keine Meldungen.")
    out.append("")

    # --- backup
    if backups or scope_is_node:
        out += ["## Backup", ""]
        if backups:
            out += ["| Tag | Ergebnis | Größe | Dauer | Stillstand |", "|---|---|---|---|---|"]
            for d, b in backups:
                out.append(f"| {fmt_day(d)} | {'gelungen' if b.get('state') == 'ok' else 'fehlgeschlagen'}"
                           f" | {fmt_bytes(b.get('bytes')) if b.get('bytes') else '–'}"
                           f" | {str(b['total_seconds']) + ' s' if b.get('total_seconds') else '–'}"
                           f" | {str(b['downtime_seconds']) + ' s' if b.get('downtime_seconds') else '–'} |")
        else:
            out.append("Kein Lauf aufgezeichnet.")
        out.append("")

    # --- brake
    if brake:
        out += ["## Gebremste Anfragen", "",
                "| Instanz | Abgewiesen | Adressen | an Tagen | Spitze |", "|---|---|---|---|---|"]
        for inst, b in sorted(brake.items(), key=lambda x: -x[1]["count"]):
            out.append(f"| {inst} | {b['count']} | {_clients(b) if b['clients'] else '–'} | {b['days']}"
                       f" | {b['peak_count']} um {fmt_epoch(b['peak_at'])} |" if b["peak_count"] else
                       f"| {inst} | {b['count']} | {_clients(b) if b['clients'] else '–'} | {b['days']} | – |")
        out += ["", "Wenige Adressen in wenigen Minuten sind meist ein Scanner. Adressen werden nur "
                    "gezählt, nie gespeichert.", ""]

    # --- secret files (RFC-0084)
    if guarded or exposed:
        out += ["## Geheimnisdateien", ""]
        if exposed:
            out += ["Die tägliche Selbstprüfung fand Dateien, die nie öffentlich sein dürfen:", ""]
            out += [f"- {inst}: {', '.join(p)}" for inst, p in sorted(exposed.items())] + [""]
        if guarded:
            out += ["| Instanz | abgewiesen | Adressen | an Tagen |", "|---|---|---|---|"]
            for inst, g in sorted(guarded.items(), key=lambda x: -x[1]["count"]):
                out.append(f"| {inst} | {g['count']} | {_clients(g) if g['clients'] else '–'} | {g['days']} |")
            out += ["", "Das Gateway beantwortet Anfragen nach .env, .git/, Schlüsseln, Abzügen u. a. "
                        "selbst mit 404 — die App sieht sie nie.", ""]

    # --- measurements
    if scope_is_node:
        out += ["## Messwerte", ""]
        rows = []
        for s, (word, unit) in SERIES_WORDS.items():
            cells = [week[d]["metrics"][s] for d in have if s in (week[d].get("metrics") or {})]
            if not cells:
                continue
            n = sum(c[0] for c in cells)
            mean = sum(c[0] * c[1] for c in cells) / n if n else 0
            u = _unit(unit)
            rows.append(f"| {word} | {_num(min(c[2] for c in cells))}{u} | {_num(mean)}{u}"
                        f" | {_num(max(c[3] for c in cells))}{u} |")
        if rows:
            out += ["| | min | Ø | max |", "|---|---|---|---|"] + rows
        else:
            out.append("Keine Messwerte aufgezeichnet.")
        out.append("")

    # --- growth
    if history:
        out += ["## Wachstum", ""]
        last = have[-1] if have else None
        cells = []
        for days, word in ((7, "Woche"), (31, "Monat"), (365, "Jahr")):
            g = _growth(history, last, days)
            cells.append(fmt_delta(g) if g is not None else
                         f"– (seit {_measured_days(history, last)} Tagen gemessen)")
        out += ["| | Woche | Monat | Jahr |", "|---|---|---|---|",
                "| Belegt | " + " | ".join(cells) + " |"]
        used = _last_bytes(history, last)
        if used:
            out += ["", f"Belegt am {fmt_day(last)}: {fmt_bytes(used['disk_used'])}, frei "
                        f"{fmt_bytes(used['disk_free'])}."]
        tb = _tenant_growth(history, last, 7, tenant_names or {})
        if tb:
            out += ["", "| Mandant | belegt | Woche |", "|---|---|---|"] + tb
        out.append("")
    out += ["--", "Diesen Bericht schreibt die OAAP-Plattform einmal pro Woche (RFC-0083). "
                  "Wer ihn bekommt, steht im Portal unter Nachrichten → Verteiler."]
    return "\n".join(out) + "\n"


OP_WORDS = {"open": "Lage begonnen", "close": "behoben", "remind": "Erinnerung",
            "event": "Ereignis", "test": "Probe"}
RESULT_WORDS = {"sent": "verschickt", "report": "nur im Bericht", "nobody": "niemand erreicht",
                "failed": "fehlgeschlagen", "limited": "Grenze erreicht"}


def _kernel_of(events):
    for e in reversed(events):
        m = re.search(r"running (\S+)", e.get("d", ""))
        if e["a"] == "reboot.done" and m:
            return m.group(1)
    return ""


def _clients(b):
    n = b["clients"]
    if b.get("more") or b.get("clients_more"):
        return "50+ Adressen"
    return f"{n} Adresse" + ("" if n == 1 else "n")


def _last_value(week, have, part, key):
    for d in reversed(have):
        v = (week[d].get(part) or {}).get(key)
        if v:
            return v[1] if isinstance(v, list) else v
    return None


def _bytes_by_day(history):
    return {e["day"]: e["bytes"] for e in history if (e.get("bytes") or {}).get("disk_used")}


def _last_bytes(history, last):
    bd = _bytes_by_day(history)
    days = [d for d in bd if last is None or d <= last]
    return bd[max(days)] if days else None


def _growth(history, last, days):
    """Bytes used on `last` minus `days` earlier (the nearest measured day at
    or before that, within a tenth of the span) -- None until measured."""
    if not last:
        return None
    bd = _bytes_by_day(history)
    if last not in bd:
        return None
    target = _day_epoch(last) - days * 86400
    slack = max(1, days // 10) * 86400
    cand = [d for d in bd if target - slack <= _day_epoch(d) <= target + 86400 * 0.5]
    if not cand:
        return None
    base = max(cand)
    return bd[last]["disk_used"] - bd[base]["disk_used"]


def _measured_days(history, last):
    bd = [d for d in _bytes_by_day(history) if not last or d <= last]
    if not bd:
        return 0
    return int((_day_epoch(max(bd)) - _day_epoch(min(bd))) // 86400) + 1


def _tenant_growth(history, last, days, names):
    bd = {e["day"]: (e.get("bytes") or {}).get("tenants") or {} for e in history}
    if not last or not bd.get(last):
        return []
    now = bd[last]
    target = time.strftime("%Y-%m-%d", time.localtime(_day_epoch(last) - days * 86400))
    old_days = [d for d in bd if d <= target and bd[d]]
    old = bd[max(old_days)] if old_days else {}
    rows = []
    for tid, b in sorted(now.items(), key=lambda x: -x[1]):
        delta = fmt_delta(b - old[tid]) if tid in old else "–"
        rows.append(f"| {names.get(tid, tid[:8])} | {fmt_bytes(b)} | {delta} |")
    return rows


def _cell(text):
    return (text or "").replace("|", "/").replace("\n", " ")[:160]


def _num(v):
    """German: 3,2 -- 46 -- 0,1 (one decimal below ten, none above)."""
    t = f"{v:.0f}" if abs(v) >= 10 else f"{v:.1f}".rstrip("0").rstrip(".")
    return t.replace(".", ",")


def _unit(u):
    return {"%": " %", "ms": " ms"}.get(u, u)


def fmt_bytes(n):
    if n is None:
        return "–"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return (f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}").replace(".", ",")
        n /= 1024
    return str(n)


def fmt_delta(n):
    if n is None:
        return "–"
    return ("+" if n >= 0 else "−") + fmt_bytes(abs(n))


def fmt_day(day):
    t = time.strptime(day, "%Y-%m-%d")
    return f"{DAY_NAMES[t.tm_wday]} {t.tm_mday:02d}.{t.tm_mon:02d}."


def fmt_epoch(t):
    lt = time.localtime(t)
    return f"{DAY_NAMES[lt.tm_wday]} {lt.tm_hour:02d}:{lt.tm_min:02d}"


def fmt_when(iso):
    t = _epoch(iso)
    if t is None:
        return iso or ""
    lt = time.localtime(t)
    return f"{DAY_NAMES[lt.tm_wday]} {lt.tm_mday:02d}.{lt.tm_mon:02d}. {lt.tm_hour:02d}:{lt.tm_min:02d}"


def _epoch(iso):
    if not iso:
        return None
    try:
        import datetime as _dt
        s = str(iso).replace("Z", "+00:00")
        d = _dt.datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=_dt.timezone.utc)
        return d.timestamp()
    except ValueError:
        return None


def _day_epoch(day):
    return time.mktime(time.strptime(day, "%Y-%m-%d"))


def day_bounds(day):
    """Local midnight to local midnight, epoch -- a report week is the
    operator's week, not UTC's."""
    start = _day_epoch(day)
    end = time.mktime(time.strptime(time.strftime("%Y-%m-%d", time.localtime(start + 90000)),
                                    "%Y-%m-%d"))
    return start, end


# ---------------------------------------------------------------- markdown

def to_html(md, title=""):
    """The subset the report writes: headings, lists, tables, paragraphs,
    emphasis, inline code. Everything escaped first -- a detail string from
    the log can never become markup."""
    out, para, table, items = [], [], [], []

    def inline(t):
        t = html.escape(t, quote=False)
        t = re.sub(r"`([^`]+)`", r"<code>\1</code>", t)
        t = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", t)
        t = re.sub(r"(?<![\w])_([^_]+)_(?![\w])", r"<em>\1</em>", t)
        return t

    def flush():
        if para:
            out.append("<p>" + inline(" ".join(para)) + "</p>")
            para.clear()
        if items:
            out.append("<ul>" + "".join(f"<li>{inline(i)}</li>" for i in items) + "</ul>")
            items.clear()
        if table:
            rows = [r for r in table if not re.fullmatch(r"\|?[\s:|-]+\|?", r)]
            cells = [[c.strip() for c in r.strip().strip("|").split("|")] for r in rows]
            if cells:
                h = "".join(f"<th>{inline(c)}</th>" for c in cells[0])
                b = "".join("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>"
                            for r in cells[1:])
                out.append(f'<table><thead><tr>{h}</tr></thead><tbody>{b}</tbody></table>')
            table.clear()

    for line in md.splitlines():
        s = line.rstrip()
        if s.startswith("|"):
            if para or items:
                flush()
            table.append(s)
            continue
        if table:
            flush()
        m = re.match(r"(#{1,3}) (.*)", s)
        if m:
            flush()
            n = len(m.group(1))
            out.append(f"<h{n}>{inline(m.group(2))}</h{n}>")
        elif s.startswith("- "):
            if para:
                flush()
            items.append(s[2:])
        elif s == "--":
            flush()
            out.append("<hr>")
        elif not s:
            flush()
        else:
            if items:
                flush()
            para.append(s)
    flush()
    body = "\n".join(out)
    if not title:
        return body
    return ("<!doctype html><html lang=\"de\"><head><meta charset=\"utf-8\">"
            f"<title>{html.escape(title)}</title><style>"
            "body{font-family:system-ui,sans-serif;max-width:46rem;margin:1rem auto;"
            "padding:0 1rem;color:#1f2937;line-height:1.45}"
            "h1{font-size:1.3rem}h2{font-size:1.05rem;margin-top:1.4rem;"
            "border-bottom:1px solid #e5e7eb;padding-bottom:.2rem}"
            "table{border-collapse:collapse;width:100%;font-size:.92rem}"
            "th,td{text-align:left;padding:.25rem .5rem;border-bottom:1px solid #e5e7eb}"
            "th{color:#6b7280;font-weight:600}hr{border:0;border-top:1px solid #e5e7eb}"
            "p,li{max-width:70ch}</style></head><body>" + body + "</body></html>")


def report_path(reports_dir, scope, week):
    return os.path.join(reports_dir, scope, f"{week}.md")


def list_reports(reports_dir, scope):
    try:
        names = sorted((n for n in os.listdir(os.path.join(reports_dir, scope))
                        if re.fullmatch(r"\d{4}-W\d{2}\.md", n)), reverse=True)
    except OSError:
        return []
    return [n[:-3] for n in names]
