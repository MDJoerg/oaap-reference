"""The restart history of an instance (portal 0.3.61, 2.19).

The host writes one line per restart into apps/restarts/<instance>.jsonl
(appctl.restart_record); this module only reads it and puts it into the
words of the page. No Flask here, so the tests load it directly.
"""
import json
import os
import re
from datetime import datetime, timezone

CAUSE_LABELS = {
    "manual": "Manueller Neustart",
    "config": "Konfiguration geändert",
    "deploy": "Deployment",
    "install": "Installation",
    "update": "Plattform-Update",
    "rebuild": "Abbild neu gebaut",
    "wake": "Aufgeweckt",
    "key": "Instanzschlüssel erneuert",
    "setting": "Einstellung geändert",
    "rename": "Umbenannt",
    "restore": "Wiederherstellung",
    "backup": "Sicherung",
    "crash": "Absturz",
    "node": "Knoten-Neustart",
    "external": "Start von außen",
}
# The causes the minute timer found -- nobody on the platform asked for them
OUTSIDE = frozenset({"crash", "node", "external"})
SHOWN = 50


def journal_file(directory, name):
    return os.path.join(directory,
                        re.sub(r"[^A-Za-z0-9._-]", "_", str(name)) + ".jsonl")


def _when(stamp):
    """The stamp in UTC, to the minute (the page says UTC, like its other
    cards); the raw text if it cannot be read."""
    st = str(stamp or "")
    try:
        d = datetime.fromisoformat(st.replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc).strftime("%d.%m.%Y %H:%M")
    except ValueError:
        return st[:16].replace("T", " ")


def read(directory, name, limit=SHOWN):
    """The journal, newest first, as rows for the page; [] when there is
    none (an instance installed before 0.1.374 has no lines yet)."""
    rows = []
    try:
        with open(journal_file(directory, name), encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return rows
    for line in reversed(lines):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if not isinstance(r, dict) or not r.get("at") or not r.get("cause"):
            continue
        cause = str(r["cause"])
        count = int(r.get("count") or 1)
        detail = str(r.get("detail") or "")
        if count > 1:
            detail = (detail + "; " if detail else "") + \
                f"{count}-mal bis {_when(r.get('until'))}"
        rows.append({
            "at": str(r["at"]), "when": _when(r["at"]), "cause": cause,
            "label": CAUSE_LABELS.get(cause, cause),
            "outside": cause in OUTSIDE,
            "service": str(r.get("service") or ""),
            "by": str(r.get("by") or ("—" if cause in OUTSIDE else "")),
            "detail": detail, "count": count,
        })
        if len(rows) >= limit:
            break
    return rows


def last_sentence(rows):
    """One line for the overview: when the app last started and why."""
    if not rows:
        return ""
    r = rows[0]
    s = f"{r['when']} UTC — {r['label']}"
    if r["detail"]:
        s += f" ({r['detail']})"
    if r["by"] and r["by"] != "—":
        s += f", durch {r['by']}"
    return s
