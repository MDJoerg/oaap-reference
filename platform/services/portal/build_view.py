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


# ------------------------------------------- invitations and requests (stage 4)
#
# The prospect's form and the operator's list of requests (RFC-0055 §13). The
# portal reads `request-view.json` (written by the host) and writes nothing:
# every button queues a request the host judges again.

import hashlib

INVITE_STATE = {"open": ("Offen", "todo"), "used": ("Benutzt", "ok"),
                "revoked": ("Widerrufen", "off"), "expired": ("Abgelaufen", "off")}
REQUEST_STATE = {"pending": ("Wartet auf Entscheidung", "todo"),
                 "approved": ("Freigegeben", "ok"),
                 "rejected": ("Abgelehnt", "off")}
ITEM_RE = re.compile(r"^(inv|req)-[0-9a-f]{12}$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,64}$")
MAIL_RE = re.compile(r"^[^@\s<>\"',;]{1,64}@[^@\s<>\"',;]{1,120}\.[^@\s<>\"',;]{2,}$")
LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")
FIELD_MAX = 300


def token_profile(request_view, token):
    """Das Profil, zu dem dieser Link gehört, oder None, wenn er nicht (mehr)
    offen ist. Verglichen wird der Hash; den Link selbst hat nur der, dem
    man ihn gegeben hat."""
    if not TOKEN_RE.match(str(token or "")):
        return None
    h = hashlib.sha256(str(token).encode("utf-8")).hexdigest()
    return ((request_view or {}).get("live") or {}).get(h)


def invite_rows(request_view):
    out = []
    for i in (request_view or {}).get("invites") or []:
        label, tone = INVITE_STATE.get(i.get("state"), (i.get("state", "?"), "off"))
        out.append({"id": i.get("id", ""), "profile": i.get("profile", ""),
                    "note": i.get("note", ""), "state": label, "tone": tone,
                    "raw": i.get("state", ""), "created": _when(i.get("created")),
                    "expires": _when(i.get("expires"))[:10], "by": i.get("by", "")})
    return out


def request_rows(request_view):
    out = []
    for r in (request_view or {}).get("requests") or []:
        label, tone = REQUEST_STATE.get(r.get("state"), (r.get("state", "?"), "off"))
        out.append({"id": r.get("id", ""), "profile": r.get("profile", ""),
                    "label": r.get("label", ""), "note": r.get("note", ""),
                    "params": list((r.get("params") or {}).items()),
                    "contact": r.get("contact", ""), "state": label, "tone": tone,
                    "raw": r.get("state", ""), "created": _when(r.get("created")),
                    "decided": _when(r.get("decided")), "by": r.get("by", ""),
                    "build": r.get("build", ""), "reason": r.get("reason", "")})
    return out


def find_item(request_view, rid):
    if not ITEM_RE.match(str(rid or "")):
        return None
    for key in ("invites", "requests"):
        for x in (request_view or {}).get(key) or []:
            if x.get("id") == rid:
                return x
    return None


def pending_count(request_view):
    return sum(1 for r in (request_view or {}).get("requests") or []
               if r.get("state") == "pending")


def public_params(profile, form):
    """Formular des Interessenten -> (Parameter, Kontakt, Fehler).

    Strenger als der Betreiber-Assistent, weil hier niemand angemeldet ist:
    jedes Feld ist auf FIELD_MAX Zeichen gekappt, das Kürzel hat sein Format
    schon hier, die Adresse sieht aus wie eine Adresse. Der Host urteilt noch
    einmal (und kennt, was nur er weiß: ob das Kürzel frei ist)."""
    params, missing = start_params(profile, form)
    errors = []
    if missing:
        errors.append("Bitte ausfüllen: " + ", ".join(missing))
    for k, v in list(params.items()):
        if len(v) > FIELD_MAX:
            errors.append(f"'{k}' ist zu lang")
    for f in fields(profile):
        v = params.get(f["name"])
        if f["kind"] == "label" and v is not None:
            params[f["name"]] = v = v.lower()
            if not LABEL_RE.match(v):
                errors.append("Das Kürzel darf nur Kleinbuchstaben, Ziffern und „-“ "
                              "enthalten (höchstens 31 Zeichen).")
    contact = str(form.get("contact", "") or "").strip()
    if not MAIL_RE.match(contact) or len(contact) > 160:
        errors.append("Bitte eine E-Mail-Adresse angeben, unter der wir Sie erreichen.")
    return params, contact, errors
