"""Die Aufbau-Seiten (oaap.core.portal 2.9, RFC-0055 Stufe 3): was sie zeigen.

Gelesen wird dieselbe Datei wie in der Betreiber-API (`build-view.json`, vom
Host geschrieben); geändert wird nur über die Aufträge der API. Hier steht,
wie ein Aufbau dargestellt wird und wie aus einem Formular die Parameter
eines Aufbaus werden -- ohne Flask und ohne Anfrage, damit man es ohne Portal
prüfen kann (wie `cohort_view.py`).
"""
import re

BUILD_STATE = {
    "running": ("Läuft", "todo"),
    "waiting": ("Wartet auf einen Menschen", "todo"),
    "failed": ("Fehlgeschlagen", "todo"),
    "done": ("Fertig", "ok"),
    "rolled-back": ("Zurückgebaut", "off"),
}
STEP_STATE = {
    "pending": "offen", "running": "läuft", "done": "erledigt",
    "failed": "fehlgeschlagen", "waiting": "wartet", "skipped": "übersprungen",
}
STEP_TYPE = {
    "tenant.create": "Mandanten anlegen",
    "address.ensure": "Adresse veröffentlichen",
    "address.wait": "Warten, bis die Adresse antwortet",
    "idp.provision": "Anmeldedienst (Realm) einrichten",
    "tenant.policy": "Richtlinie für die erste Anmeldung",
    "tenant.face": "Gesicht (Titel, Farben)",
    "app.install": "Anwendung installieren",
    "manual": "Ein Mensch erledigt das",
    "backup.check": "Sicherung prüfen",
}
OPEN_STATES = ("running", "waiting", "failed")
BUILD_RE = re.compile(r"^b-[0-9]{8}t[0-9]{6}-[a-z0-9-]{1,63}$")
PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")


def state_label(state):
    return BUILD_STATE.get(state, (state or "?", "off"))


def _when(iso):
    """„2026-10-03 08:46“ aus einer ISO-Zeit; leer, wenn keine."""
    s = str(iso or "")
    return (s[:10] + " " + s[11:16]).strip() if len(s) >= 16 else s[:10]


def rows(view):
    """Die Aufbauten für die Liste, neueste zuerst (der Host sortiert so)."""
    out = []
    for b in (view or {}).get("builds") or []:
        label, tone = state_label(b.get("state"))
        out.append({"id": b.get("id", ""), "label": b.get("label", ""),
                    "profile": b.get("profile", ""), "state": label,
                    "tone": tone, "by": b.get("by", ""),
                    "created": _when(b.get("created"))})
    return out


def profiles(view):
    """Die Profile für die Liste: eines, das der Host abgelehnt hat, trägt
    seinen Grund und keinen Knopf."""
    out = []
    for p in (view or {}).get("profiles") or []:
        out.append({"id": p.get("id", ""), "title": p.get("title", ""),
                    "problem": p.get("problem", ""),
                    "steps": len(p.get("steps") or [])})
    return out


def find_profile(view, pid):
    for p in (view or {}).get("profiles") or []:
        if p.get("id") == pid and not p.get("problem"):
            return p
    return None


def find_build(view, bid):
    if not BUILD_RE.match(str(bid or "")):
        return None
    for b in (view or {}).get("builds") or []:
        if b.get("id") == bid:
            return b
    return None


def open_for(view, label):
    """Der unfertige Aufbau dieses Kürzels, oder None (einer zur Zeit)."""
    for b in (view or {}).get("builds") or []:
        if b.get("label") == label and b.get("state") in OPEN_STATES:
            return b
    return None


# ------------------------------------------------------------- the form

def fields(profile):
    """Die Formularfelder aus den Parametern des Profils.

    Jedes Feld: name, kind, label (die eigene Beschriftung des Profils oder
    der Name), required, default, max. Die Reihenfolge ist die der Datei.
    """
    out = []
    for name, spec in (profile.get("params") or {}).items():
        spec = spec if isinstance(spec, dict) else {}
        out.append({"name": name, "kind": spec.get("kind", "text"),
                    "label": spec.get("label") or name,
                    "required": bool(spec.get("required")),
                    "default": spec.get("default"),
                    "max": spec.get("max")})
    return out


def plan(profile):
    """Die Schritte in Klartext; ein Menschenschritt ist gekennzeichnet."""
    out = []
    for s in profile.get("steps") or []:
        typ = s.get("type", "")
        out.append({"id": s.get("id", ""), "text": STEP_TYPE.get(typ, typ),
                    "human": typ == "manual"})
    return out


def start_params(profile, form):
    """Formularwerte -> (Parameter, fehlende Pflichtfelder).

    Ein leeres optionales Feld und eine Farbe mit „keine Farbe“ gehen gar
    nicht erst in die Anfrage (der Host nimmt dann den Standard des
    Profils); was übrig bleibt, beurteilt der Host noch einmal.
    """
    params, missing = {}, []
    for f in fields(profile):
        raw = str(form.get(f["name"], "") or "").strip()
        if f["kind"] == "color" and form.get(f["name"] + "__none"):
            raw = ""
        if raw:
            params[f["name"]] = raw
        elif f["required"] and f["default"] is None:
            missing.append(f["name"])
    return params, missing


# ------------------------------------------------------------- the detail

def detail(build):
    """Eine Objektseite: Kopf, Schritte, und was auf einen Menschen wartet."""
    label, tone = state_label(build.get("state"))
    steps, done = [], 0
    waiting = failed = None
    for r in build.get("steps") or []:
        st = r.get("state", "pending")
        done += st == "done"
        row = {"id": r.get("id", ""), "type": STEP_TYPE.get(r.get("type"),
                                                            r.get("type", "")),
               "state": STEP_STATE.get(st, st), "raw": st,
               "note": r.get("note", ""), "made": list(r.get("made") or []),
               "human": r.get("type") == "manual"}
        steps.append(row)
        if st == "waiting" and waiting is None:
            waiting = {**row, "text": r.get("text") or r.get("note", ""),
                       "confirmable": r.get("done_when") == "confirmed"}
        if st == "failed" and failed is None:
            failed = row
    made_instances = any(m.startswith("instance:") for r in build.get("steps") or []
                         for m in r.get("made") or [])
    return {"id": build.get("id", ""), "label": build.get("label", ""),
            "profile": build.get("profile", ""), "state": label, "tone": tone,
            "raw_state": build.get("state", ""), "by": build.get("by", ""),
            "created": _when(build.get("created")), "steps": steps,
            "progress": f"{done} von {len(steps)}", "waiting": waiting,
            "failed": failed, "unfinished": build.get("state") in OPEN_STATES,
            "rolling_back": bool(build.get("rolling_back")),
            "rollbackable": build.get("state") != "rolled-back",
            "made_instances": made_instances,
            "running": build.get("state") == "running"}
