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
PROSE_MAX = 8000          # RFC-0117: tenant_recipe.PROSE_MAX
LIGHT_WORDS = (("green", "grün"), ("yellow", "gelb"), ("red", "rot"))
LIGHT_DE_ALL = {"green": "grün", "yellow": "gelb", "red": "rot"}
VERDICT = {"present": ("vorhanden", "ok"), "absent": ("wird angelegt", "todo"),
           "conflict": ("Konflikt", "err"), "skipped": ("nicht gewünscht", "off"),
           "refused": ("abgelehnt", "err"),
           # Stufe 3: hinter einem Schritt, der auf einen Menschen wartet
           "later": ("erst danach prüfbar", "off")}


def recipes_for(view, label, role):
    """Die Rezepte, die diese Person in diesem Mandanten sieht."""
    out = []
    for r in (view or {}).get("recipes") or []:
        # RFC-0119 D2: a template an app brought is seen only at its tenant
        if r.get("tenant") and r.get("tenant") != label:
            continue
        if r.get("problem"):
            if role == "server_admin":
                out.append(dict(r, runnable=False))
            continue
        # RFC-0117 D4: a free template needs no release
        mine = label in (r.get("released_for") or []) or bool(r.get("free"))
        # RFC-0119 section 4.5: the operator's block beats a release
        if r.get("blocked"):
            mine = False
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


def light_options(chosen, suggested="", proposal=""):
    """Die drei Ampeln; `proposal` "app" (RFC-0119 §4.4): der Vorschlag kommt
    von der App, die die Vorlage mitbrachte, und ist nie vorausgewählt."""
    who = " (Vorschlag der App)" if proposal == "app" else " (vom Modell vorgeschlagen)"
    return [{"value": v, "text": t + (who if v == suggested else ""),
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
        # RFC-0117 Stufe 2: Werte aus einer Datei (Import) füllen, was die Person formen darf
        for n, spec in (recipe.get("params") or {}).items():
            v = (prefill or {}).get(n)
            if spec.get("shape") is True and isinstance(v, str) and v.strip() and PARAM_RE.match(n):
                cap = _prose_max(spec) if spec.get("kind") == "prose" else VALUE_MAX
                used[n] = v.replace(chr(13) + chr(10), chr(10)).strip()[:cap]
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
            f["options"] = light_options(f["value"], ch.get("suggested", ""),
                                         ch.get("proposal", ""))
            f["proposal"] = ch.get("proposal", "")
            f["suggested"] = ch.get("suggested", "")
            f["current"] = ch.get("current", "")
            f["current_text"] = LIGHT_DE_ALL.get(ch.get("current", ""), "")
            f["suggested_text"] = LIGHT_DE_ALL.get(ch.get("suggested", ""), "")
        elif kind == "bool":
            f["checked"] = f["value"] in ("true", "yes", "ja", "1", "True")
        elif kind == "prose":
            f["max"] = _prose_max(spec)
            f["default"] = str(spec.get("default") or "")
        f["shape"] = spec.get("shape") is True
        # RFC-0119 §4.4: eine Ampel ohne Vorgabe und ohne `from` (die der
        # eigenen Quelle einer App) fragt der Assistent die Person
        f["asked"] = not f["shape"] and "default" not in spec and kind != "bool" and (
            kind != "light" or not spec.get("from"))
        out.append(f)
    return out


def _prose_max(spec):
    mx = spec.get("max", PROSE_MAX)
    return mx if isinstance(mx, int) and 1 <= mx <= PROSE_MAX else PROSE_MAX


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
        if spec.get("kind") == "prose":
            # RFC-0117: lines stay lines (a browser sends CRLF); the host
            # judges the length again -- cut one past it, so it says why
            v = str(form.get(n) or "").replace("\r\n", "\n").replace("\r", "\n").strip()
            if v:
                out[n] = v[:_prose_max(spec) + 1]
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


# --- Vorlagen für Assistenten (RFC-0117 Stufe 1) ---------------------------

LIGHT_DE = dict(LIGHT_WORDS)


def templates_for(view, label, role):
    """Die Karten der Seite „Assistenten“: jedes Rezept des Knotens, das
    einen Assistenten anbietet. `usable`: diese Person darf es hier
    starten (frei, freigegeben, oder der Betreiber); sonst steht dort
    „braucht Freigabe“ und der Weg ist die Anfrage."""
    runnable = {r["id"] for r in recipes_for(view, label, role) if r.get("runnable")}
    out = []
    for r in (view or {}).get("recipes") or []:
        if r.get("problem") or r.get("offers") != "assistant":
            continue
        if r.get("tenant") and r.get("tenant") != label:
            continue
        card = r.get("card") or {}
        light = card.get("light") if card.get("light") in LIGHT_DE else ""
        out.append({"id": r["id"], "title": card.get("title") or r.get("title") or r["id"],
                    "summary": card.get("summary") or "", "light": light,
                    "light_text": LIGHT_DE.get(light, ""), "icon": card.get("icon") or "",
                    "version": r.get("version", ""), "usable": r["id"] in runnable,
                    "free": bool(r.get("free")),
                    # RFC-0119 §4.5: vom Betreiber gesperrt
                    "blocked": bool(r.get("blocked")),
                    # RFC-0119: die App, die die Vorlage mitbrachte
                    "app": (r.get("app") or {}).get("name") or "",
                    "app_instance": (r.get("app") or {}).get("instance") or "",
                    "installs": [nd.get("label") or n for n, nd in (r.get("needs") or {}).items()
                                 if nd.get("install")]})
    # erst die Vorlagen der Plattform, dann die aus Apps; je erst was man
    # starten kann, dann nach Titel
    return sorted(out, key=lambda c: (bool(c["app"]), not c["usable"], c["app"].lower(),
                                      c["title"].lower()))


def find_template(view, rid, label, role):
    r = find_recipe(view, rid, label, role)
    return r if r and r.get("offers") == "assistant" else None


def wizard_parts(recipe, preview=None, prefill=None):
    """Die Schritte 2 und 3 des Assistenten: (needs, asked, shaped).

    needs: die Apps (vorhanden / wird installiert); asked: Werte ohne
    Vorgabe, die nicht zum Anpassen gehören (z. B. die Adresse des
    Suchdienstes); shaped: was die Person formen darf (`shape: true`).
    Alles andere setzt die Vorlage selbst."""
    fs = fields(recipe, preview, prefill)
    return ([f for f in fs if f["kind"] == "need"],
            [f for f in fs if f["kind"] != "need" and f.get("asked")],
            [f for f in fs if f["kind"] != "need" and f.get("shape")])


IMPORT_MAX = 20                 # Assistenten je Datei
FROM_RE = re.compile(r"^recipe:([a-z0-9][a-z0-9-]{0,62})@([0-9]{1,4})$")


def import_plan(view, doc, label, role):
    """RFC-0117 §4 (Stufe 2): was aus einer Austauschdatei wird, je Assistent.

    Mit `from` und einer Vorlage, die diese Person hier starten darf (gleiche
    Hauptversion): die Vorlage, vorausgefüllt mit Titel, Kennung, Begrüßung,
    Anweisungen und Modell der Datei -- Sicht, Zugang und Modellfreigabe kommen
    dann mit. Sonst ein Satz; ohne `from` der bisherige Weg (Chat oder MCP-Gateway,
    das ihn nur nimmt, wenn seine Sicht dort steht). Gespeichert wird hier nichts.
    -> (rows, problem)"""
    if not isinstance(doc, dict) or doc.get("v") != 1 or not isinstance(doc.get("assistants"), list):
        return [], 'Das ist keine Austauschdatei für Assistenten ({"v": 1, "assistants": [...]}).'
    items = [a for a in doc["assistants"] if isinstance(a, dict)]
    if not items:
        return [], "Die Datei enthält keinen Assistenten."
    if len(items) > IMPORT_MAX:
        return [], f"Höchstens {IMPORT_MAX} Assistenten je Datei."
    rows = []
    for a in items:
        title = str(a.get("title") or a.get("id") or "?")[:80]
        row = {"id": str(a.get("id") or "")[:48], "title": title, "view": str(a.get("view") or "")[:48],
               "rid": "", "prefill": {}, "sentence": ""}
        m = FROM_RE.match(str(a.get("from") or ""))
        if not m:
            row["sentence"] = ("Ohne Vorlage exportiert. Im Chat oder im MCP-Gateway importieren; "
                               + (f"das Gateway nimmt ihn nur, wenn es dort die Sicht „{row['view']}“ gibt."
                                  if row["view"] else "er spricht ohne Werkzeuge."))
            rows.append(row)
            continue
        rid, major = m.group(1), int(m.group(2))
        known = next((r for r in (view or {}).get("recipes") or []
                      if r.get("id") == rid and not r.get("problem")), None)
        recipe = find_template(view, rid, label, role)
        if known is None or known.get("offers") != "assistant":
            row["sentence"] = f"Die Vorlage „{rid}“ gibt es auf diesem Knoten nicht."
        elif int(known.get("major") or 0) != major:
            row["sentence"] = (f"Die Datei kommt aus „{rid}“ Hauptversion {major}; hier liegt "
                               f"{known.get('version')} -- die Werte passen vielleicht nicht, darum "
                               "nicht vorausgefüllt.")
        elif recipe is None or not any(c["id"] == rid and c["usable"]
                                       for c in templates_for(view, label, role)):
            row["sentence"] = (f"Die Vorlage „{rid}“ braucht hier die Freigabe des Betreibers -- "
                               "auf der Seite Assistenten anfragen.")
        else:
            amap = known.get("assistant_map") or {}
            row["rid"] = rid
            row["template"] = (known.get("card") or {}).get("title") or rid
            row["prefill"] = {p: str(a[f]) for f, p in amap.items()
                              if isinstance(a.get(f), str) and a[f].strip()}
        rows.append(row)
    return rows, ""


def _vkey(v):
    m = re.match(r"^([0-9]+)\.([0-9]+)\.([0-9]+)", str(v or ""))
    return tuple(int(x) for x in m.groups()) if m else (-1, -1, -1)


def template_runs(view, label):
    """Die Läufe dieses Mandanten, die von einer Vorlage kamen -- auch die
    einer App, die es nicht mehr gibt (RFC-0119 Stufe 2). `mark`: was die
    Seite zum Assistenten sagt (RFC-0119 §5/§6); nichts ändert sich von selbst."""
    here = {r["id"]: r for r in (view or {}).get("recipes") or []
            if r.get("offers") == "assistant" and not r.get("problem")
            and (not r.get("tenant") or r.get("tenant") == label)}
    raw = {st.get("id"): st for st in (view or {}).get("runs") or []}
    out = []
    for row in runs_for(view, label):
        st = raw.get(row["id"]) or {}
        app = st.get("app") or {}
        if row["recipe"] not in here and not app:
            continue
        row = dict(row, app=app.get("name", ""), mark="", mark_tone="")
        cur = here.get(row["recipe"])
        if st.get("state") == "done":
            if cur is None:
                row.update(mark="Vorlage nicht mehr da – die App wurde entfernt; "
                                "ihre Werkzeuge gibt es nicht mehr", mark_tone="err")
            elif _vkey(cur.get("version")) > _vkey(st.get("version")):
                row.update(mark=f"Vorlage neuer ({cur.get('version')}) – der Assistent "
                                "bleibt, wie er ist", mark_tone="todo")
        out.append(row)
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
    tone = ("err" if r.get("delivery") == "failed" or r.get("verdict") == "rejected" else
            "ok" if r.get("released") or r.get("verdict") == "done"
            or r.get("case_status") in ("answered", "closed") else
            "todo")
    return {"id": r.get("id", ""), "label": r.get("label", ""),
            "what": r.get("title") or r.get("recipe") or (r.get("text") or "")[:80],
            "recipe": r.get("recipe", ""), "text": r.get("text", ""),
            "by": r.get("by_name") or r.get("by", ""),
            "created": build_view._when(r.get("created")), "state": r.get("state", ""),
            "tone": tone, "case": r.get("case", ""), "note": r.get("note", ""),
            "replies": r.get("replies") or [], "open": bool(r.get("open"))}


def find_open_request(view, rid):
    return next((r for r in (view or {}).get("requests") or []
                 if r.get("id") == rid and r.get("open")), None)


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
