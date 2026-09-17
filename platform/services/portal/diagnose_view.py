"""Was das Portal über den Zustand einer Instanz zeigen darf (RFC-0038).

Drei Fragen, in steigender Empfindlichkeit — und diese Reihenfolge ist
die ganze Gestaltung:

1. **Zustand** (D1): Läuft der Container, seit wann, wie oft neu
   gestartet, letzter Exit-Code, wegen Speichermangel beendet. Das sind
   Tatsachen über einen Container, nichts, was die App geschrieben hat.
   Immer sichtbar, ohne Freischaltung, ohne Protokolleintrag.
2. **Das Diagnose-Fenster** (D2): ausdrücklich geöffnet, 15/30/60
   Minuten, im Mandantenprotokoll. Nur solange es offen ist, gibt es das
   App-Log.
3. **Die Gateway-Sicht** (D3): nur bei offenem Fenster aufgezeichnet —
   und nie Query-Parameter, nie `Authorization`, nie Cookies, nie die
   Identitäts-Kopfzeilen.

Wie `store_view` und `instance_view` trägt diese Datei **kein Flask**:
Das sind Regeln aus einem RFC, und sie sollen ohne Anfrage, ohne
Container und ohne Knoten lesbar und prüfbar sein.

**Der Leser hier ist eine ERLAUBNISLISTE.** Der Filter im Gateway
löscht, was nach D3 nicht geschrieben werden darf; diese Datei nimmt
darüber hinaus nur die Felder an, die sie kennt. Zwei Schichten mit
umgekehrter Logik: Fängt Caddy in einer künftigen Fassung an, ein neues
Feld zu protokollieren, erscheint es auf keiner Seite — auch wenn
niemand daran gedacht hat, es zu löschen.
"""
import json
import re
from datetime import datetime, timezone

# --- D1: Zustand --------------------------------------------------------

# Dockers Vokabular ist für ein Log in Ordnung. „exited" ist keine
# Antwort auf „was ist mit meiner App".
STATE_LABEL = {
    "running": "läuft",
    "restarting": "startet neu",
    "paused": "angehalten",
    "exited": "beendet",
    "dead": "tot",
    "created": "angelegt, nie gestartet",
    "removing": "wird entfernt",
    "absent": "kein Container vorhanden",
}
HEALTH_LABEL = {
    "healthy": "Selbstauskunft: gesund",
    "unhealthy": "Selbstauskunft: krank",
    "starting": "Selbstauskunft: startet noch",
}

# Ein Neustartzähler, der sich in den letzten zehn Minuten bewegt hat,
# ist der Befund „die App startet vermutlich immer wieder neu" (D1).
# Zehn Minuten, weil ein einzelner Neustart nach einem Deployment normal
# ist und eine Stunde später niemanden mehr interessiert.
RESTART_WARN_MINUTES = 10


def _parse_iso(stamp):
    """Ein Zeitpunkt aus Docker oder aus unserer eigenen Ansicht.

    Docker schreibt Nanosekunden und ein 'Z'; Python 3.11+ kommt mit dem
    'Z' zurecht, mit neun Nachkommastellen nicht. Beides wird hier
    zurechtgeschnitten — und ein unlesbarer Zeitpunkt ergibt None statt
    einer erfundenen Zahl.
    """
    s = str(stamp or "").strip()
    if not s or s.startswith("0001-01-01"):
        return None
    s = re.sub(r"(\.\d{6})\d+", r"\1", s).replace("Z", "+00:00")
    try:
        when = datetime.fromisoformat(s)
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def since_words(when, now=None):
    """„seit 3 Minuten" — in der Einheit, in der ein Mensch wartet."""
    if when is None:
        return ""
    now = now or datetime.now(timezone.utc)
    secs = int((now - when).total_seconds())
    if secs < 0:
        return "gerade"
    if secs < 90:
        return f"{max(1, secs)} Sekunden"
    if secs < 5400:
        return f"{secs // 60} Minuten"
    if secs < 172800:
        return f"{secs // 3600} Stunden"
    return f"{secs // 86400} Tagen"


def state_rows(inst, view, now=None):
    """Der Zustand je Dienst-Container dieser Instanz (D1).

    `view` ist der Eintrag des Hosts für diese Instanz (oder None, wenn
    der Host noch nichts geschrieben hat). None ist **nicht** „läuft
    nicht": Ohne Ansicht sagt die Karte „unbekannt" — die Seite darf
    keine Störung behaupten, nur weil sie nichts gelesen hat.
    """
    now = now or datetime.now(timezone.utc)
    declared = inst.get("services") or [{"service": "",
                                         "container": inst.get("container", "")}]
    facts = {s.get("container"): s for s in ((view or {}).get("services") or [])}
    rows = []
    for d in declared:
        f = facts.get(d.get("container")) or {}
        state = str(f.get("state") or "")
        started = _parse_iso(f.get("started"))
        restarts = int(f.get("restarts") or 0)
        known = bool(f)
        level = "ok"
        if not known:
            level = "unknown"
        elif state != "running":
            level = "err"
        elif f.get("health") == "unhealthy" or f.get("oom"):
            level = "warn"
        rows.append({
            "service": d.get("service") or "",
            "container": d.get("container") or "",
            "known": known,
            "state": state,
            "label": (STATE_LABEL.get(state, state or "unbekannt") if known
                      else "unbekannt"),
            "since": since_words(started, now) if started else "",
            "restarts": restarts,
            "exit_code": int(f.get("exit_code") or 0),
            "oom": bool(f.get("oom")),
            "health": HEALTH_LABEL.get(f.get("health"), ""),
            "level": level,
            # Der Befund aus D1, aus EINER Momentaufnahme ableitbar: Der
            # Zähler steht über null UND der Container ist gerade erst
            # gestartet. Genau das ist eine Neustartschleife — und es
            # braucht keine zweite Messung, die es nicht gibt.
            "looping": bool(restarts and started
                            and (now - started).total_seconds()
                            < RESTART_WARN_MINUTES * 60),
        })
    return rows


def state_warning(rows):
    """Der eine Satz über den Reitern, oder "" — nie mehr als einer.

    Reihenfolge nach Dringlichkeit: eine Schleife ist der Fund, den
    dieses RFC sichtbar machen soll, ein toter Container der, den man
    ohnehin sieht.
    """
    loop = next((r for r in rows if r["looping"]), None)
    if loop:
        return (f"{loop['restarts']} Neustarts, zuletzt vor "
                f"{loop['since']} — die App startet vermutlich immer wieder "
                f"neu. Das Diagnose-Fenster zeigt, warum.")
    oom = next((r for r in rows if r["oom"]), None)
    if oom:
        return ("Der Container wurde wegen Speichermangel beendet "
                "(OOM) — die App braucht mehr Arbeitsspeicher, als der "
                "Knoten ihr geben konnte.")
    down = next((r for r in rows if r["level"] == "err"), None)
    if down:
        extra = (f" (Exit-Code {down['exit_code']})"
                 if down["state"] == "exited" else "")
        return (f"Dieser Dienst {down['label']}{extra}. Über „App neu "
                f"starten“ im Reiter Verwaltung kommt er zurück; warum "
                f"er stehen blieb, zeigt das Diagnose-Fenster.")
    return ""


# --- D2: das Fenster ----------------------------------------------------

DURATIONS = (15, 30, 60)
DEFAULT_MINUTES = 30

# Der Satz, der über beiden Ansichten steht. Wörtlich aus D2 — er ist
# die Begründung dafür, dass das Fenster überhaupt eines ist.
LOG_WARNING = ("Logs können vertrauliche Daten enthalten, die die App "
               "selbst schreibt. Öffne das Fenster nur für die "
               "Fehlersuche.")
# Und der Satz, der die häufigste Enttäuschung verhindert (D3).
COLLECT_NOTE = ("Aufgezeichnet wird ab jetzt — ruf die App danach noch "
                "einmal auf.")


def window(inst, now=None):
    """Das offene Fenster dieser Instanz, oder None.

    Ein abgelaufenes Fenster ist None, auch bevor der Sweep gelaufen
    ist: Die Seite darf nie ein Fenster als offen zeigen, dessen Zeit um
    ist — sonst wäre die Zeitgrenze eine Anzeige und keine Zusage.
    """
    w = (inst or {}).get("diagnose") or None
    if not w:
        return None
    until = _parse_iso(w.get("until"))
    now = now or datetime.now(timezone.utc)
    if until is None or until <= now:
        return None
    left = int((until - now).total_seconds())
    return {"until": w.get("until", ""),
            "minutes": int(w.get("minutes") or 0),
            "by": w.get("by") or "?",
            "opened": w.get("opened", ""),
            "left_seconds": left,
            "left": (f"{left // 60} Minuten" if left >= 90
                     else f"{left} Sekunden")}


# --- D3: die Gateway-Sicht ---------------------------------------------

# Die Erlaubnisliste. Was hier nicht steht, erreicht keine Seite.
REQUEST_HEADERS = ("Origin", "Access-Control-Request-Method",
                   "Access-Control-Request-Headers")
RESPONSE_HEADERS = ("Access-Control-Allow-Origin", "Access-Control-Allow-Methods",
                    "Access-Control-Allow-Headers",
                    "Access-Control-Allow-Credentials", "Vary")
# Was der Gateway-Filter an die Stelle eines Nachweises schreibt: die
# Tatsache, dass einer da war, nie sein Wert (D3).
REDACTED = "REDACTED"
MAX_ROWS = 200


def _first(headers, name):
    v = (headers or {}).get(name)
    if isinstance(v, list):
        return str(v[0]) if v else ""
    return str(v or "")


def answered_by(status, method, location):
    """Wer hat geantwortet? Die Fälle, die D3 auseinanderhält."""
    if method == "OPTIONS" and 200 <= status < 300:
        return "Vorab-Anfrage, direkt von der App"
    if method == "OPTIONS" and status >= 400:
        # Am 17.09. auf oaap-test gemessen: Die Vorab-Anfrage ging am
        # Gateway vorbei zur App (RFC-0027), und die antwortete 501 --
        # sie kennt OPTIONS nicht. Im Browser heißt das „CORS-Fehler",
        # und ohne diese Zeile hätte die Seite „von der App
        # beantwortet" gesagt: wahr und nutzlos.
        return "Vorab-Anfrage abgelehnt — die App kennt OPTIONS nicht"
    if 300 <= status < 400 and "/auth/login" in (location or ""):
        return "zur Anmeldung umgeleitet"
    if status == 401:
        return "abgelehnt: kein oder kein gültiger Schlüssel"
    if status == 403:
        return "abgelehnt: Rolle, Gruppe oder Mandant passt nicht"
    if status == 429:
        return "gebremst (zu viele Anfragen)"
    if status == 502 or status == 503:
        return "die App war nicht erreichbar"
    return "von der App beantwortet"


def gateway_rows(text, limit=MAX_ROWS):
    """Die Anfragen aus dem Gateway-Log dieser Instanz, neueste unten.

    `text` ist der Inhalt der Logdatei (JSON je Zeile). Eine kaputte
    Zeile wird übersprungen, nie fatal: Das ist eine Aufzeichnung, und
    eine Aufzeichnung, die wegen eines falschen Bytes nicht mehr lesbar
    ist, hilft niemandem.
    """
    rows = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if not isinstance(e, dict):
            continue
        req = e.get("request") or {}
        rh = e.get("resp_headers") or {}
        h = req.get("headers") or {}
        try:
            status = int(e.get("status") or 0)
        except (TypeError, ValueError):
            status = 0
        when = None
        try:
            when = datetime.fromtimestamp(float(e.get("ts")), timezone.utc)
        except (TypeError, ValueError):
            pass
        location = _first(rh, "Location")
        # Nochmals ohne Query, obwohl der Filter das schon tut: Eine
        # Datei aus einer Fassung vor dem Filter darf keine
        # Query-Parameter auf eine Seite bringen.
        uri = str(req.get("uri") or "").split("?")[0]
        rows.append({
            "when": when.strftime("%H:%M:%S") if when else "?",
            "method": str(req.get("method") or "?"),
            "path": uri,
            "status": status,
            "ms": int(round(float(e.get("duration") or 0) * 1000)),
            "credentials": (_first(h, "Authorization") == REDACTED
                            or _first(h, "Cookie") == REDACTED),
            "origin": _first(h, "Origin"),
            "req_headers": [(n, _first(h, n)) for n in REQUEST_HEADERS
                            if _first(h, n)],
            "resp_headers": [(n, _first(rh, n)) for n in RESPONSE_HEADERS
                             if _first(rh, n)],
            "acao": _first(rh, "Access-Control-Allow-Origin"),
            "verdict": answered_by(status, str(req.get("method") or ""),
                                   location),
        })
    return rows[-limit:]


def cors_note(rows):
    """Eine Zeile Deutung, wo das Muster eindeutig ist (D3).

    Bewusst höchstens eine, und bewusst nur für Muster, bei denen es
    keine zweite Lesart gibt. Eine Seite, die raten anfängt, schickt
    jemanden in die nächste falsche Richtung — und genau davon handelt
    dieses RFC.
    """
    cross = [r for r in rows if r["origin"]]
    if not cross:
        return ""
    # Zuerst die Vorab-Anfrage, immer: Scheitert sie, bricht der Browser
    # ab, BEVOR die eigentliche Anfrage gestellt wird. Alles Spätere in
    # der Liste ist dann eine Folge und keine Ursache.
    pre = next((r for r in cross if r["method"] == "OPTIONS"
                and r["verdict"] == "zur Anmeldung umgeleitet"), None)
    if pre:
        return ("Die Vorab-Anfrage (OPTIONS) wurde zur Anmeldung "
                "umgeleitet — der Browser bricht hier ab, bevor die "
                "eigentliche Anfrage je gestellt wird.")
    prefail = next((r for r in cross if r["method"] == "OPTIONS"
                    and r["status"] >= 400), None)
    if prefail:
        return (f"Die Vorab-Anfrage (OPTIONS {prefail['path']}) wurde mit "
                f"{prefail['status']} beantwortet — der Browser bricht hier "
                f"ab. Das Gateway reicht OPTIONS auf einer rollengeschützten "
                f"Route unverändert an die App weiter (RFC-0027); sie muss "
                f"darauf mit 200 oder 204 und den CORS-Kopfzeilen antworten.")
    def refused(r):
        return (r["method"] != "OPTIONS" and not r["credentials"]
                and (r["status"] in (401, 403)
                     or r["verdict"] == "zur Anmeldung umgeleitet"))

    nokey = next((r for r in cross if refused(r)), None)
    if nokey:
        return (f"{nokey['method']} {nokey['path']} kam OHNE Nachweis an "
                f"und wurde abgelehnt. Von einer anderen Herkunft führt "
                f"nur ein API-Schlüssel hinein (Reiter „Zugang“), keine "
                f"Browser-Anmeldung.")
    silent = next((r for r in cross if 200 <= r["status"] < 400
                   and not r["acao"] and r["method"] != "OPTIONS"), None)
    if silent:
        return (f"Das Gateway hat {silent['method']} {silent['path']} "
                f"durchgelassen ({silent['status']}), aber die Antwort "
                f"trägt keine CORS-Kopfzeile — der Browser verwirft sie "
                f"trotzdem. Das ist die App, nicht die Plattform.")
    star = next((r for r in cross if r["acao"] == "*" and r["credentials"]),
                None)
    if star:
        return ("Die App antwortet mit „Access-Control-Allow-Origin: *“, "
                "der Aufruf schickt aber Anmeldedaten mit. Diese "
                "Kombination verbietet der Browser — die App muss die "
                "Herkunft namentlich nennen.")
    return ""


# --- das App-Log (D2) ---------------------------------------------------


def log_sections(snapshot):
    """Das aufgenommene App-Log je Dienst, älteste Zeile oben.

    `snapshot` ist die Datei, die der Host geschrieben hat, oder None.
    None heißt „noch nichts aufgenommen" und nicht „leeres Log" — der
    Unterschied ist genau der zwischen „gleich noch mal drücken" und
    „die App schreibt nichts".
    """
    if not snapshot:
        return None
    out = []
    for s in snapshot.get("services") or []:
        out.append({"service": s.get("service") or "",
                    "container": s.get("container") or "",
                    "lines": [str(x) for x in (s.get("lines") or [])]})
    return {"written": snapshot.get("written", ""),
            "tail": snapshot.get("tail", 0), "services": out}
