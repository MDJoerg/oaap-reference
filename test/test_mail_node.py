#!/usr/bin/env python3
"""Das Postfach des Knotens (RFC-0072 Stufe 1, oaap.net.destinations 2.11).

    Der Vertrag   `mail` ist ein deklariertes Verb des Keycloak-Konnektors;
                  ein neuer Realm ist deutsch; der Teil-Schreibauftrag nennt
                  nur Name, smtpServer und Sprache.
    Das Objekt    smtps:// (TLS) und smtp:// (STARTTLS) -- kein Klartext;
                  das Passwort steht im Geheimnisspeicher (0600), nie in der
                  Liste; eine App kann ein Postfach nicht binden (D7).
    Der Realm     write_mail schreibt, liest nach, nennt das Passwort nur
                  als masked/clear/absent und nie im Protokoll.
    Nebenbei      ein hochgeladenes Profil darf eine App des Paketkatalogs
                  nennen; der Aufbau kennt idp.admin_group.

Braucht kein Docker, keinen Knoten, keinen Mailserver.
Aufruf: python3 test/test_mail_node.py
"""
import json
import os
import stat
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-mail-test-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services"))
sys.path.insert(0, os.path.join(HERE, "..", "platform"))
import idp_admin as ia  # noqa: E402
import appctl as a  # noqa: E402
import tenant_build as tb  # noqa: E402

a.os.geteuid = lambda: 0
fails = 0
PW = "geh{eim}\"nis x"           # braces, quote, space: fine for mail


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:600]}")


print("=== der Vertrag ===")
ok("mail ist ein deklariertes Verb, die Tabelle bleibt stimmig",
   ia.declares("keycloak", "mail") and not ia.incoherent_verbs("keycloak"))
body = ia.space_body("keycloak", "verein", "Verein")
ok("ein neuer Realm ist deutsch (Englisch angeboten)",
   body.get("defaultLocale") == "de" and body.get("supportedLocales") == ["de", "en"]
   and body.get("internationalizationEnabled") is True, body)
MAIL = {"host": "mail.example.org", "port": 465, "tls": "tls",
        "from": "noreply@example.org", "from_name": "", "reply_to": "",
        "user": "noreply@example.org", "password": PW}
mb = ia.mail_body("keycloak", "verein", MAIL, "Handballverein")
ok("der Schreibauftrag nennt nur Name, smtpServer und Sprache",
   set(mb) == {"realm", "smtpServer", "internationalizationEnabled",
               "supportedLocales", "defaultLocale"}, sorted(mb))
sm = mb["smtpServer"]
ok("TLS von Anfang an: ssl=true, starttls=false, Werte als Text, Anzeigename = Mandant",
   sm["ssl"] == "true" and sm["starttls"] == "false" and sm["port"] == "465"
   and sm["auth"] == "true" and sm["fromDisplayName"] == "Handballverein", sm)
sm2 = ia.mail_body("keycloak", "v", dict(MAIL, tls="starttls", port=587), "")["smtpServer"]
ok("STARTTLS: ssl=false, starttls=true", sm2["ssl"] == "false" and sm2["starttls"] == "true")
ok("Nachlesen: verdeckt / im Klartext / fehlt",
   ia.mail_read("keycloak", {"smtpServer": {"host": "h", "password": "**********"}})["password"] == "masked"
   and ia.mail_read("keycloak", {"smtpServer": {"host": "h", "password": "x"}})["password"] == "clear"
   and ia.mail_read("keycloak", {"smtpServer": {"host": "h"}})["password"] == "absent"
   and ia.mail_read("keycloak", {}) == {})

print("\n=== das Objekt ===")
ok("smtps:// ohne Port = 465, TLS", a.parse_mail_target("smtps://mail.example.org")[:3]
   == ("tls", "mail.example.org", 465))
ok("smtp:// ohne Port = 587, STARTTLS", a.parse_mail_target("smtp://mail.example.org")[:3]
   == ("starttls", "mail.example.org", 587))
for bad, why in (("tcp://mail.example.org:25", "kein Klartext/andere Art"),
                 ("smtps://user:pw@mail.example.org", "kein Benutzer in der Adresse"),
                 ("smtps://localhost:465", "nicht die Plattform selbst"),
                 ("smtps://mail.example.org/x", "kein Pfad")):
    try:
        a.parse_mail_target(bad)
        refused = False
    except a.DestinationRefused:
        refused = True
    ok(f"abgelehnt: {why}", refused, bad)
a.ensure_default_tenant()
rec = a.mail_set("smtps://mail.example.org:465", "noreply@example.org",
                 user="noreply@example.org", secret=PW)
ok("gesetzt: ein smtp-Ziel 'mail' im Standardmandanten",
   rec["kind"] == "smtp" and rec["mail"]["from"] == "noreply@example.org")
listed = open(a.DESTINATIONS_FILE, encoding="utf-8").read()
ok("das Passwort steht nicht in der Liste", PW not in listed and "geh{" not in listed)
mode = stat.S_IMODE(os.stat(a.DEST_SECRETS_FILE).st_mode)
ok("... sondern im Geheimnisspeicher, 0600", mode == 0o600 and
   a.load_dest_secrets()[a.default_tenant_id()]["mail"] == PW, oct(mode))
mail, _r = a.node_mailbox()
ok("node_mailbox gibt Server, TLS, Absender und Passwort für den einen Aufruf",
   mail["host"] == "mail.example.org" and mail["tls"] == "tls" and mail["password"] == PW)
try:
    a.mail_set("smtps://mail.example.org", "noreply@example.org",
               user="u", secret="")
    refused = False
except a.DestinationRefused:
    refused = True
ok("mit Benutzer, ohne Passwort: abgelehnt", refused)
reg = {"instances": {"app1": {"tenant": a.default_tenant_id(), "app_id": "x"}}}
try:
    a.destination_bind(reg, "app1", "mail")
    refused = ""
except a.DestinationRefused as exc:
    refused = str(exc)
ok("eine App kann das Postfach nicht binden (D7)", "mailbox" in refused, refused)

print("\n=== der Realm ===")
REALM = {"realm": "verein", "registrationAllowed": False, "displayName": "Verein"}
SEEN = {"puts": []}


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *_a):
        pass

    def _json(self, doc, status=200):
        raw = json.dumps(doc).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        return self._json({"access_token": "t"})

    def do_GET(self):
        if self.path == "/admin/serverinfo":
            return self._json({"systemInfo": {"version": ia.connector_of("keycloak")["pinned"]}})
        if self.path == "/admin/realms/verein":
            doc = json.loads(json.dumps(REALM))
            if "smtpServer" in doc and doc["smtpServer"].get("password"):
                doc["smtpServer"]["password"] = "**********"
            return self._json(doc)
        return self._json({}, 404)

    def do_PUT(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        SEEN["puts"].append(body)
        REALM.update(body)          # Keycloak applies the named fields only
        self.send_response(204)
        self.end_headers()


srv = HTTPServer(("127.0.0.1", 0), Fake)
threading.Thread(target=srv.serve_forever, daemon=True).start()
adm = ia.Admin("keycloak", f"http://127.0.0.1:{srv.server_port}", "client",
               "oaap-admin", "s")
okw, back, msg = adm.write_mail("verein", MAIL, "Handballverein")
ok("geschrieben und nachgelesen", okw and back.get("host") == "mail.example.org", msg)
ok("das Passwort beim Nachlesen verdeckt", back.get("password") == "masked", back)
ok("ein einziger Teil-Schreibauftrag; der Rest des Realms bleibt",
   len(SEEN["puts"]) == 1 and REALM["registrationAllowed"] is False
   and REALM["displayName"] == "Verein")
ok("das Passwort steht nicht im Protokoll", not any(PW in t for t in adm.trace), adm.trace)
srv.shutdown()

print("\n=== nebenbei ===")
a.load_sources = lambda: ([{"id": "katalog", "url": "catalog:katalog", "enabled": True,
                            "trust": "platform"}], [])
a.catalog_source.is_catalog_url = lambda url: str(url).startswith("catalog:")
a._catalog_read = lambda src: {"apps": [{"id": "hbvp", "package": {"zip": "hbvp-1.zip"}}]}
ok("ein hochgeladenes Profil darf eine App des Paketkatalogs nennen",
   "hbvp" in (a._catalogue_app_ids() or set()), a._catalogue_app_ids())
prof = {"profile": tb.PROFILE_FORMAT, "id": "verein", "params":
        {"label": {"kind": "label", "required": True}},
        "steps": [{"id": "tenant", "type": "tenant.create", "label": "{label}"},
                  {"id": "verwalter", "type": "idp.admin_group"}]}
ok("der Aufbau kennt idp.admin_group", tb.profile_problems(prof) == [],
   tb.profile_problems(prof))

a.mail_clear()
ok("entfernt: kein Postfach mehr, kein Geheimnis",
   a.node_mailbox() == (None, None)
   and "mail" not in (a.load_dest_secrets().get(a.default_tenant_id()) or {}))

print(f"\n{'ALL PASS' if not fails else str(fails) + ' FAILURES'}")
sys.exit(1 if fails else 0)
