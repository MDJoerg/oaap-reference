"""Die Kohorten-Seite (oaap.core.portal 0.3.17): was sie zeigt, als reine Regeln.

Die Seite ist **nur lesend** (RFC-0046 Stufe 3, erste Fassung): sie liest
dieselbe Datei wie die Verwaltungs-API (`cohort-view.json`, vom Host
geschrieben) und ändert nichts. Wer etwas ändern will, nimmt die API oder
`oaap cohort`; die Seite sagt, wie. Ohne Flask und ohne Anfrage, wie
`relay_view.py`, damit man sie ohne Portal prüfen kann.
"""
from datetime import date

STATE_LABEL = {
    "running": ("Läuft", "ok"),
    "stopped": ("Angehalten", "off"),
    "incomplete": ("Unvollständig", "warn"),
}

INSTANCE_LABEL = {
    "running": "läuft",
    "exited": "angehalten",
    "absent": "nicht vorhanden",
    "missing": "fehlt",
}


def _parse(day):
    try:
        return date.fromisoformat(day)
    except (TypeError, ValueError):
        return None


def days_left(day, today=None):
    """Tage bis zu einem Datum (negativ: vorbei), None bei keinem Datum."""
    d = _parse(day)
    if d is None:
        return None
    return (d - (today or date.today())).days


def when(day, today=None):
    """„2026-10-24 (in 23 Tagen)" — das Datum bleibt lesbar und exakt."""
    n = days_left(day, today)
    if n is None:
        return "–"
    if n == 0:
        rel = "heute"
    elif n == 1:
        rel = "morgen"
    elif n > 0:
        rel = f"in {n} Tagen"
    elif n == -1:
        rel = "gestern"
    else:
        rel = f"vor {-n} Tagen"
    return f"{day} ({rel})"


def instance_label(state):
    """`running,exited` ist gemischt — das ist kein Ausfall, aber auch nicht gesund."""
    parts = [p for p in (state or "").split(",") if p]
    if not parts:
        return "unbekannt"
    return ", ".join(INSTANCE_LABEL.get(p, p) for p in parts)


def instance_tone(state):
    parts = set(p for p in (state or "").split(",") if p)
    if parts == {"running"}:
        return "ok"
    if parts & {"missing"}:
        return "warn"
    return "off"


def row(c, today=None):
    """Eine Zeile der Liste."""
    label, tone = STATE_LABEL.get(c.get("state"), (c.get("state") or "?", "off"))
    seats = c.get("seats") or []
    waiting = sum(1 for s in seats if s.get("waiting") or s.get("refused"))
    life = c.get("lifetime") or {}
    if c.get("ended"):
        label, tone = "Beendet", "off"
    return {"name": c.get("name", ""), "label": label, "tone": tone,
            "seats": len(seats), "problem_seats": waiting,
            "ends": when(life.get("ends"), today)}


def rows(cohorts, today=None):
    return sorted((row(c, today) for c in (cohorts or {}).values()),
                  key=lambda r: r["name"])


def detail(c, today=None):
    """Alles für die Detailseite einer Kohorte."""
    base = row(c, today)
    life = c.get("lifetime") or {}
    base["created"] = (c.get("created") or "")[:10] or "–"
    base["deactivate"] = when(life.get("deactivate_at"), today)
    base["delete"] = when(life.get("delete_at"), today)
    seats = []
    for s in c.get("seats") or []:
        items = [{"app": i.get("app", ""), "label": instance_label(i.get("state")),
                  "tone": instance_tone(i.get("state")),
                  "address": i.get("address", "")}
                 for i in s.get("instances") or []]
        note = ""
        if s.get("refused"):
            note = f"abgelehnt: {s['refused']}"
        elif s.get("waiting"):
            note = "wartet auf eine freie Instanz"
        seats.append({"id": s.get("id", ""), "user": s.get("user", ""),
                      "label": s.get("label") or "", "instances": items,
                      "note": note})
    base["seat_list"] = seats
    return base
