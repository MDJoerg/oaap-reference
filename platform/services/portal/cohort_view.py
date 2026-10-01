"""Die Kohorten-Seite (oaap.core.portal): was sie zeigt, als reine Regeln.

Gelesen wird dieselbe Datei wie in der Verwaltungs-API (`cohort-view.json`,
vom Host geschrieben); geändert wird über die Aufträge der API. Hier steht
nur, wie etwas angezeigt wird -- und die Beispielvorlage zum Herunterladen.
Ohne Flask und ohne Anfrage, wie `relay_view.py`, damit man sie ohne Portal
prüfen kann.
"""
import io
import zipfile
from datetime import date, timedelta

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


# --------------------------------------------------------- the example template

EXAMPLE_NAME = "beispiel-kurs"

EXAMPLE_YAML = """\
# Vorlage für eine Kohorte (OAAP). Jede Zeile ist erklärt -- ändere, was Du brauchst.
# Diese Datei muss `cohort.yaml` heißen und im ZIP ganz oben liegen.
oaap_cohort: "0.1"          # Format der Vorlage; nicht ändern
name: beispiel-kurs         # Kleinbuchstaben, Ziffern, "-", höchstens 24 Zeichen.
                            # Der Name steht vor allen Benutzern und Instanzen.
seats: 3                    # Anzahl der Plätze (1 bis 99). Oder statt dessen eine Liste:
                            #   participants: ["Anna Beispiel", "Ben Test"]
lifetime:
  ends: {ends}        # letzter Kurstag (JJJJ-MM-TT); am Tag danach werden die Instanzen angehalten
  deactivate_users_after: 30d   # optional: Benutzer sind 30 Tage nach `ends` gesperrt
  delete_users_after: 90d       # optional: ... und nach 90 Tagen gelöscht (muss später liegen)
resources:                  # je Instanz
  memory: 1g
  cpus: 1
apps:
  - id: code-server         # eine App aus dem Store des Knotens
    name: ide               # wird der Teil im Instanznamen: beispiel-kurs-ide-01
    config:                 # Einstellungen der App (hier: nur die Zeitzone)
      TZ: Europe/Berlin     # Zugangsdaten gehören NICHT hierher
    seed:                   # Dateien, die jeder Platz beim ersten Start im Home bekommt
      willkommen.md: seeds/willkommen.md   # Ziel im Home: Quelle im ZIP.
                                           # In der Datei: {{name}} = Kohorte, {{nn}} = Platz, {{user}} = Benutzer
    material: material/     # Ordner im ZIP, den jeder Platz mitbekommt (Kursunterlagen)
users:
  prefix: tn                # Benutzername: beispiel-kurs-tn-01
  display_name: "Teilnehmer {{nn}}"   # {{nn}} = Platznummer
handout: handout.csv        # die Zugangsdaten; gibt es nur einmal, direkt nach dem Anlegen
"""

EXAMPLE_FILES = {
    "seeds/willkommen.md": (
        "# Willkommen im Kurs {name}!\n\n"
        "Dein Platz ist {nn}, Dein Benutzername {user}. Diese Datei liegt in "
        "Deinem Home -- Du kannst sie ändern oder löschen.\n"),
    "material/uebung1.md": (
        "# Übung 1\n\nHier stehen die Kursunterlagen. Der Ordner `material/` "
        "der Vorlage liegt bei jedem Teilnehmer.\n"),
}


def example_files(today=None):
    """The example as {path: text}; `ends` is four weeks from today so that
    the template is always one that may be used."""
    ends = ((today or date.today()) + timedelta(days=28)).isoformat()
    out = {"cohort.yaml": EXAMPLE_YAML.format(ends=ends)}
    out.update(EXAMPLE_FILES)
    return out


def example_zip(today=None):
    """The example as a ZIP, `cohort.yaml` at its root."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for path, text in example_files(today).items():
            z.writestr(path, text.encode("utf-8"))
    return buf.getvalue()
