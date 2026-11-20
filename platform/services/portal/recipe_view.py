"""Die Seite Einrichten (RFC-0109 Stufe 2): Rezepte in einem bestehenden Mandanten.

Gelesen wird `recipe-view.json` (vom Host geschrieben: Rezepte, ihre
Freigaben, die Mandanten, die letzten Läufe); geändert wird nur über
Aufträge (Aktion `tenant-recipe`), die der Host noch einmal beurteilt --
Rolle und Mandant aus dem Datensatz des Auslösers, nie aus der Anfrage.
Hier steht nur, was die Seite zeigt und wie aus einem Formular die Werte
eines Rezepts werden: ohne Flask, damit es sich ohne Portal prüfen lässt.

Was ein tenant_admin sieht, entscheidet die Freigabe: nur Rezepte, die der
Betreiber für SEINEN Mandanten freigegeben hat, und nur die Läufe seines
Mandanten. Der Betreiber sieht am Ort eines Mandanten jedes Rezept.
"""
import re

import build_view

RECIPE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
RUN_RE = re.compile(r"^r-[0-9]{8}t[0-9]{6}-[a-z0-9-]{1,63}$")
PARAM_RE = re.compile(r"^[a-z][a-z0-9_]{0,30}$")
VALUE_MAX = 200
LIGHT_WORDS = (("green", "grün"), ("yellow", "gelb"), ("red", "rot"))
VERDICT = {"present": ("vorhanden", "ok"), "absent": ("wird angelegt", "todo"),
           "conflict": ("Konflikt", "err"), "skipped": ("nicht gewünscht", "off"),
           "refused": ("abgelehnt", "err"),
           # Stufe 3: hinter einem Schritt, der auf einen Menschen wartet
           "later": ("erst danach prüfbar", "off")}


def recipes_for(view, label, role):
    """Die Rezepte, die diese Person in diesem Mandanten sieht."""
    out = []
    for r in (view or {}).get("recipes") or []:
        if r.get("problem"):
            if role == "server_admin":
                out.append(dict(r, runnable=False))
            continue
        mine = label in (r.get("released_for") or [])
        if role == "server_admin" or (role == "tenant_admin" and mine):
            out.append(dict(r, runnable=True, released_here=mine))
    return out


def find_recipe(view, rid, label, role):
    if not RECIPE_RE.match(str(rid or "")):
        return None
    return next((r for r in recipes_for(view, label, role)
                 if r["id"] == rid and r.get("runnable")), None)


def runs_for(view, label):
    """Die Läufe dieses Mandanten für die Liste, neueste zuerst."""
    out = []
    for st in (view or {}).get("runs") or []:
        if st.get("label") != label:
            continue
        text, tone = build_view.state_label(st.get("state"))
        out.append({"id": st.get("id", ""), "recipe": st.get("recipe", ""),
                    "title": st.get("title", ""), "state": text, "tone": tone,
                    "by": st.get("by", ""), "created": build_view._when(st.get("created")),
                    "version": build_view.version_text(st.get("version"))})
    return out


def find_run(view, run_id, label):
    if not RUN_RE.match(str(run_id or "")):
        return None
    return next((st for st in (view or {}).get("runs") or []
                 if st.get("id") == run_id and st.get("label") == label), None)


def run_detail(st):
    """Ein Lauf wie ein Aufbau (derselbe Zustand der Engine), ohne Instanzen-
    Haken: ein Rezept löscht nie eine Instanz von der Seite aus."""
    d = build_view.detail(st)
    d["recipe"] = st.get("recipe", "")
    d["title"] = st.get("title", "")
    d["needs"] = st.get("needs") or {}
    return d


def light_options(chosen, suggested=""):
    return [{"value": v, "text": t + (" (vom Modell vorgeschlagen)" if v == suggested else ""),
             "selected": v == chosen} for v, t in LIGHT_WORDS]


def fields(recipe, preview=None, prefill=None):
    """Das Formular: erst die Apps (leer = die einzige Instanz der App), dann
    die Werte. Mit einer Vorschau stehen die Listen der Apps zur Wahl und
    die Werte, die die Vorschau benutzt hat. `prefill` (Stufe 3): die
    Instanz, von deren Seite man kam (?<bedarf>=<name>), ohne Vorschau."""
    choices = (preview or {}).get("choices") or {}
    used = dict((preview or {}).get("params") or {})
    needs_used = {n: c.get("name", "") for n, c in ((preview or {}).get("needs") or {}).items()}
    if not preview:
        needs_used.update({n: str(v)[:VALUE_MAX] for n, v in (prefill or {}).items()
                           if n in (recipe.get("needs") or {}) and PARAM_RE.match(n)})
    out = []
    for n, spec in (recipe.get("needs") or {}).items():
        out.append({"name": n, "kind": "need", "label": spec.get("label") or n,
                    "app": spec.get("app", ""), "value": needs_used.get(n, ""),
                    "install": bool(spec.get("install"))})
    for n, spec in (recipe.get("params") or {}).items():
        kind = spec.get("kind", "text")
        f = {"name": n, "kind": kind, "label": spec.get("label") or n,
             "value": str(used.get(n, "") or (choices.get(n) or {}).get("value", "")
                          or spec.get("default", "") or "")}
        ch = choices.get(n) or {}
        if kind == "setup_item":
            f["entries"] = ch.get("items") or []
            f["note"] = ch.get("note", "")
            f["source"] = f"{spec.get('need')}/{spec.get('item_kind')}"
        elif kind == "light":
            f["options"] = light_options(f["value"], ch.get("suggested", ""))
        elif kind == "bool":
            f["checked"] = f["value"] in ("true", "yes", "ja", "1", "True")
        out.append(f)
    return out


def start_params(recipe, form):
    """Die Werte aus einem Formular: nur Namen, die das Rezept kennt, nur
    nicht leere Texte, gekürzt. Ein leerer Bedarf heißt „die einzige
    Instanz“ und geht nicht mit; der Host prüft alles noch einmal."""
    names = list((recipe.get("needs") or {})) + list((recipe.get("params") or {}))
    out = {}
    for n in names:
        if not PARAM_RE.match(n):
            continue
        spec = (recipe.get("params") or {}).get(n) or {}
        if spec.get("kind") == "bool":
            out[n] = "true" if form.get(n) in ("1", "true", "on") else "false"
            continue
        v = str(form.get(n) or "").strip()[:VALUE_MAX]
        if v:
            out[n] = v
    return out


def preview_rows(preview):
    rows = []
    for r in (preview or {}).get("rows") or []:
        text, tone = VERDICT.get(r.get("verdict"), (r.get("verdict", ""), "off"))
        rows.append({"id": r.get("id", ""), "type": build_view.STEP_TYPE.get(
            r.get("type"), r.get("type", "")), "verdict": text, "tone": tone,
            "note": r.get("note", "")})
    return rows


def preview_state(preview):
    """Was die Vorschau sagt: zu wählen, Halt, nichts zu tun, oder bereit."""
    if not preview:
        return "none"
    if preview.get("problems"):
        return "choose"
    rows = preview.get("rows") or []
    if any(r.get("verdict") in ("conflict", "refused") for r in rows):
        return "blocked"
    if all(r.get("verdict") in ("present", "skipped") for r in rows):
        return "complete"
    return "ready"


def recipes_needing(view, label, role, app_id):
    """Stufe 3 (RFC-0109 §7): die Rezepte, die diese App brauchen und die
    diese Person in diesem Mandanten ausführen darf -- für „Einrichten…“ auf
    der Seite einer Instanz. -> [{id, title, need}]"""
    out = []
    for r in recipes_for(view, label, role):
        if not r.get("runnable"):
            continue
        for n, spec in (r.get("needs") or {}).items():
            if (spec or {}).get("app") == app_id:
                out.append({"id": r["id"], "title": r.get("title") or r["id"], "need": n})
                break
    return out


def released_text(recipe):
    """„pxx, bec“ oder „niemandem (nur der Betreiber)“."""
    rel = recipe.get("released_for") or []
    return ", ".join(rel) if rel else "niemandem (nur der Betreiber)"


# --- Anfragen an den Betreiber (RFC-0109 §13, Stufe 3) ----------------------

TEXT_MAX = 2000


def requests_for(view, label):
    """Die Anfragen dieses Mandanten, neueste zuerst."""
    return [_request_row(r) for r in (view or {}).get("requests") or []
            if r.get("label") == label]


def open_requests(view):
    """Für den Betreiber auf „Aufbau“: die offenen Anfragen aller Mandanten."""
    return [_request_row(r) for r in (view or {}).get("requests") or [] if r.get("open")]


def _request_row(r):
    tone = ("err" if r.get("delivery") == "failed" else
            "ok" if r.get("released") or r.get("case_status") in ("answered", "closed") else
            "todo")
    return {"id": r.get("id", ""), "label": r.get("label", ""),
            "what": r.get("title") or r.get("recipe") or (r.get("text") or "")[:80],
            "recipe": r.get("recipe", ""), "text": r.get("text", ""),
            "by": r.get("by_name") or r.get("by", ""),
            "created": build_view._when(r.get("created")), "state": r.get("state", ""),
            "tone": tone, "case": r.get("case", ""), "note": r.get("note", ""),
            "replies": r.get("replies") or [], "open": bool(r.get("open"))}


def requestable(view):
    """Die Rezepte des Knotens, die man anfragen kann -- auch nicht
    freigegebene: genau dafür ist die Anfrage da. Nur Kennung und Titel."""
    return [{"id": r["id"], "title": r.get("title") or r["id"]}
            for r in (view or {}).get("recipes") or [] if not r.get("problem")]


def request_form(form, view):
    """(recipe, values, text) aus dem Formular, oder ein Satz, warum nicht.
    Der Host prüft alles noch einmal (Mandant aus dem Datensatz)."""
    rid = str(form.get("recipe") or "").strip()
    if rid and rid not in {r["id"] for r in requestable(view)}:
        return None, "Dieses Rezept gibt es auf dem Knoten nicht."
    text = str(form.get("text") or "").strip()
    if len(text) > TEXT_MAX:
        return None, f"Höchstens {TEXT_MAX} Zeichen."
    if not rid and not text:
        return None, "Bitte sagen, was gebraucht wird: ein Rezept oder ein paar Worte."
    values = {}
    for k in form:
        if k.startswith("v_") and PARAM_RE.match(k[2:]):
            v = str(form.get(k) or "").strip()[:VALUE_MAX]
            if v:
                values[k[2:]] = v
    return (rid, values, text), ""
