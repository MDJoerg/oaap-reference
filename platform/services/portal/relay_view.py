"""Das Ereignis-Relais auf der Gesundheitsseite (oaap.core.portal 0.3.14).

RFC-0032 §1.5 hat entschieden, was passiert, wenn niemand die Ereignisse
des Zwillings abholt: **der Ausgang wächst, nichts geht verloren, und das
Portal sagt es.** Diese Datei ist das „sagt es" — als reine Regeln, ohne
Flask und ohne Anfrage, wie `store_view.py`/`twin_view.py` es vormachen.

Die Eingabe ist, was der Zwilling unter `/internal/twin/outbox` meldet:
je Mandant die Zahl wartender Ereignisse, das Alter der letzten
Rückmeldung des Relais (gemessen mit der Uhr von Postgres, nicht mit
der des Portals) und dessen letzter Fehler.

Dieselbe Haltung wie beim Deploy-Worker (Nachtrag 0.3.8): am **Symptom**
urteilen, nicht am Mechanismus, und jede Warnung nennt einen Weg zurück.
"""

NAME = "Ereignis-Relais"

# Das Relais meldet sich spätestens jede Minute (relay.HEARTBEAT_SECONDS).
# Erst nach mehreren verpassten Meldungen gilt es als verstummt — eine
# einzelne verpasste ist ein Neustart, kein Ausfall.
RELAY_STALE_SECONDS = 300


def _row(state, label, detail):
    return {"name": NAME, "state": state, "label": label, "detail": detail}


def _n(count, singular, plural):
    """„1 Ereignis wartet" / „3 Ereignisse warten" — Substantiv UND Verb.
    Live auf oaap-test stand zuerst „1 Ereignis warten" da (12.09.): nur
    das Substantiv folgte der Zahl."""
    if count == 1:
        return f"1 Ereignis {singular}"
    return f"{count} Ereignisse {plural}"


def relay_state(profiles, report):
    """Eine Zeile für die Tabelle „Kernservices" — oder None, wenn der
    Knoten gar keinen Zwilling trägt und es nichts zu berichten gibt.

    `profiles`: die Knotenprofile. `report`: die Antwort des Zwillings
    (`{"tenants": [...]}`), oder None, wenn er nicht antwortete.
    """
    profiles = set(profiles or ())
    if "store" not in profiles:
        return None
    if report is None:
        return _row("unknown", "Unbekannt",
                    "Der Zwilling-Dienst antwortet nicht — ob Ereignisse "
                    "warten, ist von hier nicht zu sehen.")
    tenants = report.get("tenants") or []
    if not tenants:
        return _row("ok", "Nichts zu tun",
                    "Noch kein Mandant mit digitalem Zwilling auf diesem Knoten.")

    broken = [t for t in tenants if t.get("error")]
    if broken:
        return _row("warn", "Unvollständig",
                    f"{len(broken)} Mandant(en) ohne Relais-Tabellen oder "
                    "ohne Datenbank — auf der Maschine: "
                    "'sudo oaap data store migrate-twin'")

    pending = sum(int(t.get("pending") or 0) for t in tenants)

    if "broker" not in profiles:
        if pending:
            return _row("warn", "Kein Broker",
                        f"{_n(pending, 'wartet', 'warten')} auf "
                        "Veröffentlichung, aber dieser Knoten trägt das "
                        "Profil 'broker' nicht — niemand holt sie ab. "
                        "Verloren geht nichts. Einschalten: "
                        "'sudo oaap node add-profile broker'")
        return _row("ok", "Nichts offen",
                    "Kein Broker auf diesem Knoten (Profil 'broker' fehlt); "
                    "es wartet kein Ereignis.")

    ages = [float(t["checked_age"]) for t in tenants
            if t.get("checked_age") is not None]
    newest = min(ages) if ages else None
    errors = sorted({t["last_error"] for t in tenants if t.get("last_error")})

    if newest is None or newest > RELAY_STALE_SECONDS:
        since = ("hat sich noch nie gemeldet" if newest is None else
                 f"hat sich seit {int(newest // 60)} Minuten nicht gemeldet")
        if pending:
            return _row("error", "Steht",
                        f"{_n(pending, 'wartet', 'warten')}, und das Relais "
                        f"{since} — prüfen mit 'docker logs oaap-relay-1', "
                        "wieder starten mit 'sudo oaap update'")
        return _row("warn", "Schweigt",
                    f"Es wartet kein Ereignis, aber das Relais {since} — "
                    "läuft der Container 'oaap-relay-1'? ('docker ps')")

    if errors:
        if pending:
            return _row("error", "Steht",
                        f"{_n(pending, 'wartet', 'warten')}; letzter Fehler: "
                        f"{errors[0]}")
        return _row("warn", "Keine Verbindung",
                    f"Es wartet kein Ereignis, aber: {errors[0]}")

    if pending:
        return _row("ok", "Arbeitet",
                    f"{_n(pending, 'wird', 'werden')} gerade veröffentlicht")
    return _row("ok", "Gesund", "Alle Ereignisse veröffentlicht")
