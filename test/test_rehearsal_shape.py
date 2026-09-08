#!/usr/bin/env python3
"""Die Form der Generalprobe: was sie NICHT darf (RFC-0030, Spec 2.15).

Eine Generalprobe traegt eine **Kopie der Produktivdaten**. Gefaehrlich
ist daran nicht das Lesen, sondern das Handeln: die echte Mahnung
verschicken, den echten Webhook rufen, in die produktive Nachbar-App
schreiben. Deshalb sind die vier Verweigerungen das eigentliche Produkt
dieses Schritts und nicht sein Beiwerk — und deshalb steht diese
Pruefung VOR der, die die Daten kopiert.

Geprueft wird gegen ein Original, das **all das hat**: eigene Adresse,
Alias, oeffentliche Route, App-Verknuepfung und einen gesetzten
`secret: true`-Wert. Gegen ein Original ohne diese Dinge wuerde man nur
pruefen, dass aus nichts nichts wird.

Die fuenf Regeln:

1. keine eigene Adresse und kein Alias (Spec 2.15.2),
2. keine oeffentliche Route — auch eine im Manifest als `public`
   erklaerte verlangt Anmeldung, und zwar an JEDEM Eingang,
3. keine App-Verknuepfungen, in keine Richtung,
4. die gespeicherten Routen werden NICHT umgeschrieben (sonst laege
   eine unwahre Kopie des Manifests herum),
5. eine Generalprobe ist ein Urteil, keine Quelle: kein Uebernehmen
   heraus, kein erneutes Ausrollen hinein.

Aufruf: python3 test/test_rehearsal_shape.py
"""
import argparse
import contextlib
import datetime
import io as _io
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-rehearsal-test-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, os.path.join(HERE, "..", "platform"))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services", "portal"))

import appctl as m                                            # noqa: E402
import instance_view as iv                                    # noqa: E402

m.reload_gateway = lambda: None
os.makedirs(m.CADDY_APPS_DIR, exist_ok=True)

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:500]}")


ERR = []
_die = m.die


def _capture_die(msg):
    ERR.append(str(msg))
    _die(msg)


m.die = _capture_die


def dies(fn, *a, **kw):
    """Ruft auf und gibt die Ablehnung zurueck ('' wenn keine kam)."""
    del ERR[:]
    try:
        with contextlib.redirect_stdout(_io.StringIO()):
            fn(*a, **kw)
    except SystemExit:
        return ERR.pop() if ERR else "(ohne Text)"
    return ""


ROUTES = [{"path": "/", "roles": ["keyuser"]},
          {"path": "/hook", "roles": ["public"]}]


def record(**over):
    base = {
        "app_id": "crm", "app_name": "CRM", "version": "1.4.0",
        "channel": "production", "port": 8101, "container": "oaap-app-crm",
        "image": "oaap-app/crm:1.4.0", "svc_port": 8000,
        "services": [{"service": "", "container": "oaap-app-crm",
                      "image": "oaap-app/crm:1.4.0", "build": "", "port": 8000}],
        "routes": [dict(r) for r in ROUTES],
        "storage": [], "config": [
            {"key": "SMTP_PASSWORD", "label": "Mailpasswort", "secret": True,
             "multiline": False, "generate": "", "default": ""}],
        "roles": ["keyuser"], "visibility": {},
        "source": {"kind": "artifact", "version": "1.4.0",
                   "stored": "1.4.0-a.zip", "sha256": "a" * 64, "path": ""},
    }
    base.update(over)
    return base


print("")
print("Ein Original, das alles hat, was nicht mitkommen darf")

tid = m.ensure_default_tenant()
reg = m.load_registry()
reg["instances"]["crm"] = record(
    tenant=tid, id="aaaaaaaaaaaa", name="crm",
    address="crm.example.org", aliases=["kunden.example.org"],
    links=["lager"])
reg["instances"]["lager"] = record(
    tenant=tid, id="bbbbbbbbbbbb", name="lager", app_id="lager",
    app_name="Lager", port=8102, container="oaap-app-lager",
    services=[{"service": "", "container": "oaap-app-lager",
               "image": "oaap-app/lager:1.0.0", "build": "", "port": 8000}])
reg["instances"]["crm-test"] = record(
    tenant=tid, id="cccccccccccc", name="crm-test", channel="test", port=8103,
    container="oaap-app-crm-test", version="1.5.0",
    services=[{"service": "", "container": "oaap-app-crm-test",
               "image": "oaap-app/crm:1.5.0", "build": "", "port": 8000}],
    source={"kind": "artifact", "version": "1.5.0", "stored": "1.5.0-b.zip",
            "sha256": "b" * 64, "path": ""})

now = datetime.datetime.now(datetime.timezone.utc)
reg["instances"]["crm-probe"] = record(
    tenant=tid, id="dddddddddddd", name="crm-probe", port=8104,
    container="oaap-app-crm-probe", version="1.5.0",
    services=[{"service": "", "container": "oaap-app-crm-probe",
               "image": "oaap-app/crm:1.5.0", "build": "", "port": 8000}],
    rehearsal={"of": "crm", "code_from": "crm-test",
               "archive": "/var/backups/oaap/oaap-backup-x.tar.gz",
               "archive_created": "2026-09-06T02:00:11Z",
               "created": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
               "expires": (now + datetime.timedelta(days=7)).strftime(
                   "%Y-%m-%dT%H:%M:%SZ"),
               "extensions": 0})
m.save_registry(reg)

ok("das Original hat eine eigene Adresse, einen Alias und eine Verknuepfung",
   bool(reg["instances"]["crm"]["address"])
   and bool(reg["instances"]["crm"]["aliases"])
   and bool(reg["instances"]["crm"]["links"]))
ok("und eine oeffentliche Route",
   any("public" in r["roles"] for r in reg["instances"]["crm"]["routes"]))
ok("die Generalprobe erkennt sich selbst",
   m.is_rehearsal(reg["instances"]["crm-probe"])
   and not m.is_rehearsal(reg["instances"]["crm"]))

print("")
print("Regel 2 — keine oeffentliche Route, an keinem Eingang")


def site(name):
    m.write_app_caddy(name, m.load_registry()["instances"][name])
    with open(os.path.join(m.CADDY_APPS_DIR, f"{name}.caddy"),
              encoding="utf-8") as f:
        return f.read()


def blocks(text):
    """Jeder handle-Block der Site, ohne den reservierten /auth/*-Block."""
    out, cur = [], None
    for line in text.splitlines():
        if line.startswith("\thandle"):
            cur = [line]
        elif cur is not None:
            cur.append(line)
            if line == "\t}":
                if not cur[0].startswith("\thandle /auth/*"):
                    out.append("\n".join(cur))
                cur = None
    return out


prod = site("crm")
probe = site("crm-probe")
# Geprueft wird auf `/verify` und nicht auf `forward_auth`: Eine
# oeffentliche Route ruft identity ebenfalls, aber unter `/throttle`
# (RFC-0010) — das ist die Drossel, keine Anmeldung. Wer hier nur nach
# `forward_auth` sucht, haelt die Bremse fuer eine Tuer.
ok("beim Original bleibt die oeffentliche Route ohne Anmeldung",
   any("uri /verify" not in b for b in blocks(prod)), prod)
ok("sie geht aber durch die Drossel",
   any("uri /throttle" in b for b in blocks(prod)), prod)
ok("bei der Generalprobe verlangt JEDER Block eine Anmeldung",
   bool(blocks(probe)) and all("uri /verify" in b for b in blocks(probe)), probe)
ok("und keiner haengt mehr an der Drossel statt an der Anmeldung",
   all("uri /throttle" not in b for b in blocks(probe)), probe)
ok("die vormals oeffentliche Route verlangt nur Anmeldung, keine Rolle",
   any("uri /verify?roles=&" in b for b in blocks(probe)), probe)
ok("und die Rollen der anderen Route bleiben unangetastet",
   any("roles=keyuser" in b for b in blocks(probe)), probe)

print("")
print("...auch an dem Eingang, den jemand vergessen koennte")

# Der eigentliche Schutz: site_body sucht die Antwort SELBST, wenn der
# Aufrufer sie nicht mitgibt. Genau so faellt der eine Aufrufer, den
# niemand nachgezogen hat, in die sichere Richtung.
lazy = "\n".join(m.site_body(reg["instances"]["crm-probe"]["routes"],
                             "oaap-app-crm-probe", 8000, None, "crm-probe"))
ok("ein Aufrufer ohne login_only bekommt trotzdem die sichere Fassung",
   all("uri /verify" in b for b in blocks(lazy)), lazy)
lazy_prod = "\n".join(m.site_body(reg["instances"]["crm"]["routes"],
                                  "oaap-app-crm", 8000, None, "crm"))
ok("und eine gewoehnliche Instanz behaelt ihre oeffentliche Route",
   any("uri /verify" not in b for b in blocks(lazy_prod)), lazy_prod)

print("")
print("Regel 4 — das Manifest wird nicht umgeschrieben")

stored = m.load_registry()["instances"]["crm-probe"]["routes"]
ok("die gespeicherte Route sagt weiterhin 'public'",
   any("public" in r["roles"] for r in stored),
   "sonst laege neben der App eine unwahre Kopie ihres Manifests")
ok("die Objektseite zeigt sie auch so an",
   any("ohne Anmeldung" in r["who"] for r in
       iv.route_rows(m.load_registry()["instances"]["crm-probe"])))

print("")
print("Regel 1 — keine eigene Adresse, kein Alias")

err = dies(m.cmd_address, argparse.Namespace(
    action="set", name="crm-probe", hostname="probe.example.org"))
ok("'address set' wird abgelehnt", bool(err), err)
ok("die Ablehnung nennt die Generalprobe als Grund",
   "rehearsal" in err.lower() and "RFC-0030" in err, err)
ok("und sagt, wo es stattdessen hingehoert", "production instance" in err, err)
ok("die Adresse wurde nicht gesetzt",
   not m.load_registry()["instances"]["crm-probe"].get("address"))

err = dies(m.cmd_address, argparse.Namespace(
    action="alias-add", name="crm-probe", hostname="alt.example.org"))
ok("'alias-add' ebenso", bool(err), err)

with contextlib.redirect_stdout(_io.StringIO()) as out:
    m.cmd_address(argparse.Namespace(action="show", name="crm-probe",
                                     hostname=None))
ok("'address show' bleibt erlaubt — die Frage ist berechtigt",
   "crm-probe" in out.getvalue(), out.getvalue())

err = dies(m.cmd_address, argparse.Namespace(
    action="set", name="crm", hostname="neu.example.org"))
ok("am Original aendert sich nichts", err == "", err)

print("")
print("Regel 3 — keine App-Verknuepfungen, in keine Richtung")

err = dies(m.cmd_link, argparse.Namespace(
    action="add", source="crm-probe", target="lager"))
ok("von der Generalprobe zur Produktion: abgelehnt", bool(err), err)
ok("die Ablehnung sagt, warum — zweiter Schreiber auf echten Daten",
   "RFC-0030" in err and "production data" in err, err)
err = dies(m.cmd_link, argparse.Namespace(
    action="add", source="lager", target="crm-probe"))
ok("und umgekehrt auch", bool(err), err)
ok("die Generalprobe hat keine Verknuepfungen",
   not m.load_registry()["instances"]["crm-probe"].get("links"))
ok("das Original behaelt seine",
   m.load_registry()["instances"]["crm"].get("links") == ["lager"])

print("")
print("Regel 5 — ein Urteil, keine Quelle")

reg = m.load_registry()
try:
    m.promotion_review(reg, "crm-probe", "crm")
    err = ""
except m.PromotionRefused as e:
    err = str(e)
ok("aus einer Generalprobe wird nicht uebernommen", bool(err), err)
ok("und die Ablehnung schickt zur Testinstanz",
   "verdict, not a" in err and "TEST instance" in err, err)

try:
    m.promotion_review(reg, "crm-test", "crm-probe")
    err = ""
except m.PromotionRefused as e:
    err = str(e)
ok("und in eine Generalprobe hinein auch nicht", bool(err), err)
ok("weil sie nicht erneut ausrollbar ist", "redeployable" in err, err)

print("")
print("Sichtbarkeit — ein Mensch verwechselt sie nicht mit der Produktion")

view = iv.rehearsal_view(m.load_registry()["instances"]["crm-probe"])
ok("die Generalprobe traegt ein eigenes Abzeichen",
   bool(view) and view["badge"] == "Generalprobe", view)
ok("mit Restlaufzeit daneben", bool(view) and "noch" in view["left"], view)
ok("eine gewoehnliche Instanz traegt keines",
   iv.rehearsal_view(m.load_registry()["instances"]["crm"]) is None)

past = dict(m.load_registry()["instances"]["crm-probe"])
past["rehearsal"] = dict(past["rehearsal"],
                         expires=(now - datetime.timedelta(days=1)).strftime(
                             "%Y-%m-%dT%H:%M:%SZ"))
ok("eine abgelaufene sagt das", iv.rehearsal_view(past)["expired"])
broken = dict(past, rehearsal=dict(past["rehearsal"], expires="irgendwann"))
ok("ein unlesbares Datum heisst 'unbekannt', nicht 'abgelaufen'",
   not iv.rehearsal_view(broken)["expired"]
   and "unbekannt" in iv.rehearsal_view(broken)["left"],
   "sonst wirft jemand eine Generalprobe weg, die noch laeuft")
ok("der Satz nennt beide Herkuenfte",
   "crm" in iv.rehearsal_note(view) and "crm-test" in iv.rehearsal_note(view),
   iv.rehearsal_note(view))
ok("und verharmlost die Daten nicht",
   "Kopie der Produktivdaten" in iv.rehearsal_note(view),
   iv.rehearsal_note(view))

print("")
print("Die Restlaufzeit im Host rechnet dasselbe")

ok("sieben Tage sind sieben Tage",
   m.rehearsal_days_left(m.load_registry()["instances"]["crm-probe"]) in (6, 7))
ok("abgelaufen ist negativ", m.rehearsal_days_left(past) < 0)
ok("eine gewoehnliche Instanz hat keine Restlaufzeit",
   m.rehearsal_days_left(m.load_registry()["instances"]["crm"]) is None)

print("")
print("ALLE PRUEFUNGEN BESTANDEN" if not fails else f"{fails} FEHLER")
sys.exit(1 if fails else 0)
