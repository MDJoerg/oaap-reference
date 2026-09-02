#!/usr/bin/env python3
"""Die Portalseiten fuer Zugaenge (RFC-0027).

Geprueft werden die echten Vorlagen aus `portal/app.py`, mit Jinja2
gerendert -- ohne Flask, ohne Container, ohne Knoten. Festgehalten wird
die REGEL, nicht das heutige Aussehen:

* Das Geheimnis steht genau auf einer Seite und NIE in einer Adresse.
* Entziehen ist unumkehrbar und bricht sofort etwas Laufendes -- es
  liegt deshalb hinter derselben Bestaetigung wie jede andere
  folgenschwere Aktion (Design-Guidelines 6.2.2), nicht auf einer
  Listenzeile.
* `server_admin` steht nicht zur Wahl. Identity lehnt es ohnehin ab;
  hier wird niemand dazu eingeladen, es zu versuchen.
* Eine leere Liste sagt, warum sie leer ist.

Der erste Punkt ist der, der still schiefgehen kann: Ein Geheimnis in
einer Weiterleitungs-Adresse landet im Verlauf des Browsers, im
Zugriffsprotokoll und in allem, was jemand kopiert.

Aufruf: python3 test/test_key_pages.py
"""
import ast
import io
import os
import sys

APP_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "..", "platform", "services", "portal", "app.py")

try:
    from jinja2 import Environment
except ImportError:                                          # pragma: no cover
    print("jinja2 fehlt -- pip install jinja2")
    sys.exit(1)

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:500]}")


SRC = io.open(APP_PY, encoding="utf-8").read()


def template(name):
    tree = ast.parse(SRC)
    for node in tree.body:
        if (isinstance(node, ast.Assign)
                and any(getattr(t, "id", "") == name for t in node.targets)):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} nicht in app.py gefunden")


ENV = Environment(autoescape=True)
KEY = {"id": "k7f3a91c", "principal": "terminal-3", "tenant": "t-cls",
       "roles": ["user"], "instance": "cls-viewer",
       "label": "Terminal Packstation 3", "created": "2026-09-02T08:00:00Z",
       "expires": "2026-12-01T08:00:00Z", "last_used": "2026-09-02T09:00:00Z",
       "created_by": "cls_admin", "revoked": False, "expired": False}

print("")
print("Die Liste")

liste = ENV.from_string(template("KEYS_LIST_BODY"))
html = liste.render(keys=[KEY], scope_note="", msg=None, error=None)
ok("sie nennt den Prinzipal und wofuer der Schluessel ist",
   "terminal-3" in html and "Packstation 3" in html)
ok("und die Instanz, auf die er begrenzt ist", "cls-viewer" in html)
ok("aber niemals ein Geheimnis",
   "secret" not in html.lower() and "oaapk_" not in html,
   "die Liste kennt es nicht einmal -- gespeichert ist nur ein Hash")
ok("Entziehen ist KEIN Knopf in der Zeile",
   "/revoke" not in html,
   "unumkehrbar und sofort wirksam gehoert hinter eine Bestaetigung "
   "(Design-Guidelines 6.2.2)")
ok("die Zeile fuehrt stattdessen auf die Objektseite",
   "/keys/k7f3a91c" in html)

leer = liste.render(keys=[], scope_note="", msg=None, error=None)
ok("eine leere Liste sagt, warum sie leer ist",
   "Noch kein Schl" in leer and len(leer) > 200)
ok("und erklaert, wofuer ein Schluessel ueberhaupt da ist",
   "Terminal" in leer and "Automatisierung" in leer)

entzogen = liste.render(keys=[dict(KEY, revoked=True)], scope_note="",
                        msg=None, error=None)
ok("ein entzogener Schluessel bleibt sichtbar",
   "k7f3a91c" in entzogen and "entzogen" in entzogen,
   "er wird nicht geloescht -- was mit ihm geschah, bleibt nachlesbar")

print("")
print("Die Objektseite und die Bestaetigung")

detail = ENV.from_string(template("KEY_DETAIL_BODY"))
html = detail.render(k=KEY, error=None)
ok("sie sagt, dass das Geheimnis nicht erneut anzeigbar ist",
   "nicht erneut anzeigen" in html or "nicht erneut" in html)
ok("Entziehen verlangt die Kennung getippt",
   'name="confirm"' in html and "k7f3a91c" in html)
ok("und sagt vorher, dass es sofort wirkt und endgueltig ist",
   "sofort" in html and ("rueckgaengig" in html
                         or "rückgängig" in html))
ok("es nennt den Tag der letzten Benutzung als Warnung",
   "2026-09-02" in html,
   "wer sieht, dass gestern noch etwas damit lief, ueberlegt es sich")

html_r = detail.render(k=dict(KEY, revoked=True), error=None)
ok("ein bereits entzogener Schluessel bietet das Entziehen nicht an",
   'name="confirm"' not in html_r)

print("")
print("Das Geheimnis -- genau einmal, und nie in einer Adresse")

gezeigt = ENV.from_string(template("KEY_SHOWN_BODY"))
html = gezeigt.render(k=KEY, secret="oaapk_k7f3a91c_" + "S" * 40)
ok("es steht auf der Seite", "oaapk_k7f3a91c_" in html)
ok("die Seite sagt, dass es der einzige Moment ist",
   "einzige Moment" in html)
ok("und wie es benutzt wird", "Bearer" in html)
ok("sie sagt auch, was bei Verlust zu tun ist",
   "neuen aus" in html and "entziehe" in html)

# Die eigentliche Zusage: der Code darf das Geheimnis nicht in eine
# Weiterleitung packen. Geprueft an der Quelle, weil genau das der
# bequeme Fehler waere -- redirect("/keys?secret=...") sieht harmlos aus.
create = SRC[SRC.index("def keys_create("):]
create = create[:create.index("@app.", 1)]
ok("keys_create leitet nach dem Ausstellen NICHT weiter",
   "redirect(" not in create,
   "eine Weiterleitung mit dem Geheimnis in der Adresse landet im "
   "Browserverlauf und im Zugriffsprotokoll")
ok("sondern rendert die Seite direkt aus der Antwort",
   "KEY_SHOWN_BODY" in create)

print("")
print("Was gar nicht erst zur Wahl steht")

neu = ENV.from_string(template("KEY_NEW_BODY"))
html = neu.render(principals=[{"username": "terminal-3", "kind": "machine",
                               "roles": ["user"]}],
                  all_roles=["admin", "keyuser", "user"],
                  instances=[{"key": "cls-viewer", "name": "viewer"}],
                  form={"principal": "", "roles": ["user"], "instance": "",
                        "label": "", "days": 90},
                  msg=None, error=None)
ok("die Rollenliste kommt von aussen und enthaelt kein server_admin",
   'value="server_admin"' not in html)
ok("die Seite sagt trotzdem, dass es abgelehnt wuerde",
   "server_admin" in html,
   "wer es sucht, soll den Grund lesen statt es auszuprobieren")
ok("sie draengt zur Begrenzung auf eine Instanz",
   "Begrenze ihn" in html)
ok("und erklaert, warum: die App sieht den Schluessel",
   "sieht" in html and "Sitzungs-Cookie" in html)
ok("'nie ablaufen' wird ausdruecklich ausgeschlossen",
   "Nie ablaufen" in html or "nie ablaufen" in html.lower())

ohne = neu.render(principals=[], all_roles=[], instances=[],
                  form={"principal": "", "roles": [], "instance": "",
                        "label": "", "days": 90}, msg=None, error=None)
ok("ohne Prinzipal fuehrt die Seite zum naechsten Schritt statt in ein "
   "leeres Formular", "Maschine" in ohne and "/users" in ohne)

print("")
print("Die Bestaetigung wird auch im Code geprueft, nicht nur im Browser")

revoke = SRC[SRC.index("def keys_revoke("):]
revoke = revoke[:revoke.index("@app.", 1)]
ok("der Server vergleicht die getippte Kennung selbst",
   'request.form.get("confirm"' in revoke and "!= kid" in revoke,
   "'required' im Formular ist eine Hoeflichkeit, keine Kontrolle")
ok("bei Nichtuebereinstimmung wird nichts entzogen",
   revoke.index("confirm") < revoke.index("INTERNAL.post"))

print("")
print("Maschinen im Benutzerbereich")

users = ENV.from_string(template("USERS_LIST_BODY"))
html = users.render(
    users=[{"username": "terminal-3", "display_name": "", "kind": "machine",
            "roles": ["user"], "groups": [], "active": True, "tenant": ""}],
    show_tenant=False, labels={}, default_tenant="", scope_note="",
    msg=None, error=None)
ok("eine Maschine ist in der Benutzerliste als solche erkennbar",
   "Maschine" in html)
ok("die Liste erklaert, was eine Maschine ist",
   "ohne Passwort" in html)
ok("und dass Deaktivieren ihre Schluessel entwertet",
   "entwertet" in html)

anlegen = ENV.from_string(template("USER_NEW_BODY"))
html_m = anlegen.render(form={"username": "", "display_name": "", "roles": [],
                              "groups": [], "tenant": "", "kind": "machine"},
                        all_roles=["user"], tenants=[], error=None)
ok("das Anlegen einer Maschine fragt kein Passwort",
   'name="password"' not in html_m)
ok("und warnt, so wenige Rollen wie moeglich zu geben",
   "so wenige Rollen" in html_m)
html_h = anlegen.render(form={"username": "", "display_name": "", "roles": [],
                              "groups": [], "tenant": "", "kind": "human"},
                        all_roles=["user"], tenants=[], error=None)
ok("beim Menschen bleibt das Passwortfeld", 'name="password"' in html_h)

print("")
print(f"{'FEHLER' if fails else 'Alles gruen'} - {fails} Fehlschlag(e)")
sys.exit(1 if fails else 0)
