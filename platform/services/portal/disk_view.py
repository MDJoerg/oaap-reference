"""Die Seite „Datenträger“ (oaap.data.disks 0.2, RFC-0063 Stufe 2): was sie zeigt.

Gelesen wird `disk-view.json`, vom Host geschrieben: Namen, die der Betreiber
vergeben hat (Platten, Quellen, Freigaben, Instanzen), Nummern, Größen,
Formate, Zustände -- nie ein Pfad, nie ein Dateiname, nie ein Schlüssel
(D7). `disks.json` liest das Portal nie. Geändert wird nur über Aufträge
(Aktion `disk`), die der Host noch einmal prüft. Hier steht, wie die Ansicht
dargestellt wird und wie aus einem Formular ein Auftrag wird -- ohne Flask
und ohne Anfrage, damit man es ohne Portal prüfen kann (wie `build_view.py`).
"""
import re

NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,39}$")
UUID_RE = re.compile(r"^[0-9A-Fa-f-]{4,64}$")
SLOW_USB = 5000          # Mbit/s; darunter warnt die Seite

STATE = {"open": ("Geöffnet", "ok"), "present": ("Angesteckt, zu", "todo"),
         "absent": ("Nicht angesteckt", "off")}
ROLE = {"raw": "Rohdaten (nur lesen)", "work": "Arbeitsplatte"}
ENCRYPTION = {"luks": "LUKS", "none": "keine"}
KIND = {"archive": "Archiv", "path": "Ordner"}
ARRIVED = {"ok": ("angekommen", "ok"), "missing": ("nicht im Container", "todo"),
           "mismatch": ("falscher Modus", "todo"), None: ("Container läuft nicht", "off")}


def size(n):
    """„1,2 GB“; leer, wenn unbekannt."""
    if n is None:
        return ""
    n = float(n)
    for unit, f in (("TB", 1e12), ("GB", 1e9), ("MB", 1e6), ("kB", 1e3)):
        if n >= f:
            return f"{n / f:.1f} {unit}".replace(".", ",")
    return f"{int(n)} B"


def _when(iso):
    s = str(iso or "")
    return (s[:10] + " " + s[11:16]).strip() if len(s) >= 16 else s[:10]


def disks(view):
    out = []
    for d in (view or {}).get("disks") or []:
        label, tone = STATE.get(d.get("state"), (d.get("state") or "?", "off"))
        speed = d.get("usb_speed") or 0
        out.append({
            "name": d.get("name", ""), "role": d.get("role", ""),
            "role_label": ROLE.get(d.get("role"), d.get("role", "")),
            "encryption": ENCRYPTION.get(d.get("encryption"), d.get("encryption", "")),
            "uuid": d.get("uuid", ""), "label": d.get("label", ""),
            "model": d.get("model", ""),
            "state": d.get("state", ""), "state_label": label, "tone": tone,
            # Eingehängt, aber das Gerät ist weg: das Kabel wurde gezogen,
            # ohne auszuwerfen (gemessen 2026-10-22). Die Plattform merkt das
            # erst in Stufe 3 selbst; bis dahin sagt es die Seite.
            "stale": bool(d.get("stale")),
            "size": size(d.get("size")), "free": size(d.get("free")),
            "usb": f"USB {speed} Mbit/s" if speed else "",
            "slow": bool(speed and speed < SLOW_USB and d.get("in_use")),
            "in_use": bool(d.get("in_use")),
            "can_open": d.get("state") == "present",
            "can_eject": d.get("state") == "open" or bool(d.get("stale")),
            "can_scan": d.get("state") == "open" and not d.get("stale"),
            "formattable": d.get("role") == "work" and d.get("state") == "present"
                           and not d.get("in_use"),
        })
    return out


def devices(view):
    out = []
    for d in (view or {}).get("devices") or []:
        speed = d.get("usb_speed") or 0
        out.append({"uuid": d.get("uuid", ""), "fstype": d.get("fstype", ""),
                    "size": size(d.get("size")), "label": d.get("label", ""),
                    "model": d.get("model", ""), "luks": bool(d.get("luks")),
                    "usb": f"USB {speed} Mbit/s" if speed else ""})
    return out


def _verify_text(v, running):
    if running:
        return "Prüfung läuft"
    if not v:
        return "noch nie geprüft"
    t = f"{v.get('latest', '')[:8]}: {v.get('files', 0)} Dateien"
    if "against" in v:
        changed = (v.get("different") or 0) + (v.get("new") or 0) + (v.get("missing") or 0)
        t += (" – unverändert" if not changed else
              f" – {v.get('different', 0)} anders, {v.get('new', 0)} neu, "
              f"{v.get('missing', 0)} fehlen")
    return t


def sources(view):
    out = []
    for s in (view or {}).get("sources") or []:
        v = s.get("verify") or {}
        changed = "against" in v and ((v.get("different") or 0) + (v.get("new") or 0)
                                      + (v.get("missing") or 0))
        out.append({
            "name": s.get("name", ""), "kind": KIND.get(s.get("kind"), s.get("kind", "")),
            "disk": s.get("disk", ""), "format": s.get("format", ""),
            "index_on": s.get("index_on", ""),
            "index_size": size(s.get("index_size")),
            "available": bool(s.get("available")),
            "verify": _verify_text(v, s.get("verify_running")),
            "verify_alarm": bool(changed),
            "grants": [dict(g, arrived_label=ARRIVED.get(g.get("arrived"), ARRIVED[None])[0],
                            arrived_tone=ARRIVED.get(g.get("arrived"), ARRIVED[None])[1])
                       for g in s.get("grants") or []],
        })
    return out


def scans(view):
    """Je Platte die Dateien der letzten Durchsuchung: Nummer, Größe, Format,
    und ob daraus schon eine Quelle wurde. Ohne Namen."""
    out = {}
    for name, sc in ((view or {}).get("scans") or {}).items():
        out[name] = {"written": _when(sc.get("written")), "count": sc.get("count", 0),
                     "files": [{"nr": f.get("nr"), "size": size(f.get("size")),
                                "format": f.get("format", ""),
                                "source": f.get("source", ""),
                                "addable": not f.get("source") and f.get("format") in ARCHIVES}
                               for f in sc.get("files") or []]}
    return out


ARCHIVES = ("tar", "gzip", "bzip2", "xz", "zstd", "zip")


def instances(view):
    return list((view or {}).get("instances") or [])


def grants_of(view, instance, recorded):
    """Die Zeile für die Instanzseite: „roh (ro), arbeit (rw)“ mit Ankunft.
    `recorded` ist `disk_grants` aus dem Register; die Ansicht ergänzt, ob
    die Freigabe im Container angekommen ist."""
    seen = {}
    for s in (view or {}).get("sources") or []:
        for g in s.get("grants") or []:
            if g.get("instance") == instance:
                seen[g.get("grant")] = g
    out = []
    for g in recorded or []:
        if not isinstance(g, dict) or not g.get("grant"):
            continue
        v = seen.get(g["grant"]) or {}
        label, tone = ARRIVED.get(v.get("arrived"), ARRIVED[None]) if v else ("unbekannt", "off")
        out.append({"grant": g["grant"], "source": g.get("source", ""),
                    "mode": "rw" if g.get("rw") else "ro",
                    "optional": bool(g.get("optional")),
                    "arrived": label, "tone": tone})
    return out


# --------------------------------------------------- aus dem Formular

def _name(form, key, what):
    v = (form.get(key) or "").strip()
    if not NAME_RE.match(v):
        raise ValueError(f"{what}: Kleinbuchstaben, Ziffern und „-“, mit einem "
                         "Buchstaben vorn, höchstens 40 Zeichen.")
    return v


def request_args(verb, form, view):
    """Formular -> die Felder des Auftrags, oder ValueError mit dem Satz für
    die Seite. Geprüft wird gegen die Ansicht, damit eine falsche Eingabe
    gar nicht erst in den Spool kommt; der Host prüft alles noch einmal."""
    known_disks = {d.get("name"): d for d in (view or {}).get("disks") or []}
    known_sources = {s.get("name"): s for s in (view or {}).get("sources") or []}

    def disk():
        d = (form.get("disk") or "").strip()
        if d not in known_disks:
            raise ValueError("Diese Platte gibt es nicht.")
        return d

    if verb == "adopt":
        uuid = (form.get("uuid") or "").strip()
        if not UUID_RE.match(uuid) or uuid not in {
                x.get("uuid") for x in (view or {}).get("devices") or []}:
            raise ValueError("Dieses Gerät ist nicht (mehr) angesteckt.")
        role = form.get("role", "")
        if role not in ("raw", "work"):
            raise ValueError("Bitte die Rolle wählen: Rohdaten oder Arbeitsplatte.")
        return {"uuid": uuid, "name": _name(form, "name", "Name der Platte"), "role": role}
    if verb in ("open", "eject", "scan"):
        return {"disk": disk()}
    if verb == "format":
        d = disk()
        if known_disks[d].get("role") != "work":
            raise ValueError("Nur eine Arbeitsplatte wird formatiert; Rohdaten nie.")
        mode = form.get("mode", "")
        if mode not in ("luks", "plain"):
            raise ValueError("Bitte wählen: verschlüsselt (LUKS) oder ohne.")
        typed = (form.get("confirm_uuid") or "").strip()
        if typed.lower() != (known_disks[d].get("uuid") or "").lower():
            raise ValueError("Die eingegebene UUID passt nicht zur Platte; "
                             "nichts wurde formatiert.")
        return {"disk": d, "mode": mode, "confirm_uuid": typed}
    if verb == "archive-add":
        d = disk()
        try:
            nr = int(form.get("nr") or "")
        except ValueError:
            raise ValueError("Bitte eine Datei aus der Liste wählen.") from None
        out = {"disk": d, "nr": nr, "name": _name(form, "name", "Name der Quelle"),
               "recursive": form.get("recursive") == "1"}
        if form.get("index_on"):
            out["index_on"] = _name(form, "index_on", "Platte für den Index")
        return out
    if verb == "grant":
        s = (form.get("source") or "").strip()
        if s not in known_sources:
            raise ValueError("Diese Quelle gibt es nicht.")
        inst = (form.get("instance") or "").strip()
        if inst not in instances(view):
            raise ValueError("Bitte eine Instanz wählen.")
        out = {"source": s, "instance": inst,
               "as": _name(form, "as", "Name im Container"),
               "rw": form.get("rw") == "1", "optional": form.get("optional") == "1"}
        own = (form.get("owner") or "").strip()
        if own:
            if not own.isdigit():
                raise ValueError("Der Eigentümer ist eine Zahl (uid), z. B. 1000.")
            out["owner"] = int(own)
        return out
    if verb == "revoke":
        inst, g = (form.get("instance") or "").strip(), (form.get("grant") or "").strip()
        if not any(x.get("instance") == inst and x.get("grant") == g
                   for s in known_sources.values() for x in s.get("grants") or []):
            raise ValueError("Diese Freigabe gibt es nicht.")
        return {"instance": inst, "grant": g}
    if verb == "verify":
        s = (form.get("source") or "").strip()
        if s not in known_sources:
            raise ValueError("Diese Quelle gibt es nicht.")
        return {"source": s}
    raise ValueError("Unbekannte Aktion.")


VERBS = ("adopt", "open", "eject", "format", "scan", "archive-add", "grant",
         "revoke", "verify")
