"""What the portal shows about one installed instance.

Two things live here: which instances belong on the launchpad (runtime
spec 2.10), and how the instance object page is cut into sections
(design guidelines 6.2.1/6.2.2).

Launchpad rule below; the sections rule in one sentence: **an object
page with more than three cards gets a reading head and tabs**, and the
server — not JavaScript — decides which tab is open.

Like `store_view.py`, this carries no Flask: these are RFC and design
decisions (RFC-0012 §1.2 and its §1.3 addendum) and should be readable
and testable without a request, a container or a node.

The launchpad rule in one sentence: **the app's own manifest decides,
the operator overrides.** An app that declares itself a background `service` gets no
tile, because a tile leading to a machine interface serves nobody. The
class comes from the manifest the node installed — never from a store
list, which may be disabled, unreachable, or written by a stranger.

What this is NOT is access control. A hidden tile changes nothing about
the instance's routes, roles or URL, and the gateway keeps enforcing
them on every request. Hiding an app from a person is what visibility
groups (RFC-0007) are for. Anything here that starts to feel like a
permission check is a bug.
"""

TILE_MODES = ("auto", "on", "off")
DEFAULT_APP_CLASS = "frontend"

# German, because it is rendered (design guidelines: German reference UI)
MODE_LABEL = {
    "auto": "Automatisch",
    "on": "Immer zeigen",
    "off": "Nie zeigen",
}
CLASS_LABEL = {
    "frontend": "App mit Oberfläche",
    "service": "Hintergrunddienst",
}


# The default tenant's label is the ABSENCE of a label in a hostname
# (oaap.core.tenant 1.2/2.4).
DEFAULT_TENANT_LABEL = "default"


def auto_host(name, inst, tenants, node_host):
    """The automatic address of an instance — the name the GATEWAY uses.

    `<instance>.<node>` in the default tenant, `<instance>.<label>.<node>`
    in every other one (oaap.core.tenant 2.4). The portal has to compute
    it exactly as the gateway writes it, or it prints and links a host
    that answers nowhere — which is what a tenant's launchpad tile did
    before this function existed.

    Three answers, and the third is the point:

    * a node with no external hostname has no automatic address at all;
    * an instance with no tenant reference is in the default tenant
      (2.5), and keeps the plain name;
    * an instance naming a tenant this node does NOT have gets **no**
      name. Fail closed, as at the gateway: the alternative is
      publishing a customer's app under the operator's own name.

    `tenants` is the node's tenant map, `{id: {"label": ...}}`.
    """
    if not node_host:
        return ""
    # The name the tenant chose, never the node key: an instance keyed
    # `cls-viewer` answers at `viewer.cls.<node>` (RFC-0025 §8.1). An
    # instance from before 0.1.58 has no stored name and its key IS its
    # name, which is why the fallback is exactly right.
    local = (inst or {}).get("name") or name
    ref = str((inst or {}).get("tenant") or "").strip()
    if not ref:
        return f"{local}.{node_host}"
    tenant = (tenants or {}).get(ref)
    if tenant is None:
        return ""
    label = tenant.get("label", "")
    if label and label != DEFAULT_TENANT_LABEL:
        return f"{local}.{label}.{node_host}"
    return f"{local}.{node_host}"


def app_class(inst):
    """The instance's declared class, normalised.

    Instances installed before this existed carry no class at all, and
    an unknown value is treated like a missing one — both mean
    `frontend`. That is the safe direction: a tile too many is untidy,
    a missing tile hides a working app from the person who installed it.
    """
    value = str(inst.get("app_class") or "").strip()
    return value if value in CLASS_LABEL else DEFAULT_APP_CLASS


def tile_mode(inst):
    """The operator's override; absent means "follow the app"."""
    mode = str(inst.get("tile") or "").strip()
    return mode if mode in TILE_MODES else "auto"


def class_phrase(inst):
    """How to talk about the class without overclaiming.

    Every app installed before manifest 0.2 declares nothing at all, and
    that is most of the fleet today. "Die App bezeichnet sich selbst
    als …" would be a small untruth about all of them, and small
    untruths on an admin page cost somebody an hour later.
    """
    declared = str(inst.get("app_class") or "").strip()
    if declared in CLASS_LABEL:
        return f"Die App bezeichnet sich selbst als {CLASS_LABEL[declared]}."
    if declared:
        return (f"Die App bezeichnet sich als „{declared}“ — das kennt diese "
                f"Plattform nicht, sie gilt deshalb als "
                f"{CLASS_LABEL[DEFAULT_APP_CLASS]}.")
    return ("Die App macht dazu keine Angabe und gilt deshalb als "
            f"{CLASS_LABEL[DEFAULT_APP_CLASS]}.")


def tile_visible(inst):
    """Does this instance get a launchpad tile?"""
    mode = tile_mode(inst)
    if mode != "auto":
        return mode == "on"
    return app_class(inst) != "service"


def tile_reason(inst):
    """One sentence for the instance page: why it is (not) on the launchpad.

    An operator looking for a missing app needs the reason, not just the
    state — otherwise the only way to find out is to read the source.
    """
    mode, shown = tile_mode(inst), tile_visible(inst)
    if mode != "auto":
        state = "eingeschaltet" if shown else "abgeschaltet"
        return (f"Die Kachel ist ausdrücklich {state}, unabhängig davon, was "
                f"die App über sich sagt. {class_phrase(inst)}")
    if not shown:
        return ("Die App bezeichnet sich selbst als Hintergrunddienst — sie "
                "wird von anderer Software benutzt, nicht von einem Menschen "
                "— und bekommt deshalb keine Kachel.")
    if str(inst.get("app_class") or "").strip() in CLASS_LABEL:
        return ("Die App bezeichnet sich selbst als Anwendung mit Oberfläche "
                "und erscheint deshalb im Launchpad.")
    return class_phrase(inst) + " Sie erscheint im Launchpad."


# --------------------------------------------------------------------
# Sections of the instance object page (design guidelines 6.2.2)

# Order is deliberate: read first ("Überblick" carries no form, so
# nobody lands in one), then the daily business, and the single
# irreversible action last and alone.
TABS = (
    ("ueberblick", "Überblick"),
    ("zugang", "Zugang"),
    ("netz", "Netz & Adressen"),
    ("deployment", "Deployment"),
    ("konfiguration", "Konfiguration"),
    ("verwaltung", "Verwaltung"),
)
DEFAULT_TAB = TABS[0][0]
TAB_KEYS = tuple(k for k, _ in TABS)


def valid_tab(raw, default=""):
    """The requested section, or the fallback.

    An unknown value is not an error worth a message — the tab comes
    from a link or a hidden field, so a wrong one means a stale
    bookmark, not a wrong decision. Falling back to the reading tab is
    the honest answer. The empty default means "no tab in the URL",
    which is what a redirect after a save uses.
    """
    tab = str(raw or "").strip()
    return tab if tab in TAB_KEYS else default


SOURCE_LABELS = {
    "git": "Git-Repository",
    "artifact": "Hochgeladenes Paket (ZIP)",
    "local": "Lokaler Pfad auf dem Knoten",
    "store": "Store-Eintrag",
}


def source_view(inst):
    """Where this instance's code came from: a label for the head and a
    few lines for the overview.

    An unknown kind is named as unknown rather than guessed, and an
    instance installed before the platform recorded its origin says so —
    „unbekannt" is a fact, an invented Git URL would be a lie.
    """
    src = inst.get("source") or {}
    kind = str(src.get("kind") or "")
    label = SOURCE_LABELS.get(kind, "Unbekannte Herkunft")
    lines = []
    if kind == "git":
        lines.append(f"Repository {src.get('url', '?')}")
        if src.get("path"):
            lines.append(f"Pfad im Repository: {src['path']}")
        lines.append(f"Branch oder Tag: {src.get('ref') or 'Standardbranch'}")
    elif kind == "artifact":
        if src.get("promoted_from"):
            # RFC-0020: „was läuft hier?" wird mit einem Teststand und
            # einer Prüfsumme beantwortet, nicht mit einer Versionshoffnung
            label = f"Aus dem Teststand „{src['promoted_from']}“ übernommen"
        lines.append(f"Version {src.get('version', '?')} "
                     f"aus {src.get('stored', '?')}")
        if src.get("received"):
            lines.append("Empfangen "
                         + src["received"].replace("T", " ").rstrip("Z"))
        if src.get("sha256"):
            lines.append(f"Prüfsumme {src['sha256'][:16]}…")
    elif kind == "local":
        lines.append(f"Verzeichnis {src.get('url', '?')}")
    elif not kind:
        lines.append("Diese Instanz wurde installiert, bevor die Plattform "
                     "die Herkunft festgehalten hat.")
    return label, lines


def route_rows(inst):
    """The app's routes with their roles, in words instead of JSON.

    `public` wins over everything else on a route: if one role is
    "no login at all", naming the others next to it would read as a
    restriction that does not exist.
    """
    rows = []
    for r in inst.get("routes") or []:
        roles = r.get("roles") or []
        if "public" in roles:
            who = "ohne Anmeldung (öffentlich)"
        elif roles:
            who = ", ".join(roles)
        else:
            who = "jede angemeldete Person"
        rows.append({"path": r.get("path", "/"), "who": who})
    return rows


def visibility_label(inst):
    """Who may see this instance, for the head (RFC-0007)."""
    groups = (inst.get("visibility") or {}).get("groups") or []
    return ("Gruppen " + ", ".join(groups)) if groups else "alle mit passender Rolle"


def hidden_instances(instances):
    """Names of instances that exist but carry no tile, sorted.

    The launchpad shows a `server_admin` how many there are. Without it,
    a node running only background services has a launchpad that is
    indistinguishable from a broken one (portal spec 2.2).
    """
    return sorted(name for name, inst in instances.items()
                  if not tile_visible(inst))


def grouped_tiles(tiles):
    """Tiles bucketed by their 'group' label (RFC-0036 D2), for display.

    Each tile dict is expected to carry a 'group' key (launchpad_tiles
    in app.py sets it from the instance's manifest-derived launchpad
    hint). Ungrouped tiles (label "") come first, in their OWN section
    with no heading — the exact rendering every tile had before this
    field existed, so an app that never sets launchpad.group looks
    unchanged. Labelled sections follow, sorted by label. Order WITHIN
    a section is whatever `tiles` already has (launchpad_tiles sorts by
    instance name) — grouping only buckets, it never reorders.
    """
    sections = {}
    for t in tiles:
        sections.setdefault(t.get("group") or "", []).append(t)
    ungrouped = sections.pop("", [])
    ordered = ([("", ungrouped)] if ungrouped else [])
    ordered += sorted(sections.items())
    return ordered


# --------------------------------------------------- mehrzeilige Konfiguration
#
# Manche Konfigurationswerte sind Listen: die Knoten von FleetView, die
# Bezugsquellen und Aliasse des KI-Gateways. Bis 0.1.48 bot das Portal
# dafür ein einzeiliges Eingabefeld an, und beide Apps erfanden
# unabhängig voneinander dieselbe Notlösung — Einträge mit ';' trennen.
# Zweimal dieselbe Notlösung ist das Zeichen, dass sie in die Plattform
# gehört.
#
# Warum die Zeilen NICHT als Zeilen gespeichert werden: Die Werte einer
# Instanz liegen in `instance.env` und gehen als `--env-file` an Docker.
# Beides ist zeilenweise — ein Zeilenumbruch im Wert zerreißt die Datei
# und würde beim nächsten Lesen still die halbe Konfiguration
# verschlucken. Deshalb bleibt die Übertragung einzeilig: Das Portal
# zeigt Zeilen, gespeichert wird die mit ';' verbundene Form. Apps, die
# heute schon an ';' und Zeilenumbruch trennen, ändern sich nicht.
LIST_SEPARATOR = ";"


def value_to_lines(value):
    """Gespeicherte Listenform -> was im Textfeld steht (eine Zeile je Eintrag)."""
    return "\n".join(part.strip() for part in (value or "").split(LIST_SEPARATOR)
                     if part.strip())


def lines_to_value(text):
    """Textfeld -> gespeicherte Listenform. Gibt (wert, fehler) zurück.

    Ein Eintrag, der selbst ein ';' enthält, wird **abgelehnt statt
    zerschnitten**: Stillschweigend zu zerteilen hieße, eine Adresse
    oder einen Schlüssel zu zerstören und den Anwender raten zu lassen,
    warum die App nichts mehr findet.
    """
    lines = [l.strip() for l in (text or "").replace("\r", "").split("\n")]
    lines = [l for l in lines if l]
    bad = [l for l in lines if LIST_SEPARATOR in l]
    if bad:
        return "", (f"Ein Eintrag darf kein '{LIST_SEPARATOR}' enthalten — "
                    f"damit werden die Einträge getrennt. Betroffen: {bad[0]!r}")
    return LIST_SEPARATOR.join(lines), ""


# ------------------------------------------------- Generalprobe (RFC-0030)
#
# Eine Generalprobe traegt den Code der Testinstanz auf einer **Kopie der
# Produktivdaten** (Runtime-Spec 2.15). Ein Mensch darf sie nie mit der
# Produktion verwechseln — deshalb ein eigenes Abzeichen und die
# Restlaufzeit daneben, in der Liste wie im Objektkopf.
#
# Die Rechnung steht hier und nicht im Host, weil sie eine ANZEIGE ist:
# der Host loescht nach `expires`, das Portal sagt, wie lange es noch
# hin ist. Beide lesen dasselbe Feld, keiner das Ergebnis des anderen.

def is_rehearsal(inst):
    return bool((inst or {}).get("rehearsal"))


def rehearsal_view(inst, now=None):
    """Was ueber eine Generalprobe zu sehen sein muss, oder None.

    Gibt Abzeichen-Text, Restlaufzeit in Worten und die beiden Instanzen
    zurueck, aus denen sie zusammengesetzt ist. Ein unlesbares Datum
    sagt „unbekannt" statt „abgelaufen": Eine Seite, die faelschlich
    „abgelaufen" behauptet, laesst jemanden eine Generalprobe wegwerfen,
    die noch laeuft.
    """
    if not is_rehearsal(inst):
        return None
    import datetime
    r = inst["rehearsal"]
    now = now or datetime.datetime.now(datetime.timezone.utc)
    try:
        due = datetime.datetime.strptime(r.get("expires", ""),
                                         "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc)
    except ValueError:
        due = None
    if due is None:
        left, short = "Restlaufzeit unbekannt", "?"
    elif due <= now:
        left, short = "abgelaufen — wird beim naechsten Lauf entfernt", "abgelaufen"
    else:
        days = (due - now).days
        hours = int((due - now).total_seconds() // 3600)
        if days >= 1:
            left = f"noch {days} Tag{'e' if days != 1 else ''}"
            short = f"{days} d"
        else:
            left = f"noch {hours} Stunde{'n' if hours != 1 else ''}"
            short = f"{hours} h"
    return {
        "badge": "Generalprobe",
        "left": left,
        "left_short": short,
        "expires": (r.get("expires") or "").replace("T", " ").rstrip("Z"),
        "of": r.get("of", ""),
        "code_from": r.get("code_from", ""),
        "archive": r.get("archive", ""),
        "archive_created": (r.get("archive_created") or "").replace("T", " ").rstrip("Z"),
        "extensions": int(r.get("extensions") or 0),
        "expired": bool(due is not None and due <= now),
    }


def rehearsal_note(view):
    """Der Satz, den die Seite einer Generalprobe schuldet.

    Nicht „das ist eine Testkopie" — das waere beruhigend und falsch.
    Was hier liegt, sind echte Kundendaten.
    """
    if not view:
        return ""
    return ("Diese Instanz traegt eine Kopie der Produktivdaten von "
            + "„" + view["of"] + "“ mit dem Code aus "
            + "„" + view["code_from"] + "“. Sie erreicht nach "
            "aussen nichts: keine eigene Adresse, keine oeffentliche Route, "
            "keine App-Verknuepfungen und keine uebernommenen Geheimnisse. "
            "Sie wird mitsamt ihren Daten geloescht, wenn ihre Zeit um ist.")


def _mb(n):
    """Eine Groesse, wie ein Mensch sie liest — und nie als „0.0 MB".

    Am laufenden Knoten aufgefallen (0.1.80): eine kleine Instanz stand
    mit „Die Kopie belegt etwa 0.0 MB" auf der Seite. Das ist eine
    kleine Unwahrheit ueber etwas, das nicht leer ist — und kleine
    Unwahrheiten auf einer Admin-Seite kosten spaeter jemandem eine
    Stunde.
    """
    try:
        n = int(n)
    except (TypeError, ValueError):
        return ""
    if n >= 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n} Byte"


def _short(value):
    """Ein ISO-Stempel, wie ein Mensch ihn liest — alles andere unveraendert."""
    s = str(value or "")
    if len(s) >= 16 and s[4] == "-" and s[7] == "-" and s[10] in "T ":
        return s[:16].replace("T", " ")
    return s


def age_phrase(stamp, now=None):
    """„heute" / „3 Tage alt" — das Alter eines Archivs, ausgesprochen.

    Eine Generalprobe auf einem zwei Wochen alten Archiv ist eine
    Generalprobe auf zwei Wochen alten Daten. Wer ihr Ergebnis liest,
    muss wissen, welche Daten es waren. Ein unlesbarer Stempel sagt
    nichts, statt ein Alter zu erfinden.
    """
    import datetime
    s = str(stamp or "")
    try:
        when = datetime.datetime.strptime(
            s[:19].replace(" ", "T"), "%Y-%m-%dT%H:%M:%S").replace(
            tzinfo=datetime.timezone.utc)
    except ValueError:
        return ""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    days = (now - when).days
    if days <= 0:
        return "heute"
    return f"{days} Tag{'e' if days != 1 else ''} alt"


def rehearsal_offer(name, inst, view, local=""):
    """Das Angebot „Generalprobe anlegen", oder None (RFC-0030 D5).

    None fuer alles, was keine Produktiv-Instanz ist: Eine Test-Instanz
    hat schon Testdaten, und eine Generalprobe einer Generalprobe waere
    eine Kopie einer Kopie.

    `view` ist die Ansicht, die der HOST neben die Registry schreibt —
    das Portal darf den Mandantenbaum nicht lesen, dort liegt jede
    `instance.env` jedes Kunden. Fehlt die Ansicht, sagt die Seite das:
    Ein Knoten, der noch nie gemessen hat, **leiht sich keine Zahl** —
    eine Zahl aus einem Handbuch ist immer die Maschine von jemand
    anderem (dieselbe Regel wie beim Sicherungs-Zeitplan, RFC-0029 D1).
    """
    if inst.get("channel") != "production" or is_rehearsal(inst):
        return None
    view = view or {}
    mine = (view.get("instances") or {}).get(name) or {}
    archives = view.get("archives") or []
    kb, free = mine.get("kbytes"), view.get("free_kbytes")
    base = local or name
    return {
        "candidates": mine.get("code") or [],
        "archives": [{"file": a.get("file", ""),
                      "created": _short(a.get("created")),
                      "size": _mb(a.get("bytes"))} for a in archives[:5]],
        "newest": _short(archives[0].get("created")) if archives else "",
        "newest_age": age_phrase(archives[0].get("created")) if archives else "",
        "measured": _short(view.get("written")),
        "size": _mb(kb * 1024) if kb else "",
        "free": _mb(free * 1024) if free else "",
        "after": (_mb(max(0, free - kb) * 1024)
                  if (kb is not None and free is not None) else ""),
        # Dieselbe Schwelle wie im Host (rehearsal_review): 20 % Luft.
        # Die Seite sagt es vorher, der Knoten lehnt es ab — beide reden
        # ueber dieselbe Zahl, statt dass eine Seite hofft.
        "tight": bool(kb and free is not None and free < kb * 12 // 10),
        "days": view.get("default_days") or 7,
        "suggestion": f"{base}-probe",
    }
