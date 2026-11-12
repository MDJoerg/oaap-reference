#!/usr/bin/env python3
"""Nachrichten / Postausgang (RFC-0072 Stufe 2, oaap.core.portal 2.15).

    Knoten   D10: nur 465/587, nur öffentliche Adressen (Docker-Netze und
             private Bereiche abgelehnt); D11: ohne gelungene Anmeldung wird
             nichts gespeichert; leer gelassenes Passwort bleibt; der Realm
             nimmt das eigene Postfach, sonst das des Knotens, sonst keines.
    Rollen   ein tenant_admin pflegt nur das Postfach seines Mandanten, nie
             das des Knotens und nie ein fremdes; der server_admin beides.
    Portal   Menüpunkt; der tenant_admin sieht vom Knotenpostfach nur den
             Absender; das Passwort geht in die Anfrage (0600), nur der
             server_admin nennt einen Mandanten.

Aufruf (im Portal-Abbild): python3 test/test_messages_portal.py
"""
import json
import os
import stat
import sys
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-messages-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, os.path.join(HERE, "..", "platform"))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services"))
import appctl as a  # noqa: E402

a.os.geteuid = lambda: 0
a.docker_subnets = lambda net=None: [__import__("ipaddress").ip_network("172.18.0.0/16")]
fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:600]}")


print("=== D10: was ein Mandant eintragen darf ===")
ok("Port 25 abgelehnt", "465" in a.mail_public_refusal("1.1.1.1", 25))
ok("öffentliche Adresse auf 465: erlaubt", a.mail_public_refusal("1.1.1.1", 465) == "")
ok("private Adresse abgelehnt", "not a public" in a.mail_public_refusal("10.1.2.3", 587))
ok("Docker-Netz abgelehnt", "not a public" in a.mail_public_refusal("172.18.0.5", 465))
ok("Loopback abgelehnt", "not a public" in a.mail_public_refusal("127.0.0.1", 465))
ok("unbekannter Name: abgelehnt", "resolve" in a.mail_public_refusal("nicht-da.invalid", 465))

dflt = a.ensure_default_tenant()
a.load_tenants  # noqa: B018
code = a._run_verb(["tenant", "create", "verein", "--name", "TV Verein"])[0]
code2 = a._run_verb(["tenant", "create", "andere", "--name", "Andere"])[0]
tv, _t = a.tenant_by_label("verein", include_former=False)
other, _t2 = a.tenant_by_label("andere", include_former=False)
ok("zwei Mandanten angelegt", code == 0 and code2 == 0 and tv and other, (code, code2))

LOGINS = []
a.mail_login_check = lambda m: (LOGINS.append(dict(m)) or
                                ((m.get("password") == "richtig"), "login said"))
SYNCED = []
a.tenant_realm_mail_sync = lambda tid, who="root": (SYNCED.append(tid) or (True, f"realm of {tid}: written"))

print("\n=== D11 und das Passwort ===")
try:
    a.mail_set("smtps://1.1.1.1:465", "info@verein.example", user="info@verein.example",
               secret="falsch", tid=tv, check_login=True)
    refused = ""
except a.DestinationRefused as exc:
    refused = str(exc)
ok("falsches Passwort: nichts gespeichert", "nothing was stored" in refused
   and a.mailbox_of(tv) == (None, None), refused)
a.mail_set("smtps://1.1.1.1:465", "info@verein.example", "TV Verein",
           user="info@verein.example", secret="richtig", tid=tv, check_login=True)
ok("richtiges Passwort: gespeichert", a.mailbox_of(tv)[0]["password"] == "richtig")
a.mail_set("smtps://1.1.1.1:465", "post@verein.example", "TV Verein",
           user="info@verein.example", secret="", tid=tv, check_login=True, keep_secret=True)
ok("leer gelassenes Passwort bleibt (gleicher Login)",
   a.mailbox_of(tv)[0]["password"] == "richtig" and a.mailbox_of(tv)[0]["from"] == "post@verein.example")
try:
    a.mail_set("smtps://10.0.0.9:465", "x@verein.example", tid=tv)
    refused = ""
except a.DestinationRefused as exc:
    refused = str(exc)
ok("ein Mandantenpostfach im privaten Netz: abgelehnt", "public" in refused, refused)

print("\n=== wer verschickt womit ===")
ok("eigenes Postfach geht vor", a.mail_for_tenant(tv)[0]["owner"] == "tenant")
ok("ohne eigenes und ohne Knotenpostfach: keines", a.mail_for_tenant(other) == (None, None))
a.mail_set("smtps://smtp.knoten.example:465", "noreply@example.org", user="knotenlogin@example.net",
           secret="geheim")          # the node's, server_admin, no checks of D10
ok("ohne eigenes: das des Knotens", a.mail_for_tenant(other)[0]["owner"] == "node")

print("\n=== Rollen (über den Spool-Kern) ===")
os.makedirs(os.path.dirname(a._identity_users_path()), exist_ok=True)
with open(a._identity_users_path(), "w", encoding="utf-8") as f:
    json.dump([{"username": "betreiber", "roles": ["server_admin"], "active": True,
                "tenant": dflt},
               {"username": "vorstand", "roles": ["tenant_admin"], "active": True,
                "tenant": tv},
               {"username": "knotenverwalter", "roles": ["tenant_admin"], "active": True,
                "tenant": dflt},
               {"username": "mitglied", "roles": ["user"], "active": True, "tenant": tv}], f)
SET = {"target": "smtps://1.1.1.1:465", "sender": "info@verein.example",
       "from_name": "TV", "reply_to": "", "user": "info@verein.example", "password": "richtig"}
r = a.mail_job({"op": "set", "args": dict(SET)}, "vorstand")
ok("tenant_admin speichert das eigene Postfach, der Realm folgt",
   r[0] and SYNCED[-1] == tv, r)
r = a.mail_job({"op": "set", "args": dict(SET, tenant=other)}, "vorstand")
ok("tenant_admin nennt einen fremden Mandanten: abgelehnt", not r[0], r)
r = a.mail_job({"op": "set", "args": dict(SET)}, "knotenverwalter")
ok("tenant_admin des Standardmandanten: das Knotenpostfach ist nicht seins", not r[0]
   and "server_admin" in r[1], r)
r = a.mail_job({"op": "set", "args": dict(SET)}, "mitglied")
ok("ein Mitglied: abgelehnt", not r[0], r)
r = a.mail_job({"op": "set", "args": dict(SET, tenant=other)}, "betreiber")
ok("server_admin pflegt einen Mandanten", r[0] and a.mailbox_of(other)[0], r)
SYNCED.clear()
r = a.mail_job({"op": "remove", "args": {}}, "vorstand")
ok("Entfernen: der Realm nimmt wieder das Knotenpostfach",
   r[0] and a.mailbox_of(tv) == (None, None) and SYNCED == [tv]
   and a.mail_for_tenant(tv)[0]["owner"] == "node", (r, SYNCED))
SYNCED.clear()
a._idp_write_mail  # noqa: B018
r = a.mail_job({"op": "set", "args": dict(SET, target="smtps://smtp.knoten.example:465",
                                          sender="noreply@example.org", user="knotenlogin@example.net",
                                          password="richtig")}, "betreiber")
ok("Knotenpostfach geändert: nur Realms ohne eigenes folgen (keine mit Anbieter hier)",
   r[0] and other not in SYNCED, (r, SYNCED))
ok("kein Satz trägt ein Passwort", all("richtig" not in str(x) for x in (r,)))

print("\n=== der Absendername im Realm ===")
CALLS = []


class FakeAdmin:
    def __init__(self, *a_, **k):
        self.trace = []

    def write_mail(self, space, mail, title="", accept_version=""):
        CALLS.append(title)
        return True, {}, ""


real_admin = a.idp_admin.Admin
a.idp_admin.Admin = FakeAdmin
c = {"kind": "keycloak", "base_url": "x", "auth": "client", "admin_id": "i", "admin_secret": "s"}
a._idp_write_mail("auth", c, "verein", {"name": "TV Verein"},
                  {"owner": "tenant", "from_name": "TV Handball"})
a._idp_write_mail("auth", c, "verein", {"name": "TV Verein"},
                  {"owner": "node", "from_name": "OAAP"})
a.idp_admin.Admin = real_admin
ok("eigenes Postfach: sein Absendername; Knotenpostfach: der Name des Mandanten",
   CALLS == ["TV Handball", "TV Verein"], CALLS)

print("\n=== das Portal ===")
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services", "portal"))
import app as portal  # noqa: E402

TMP = tempfile.mkdtemp(prefix="oaap-messages-portal-")
portal.DESTINATIONS_FILE = a.DESTINATIONS_FILE
portal.TENANTS_FILE = a.TENANTS_FILE
portal.SPOOL_DIR = os.path.join(TMP, "spool")
portal.SPOOL_QUEUE = os.path.join(portal.SPOOL_DIR, "queue")
portal.SPOOL_RESULTS = os.path.join(portal.SPOOL_DIR, "results")
os.makedirs(portal.SPOOL_RESULTS, exist_ok=True)
WHO = {"role": "tenant_admin", "tenant": tv}
portal.caller_roles = lambda: {WHO["role"]}
portal.caller_scope = lambda: (WHO["role"], WHO["tenant"])
portal.MAIL_WAIT_SECONDS = 3
cl = portal.app.test_client()
H = {"X-OAAP-User": "vorstand", "X-OAAP-Roles": "tenant_admin"}
t = cl.get("/nachrichten", headers=H).get_data(as_text=True)
ok("Menüpunkt und Seite für den tenant_admin", 'href="/nachrichten"' in t and "Postausgang" in t, t[-1500:])
ok("vom Knotenpostfach nur der Absender, nie Server oder Login",
   "noreply@example.org" in t and "smtp.knoten.example" not in t and "knotenlogin@example.net" not in t)
SENT = []


def answer():
    import time
    for _ in range(40):
        for fn in os.listdir(portal.SPOOL_QUEUE) if os.path.isdir(portal.SPOOL_QUEUE) else []:
            path = os.path.join(portal.SPOOL_QUEUE, fn)
            SENT.append((json.load(open(path)), stat.S_IMODE(os.stat(path).st_mode)))
            os.remove(path)
            json.dump({"ok": True, "message": "saved"},
                      open(os.path.join(portal.SPOOL_RESULTS, fn), "w"))
            return
        time.sleep(0.1)


def post(path, form):
    th = threading.Thread(target=answer)
    th.start()
    r = cl.post(path, data=form, headers=H)
    th.join()
    return r


r = post("/nachrichten/postausgang/set", dict(SET))
req, mode = SENT[-1]
ok("Speichern: eine Anfrage `mail`, 0600, mit Passwort, ohne Mandant (tenant_admin)",
   req.get("action") == "mail" and req["op"] == "set" and mode == 0o600
   and req["args"]["password"] == "richtig" and "tenant" not in req["args"], (req, oct(mode)))
ok("... und die Antwort steht auf der Seite, im Reiter Postausgang",
   r.status_code == 303 and "msg=saved" in r.headers.get("Location", "")
   and "tab=ausgang" in r.headers.get("Location", ""), r.headers.get("Location"))
WHO.update(role="server_admin", tenant=dflt)
H = {"X-OAAP-User": "betreiber", "X-OAAP-Roles": "server_admin"}
t = cl.get("/nachrichten", headers=H).get_data(as_text=True)
ok("server_admin am Knoten: erster Reiter Postausgang mit dem Postfach des Knotens",
   "Postfach des Knotens" in t and 'class="active">Postausgang' in t)
t = cl.get("/nachrichten?tab=mandanten", headers=H).get_data(as_text=True)
ok("... Reiter Mandanten: die Liste der Mandanten", "Verteiler (Einträge)" in t and "verein" in t)
r = post("/nachrichten/postausgang/set?tenant=andere", dict(SET))
ok("server_admin nennt den Mandanten in der Anfrage", SENT[-1][0]["args"].get("tenant") == other,
   SENT[-1][0])
WHO.update(role="tenant_admin", tenant=tv)
H = {"X-OAAP-User": "vorstand", "X-OAAP-Roles": "tenant_admin"}
ok("tenant_admin mit fremdem Mandanten in der Adresse: 404",
   cl.get("/nachrichten?tenant=andere", headers=H).status_code == 404)

print("\n=== Verteiler und Benachrichtigungen (RFC-0080) ===")
portal.NOTIFY_FILE = a.NOTIFY_FILE
portal.NOTIFY_LOG_FILE = a.NOTIFY_LOG_FILE
portal.NOTIFY_WAIT_SECONDS = 3
a.notify_list_set(tv, "Backup-Verantwortliche", a.notify_parse_entries(
    "vorstand@verein.example", "mandanten-admins", "archiv@verein.example"))
a.notify_route_set(tv, "backup.failed", ["Backup-Verantwortliche"], "[{mandant}] Backup")
a.notify_list_set(other, "Geheim", a.notify_parse_entries("geheim@andere.example"))
a._notify_log({"tenant": tv, "type": "backup.failed", "op": "open", "lists": ["Backup-Verantwortliche"],
               "to": 1, "cc": 1, "bcc": 1, "result": "sent", "said": "accepted for 3"})
t = cl.get("/nachrichten", headers=H).get_data(as_text=True)
ok("Reiter: nur der aktive wird gezeigt (Postausgang, kein Verteiler)",
   "Postausgang" in t and "Probe senden" not in t and "Zuletzt verschickt" not in t)
ok("tenant_admin hat keinen Reiter Mandanten; ein unbekannter Reiter fällt auf den ersten zurück",
   "tab=mandanten" not in t
   and 'class="active">Postausgang' in cl.get("/nachrichten?tab=mandanten", headers=H).get_data(as_text=True))
t = cl.get("/nachrichten?tab=verteiler", headers=H).get_data(as_text=True)
ok("tenant_admin: Pflicht-Verteiler, eigener Verteiler mit An/Cc/Bcc, Probe-Knopf",
   "Mandantenverwaltung" in t and "Backup-Verantwortliche" in t
   and "Bcc archiv@verein.example" in t and "Probe senden" in t, t[-3000:])
t = cl.get("/nachrichten?tab=meldungen", headers=H).get_data(as_text=True)
ok("die Zuordnung zeigt den Haken und den eigenen Betreff",
   'name="r__backup.failed" value="Backup-Verantwortliche" checked' in t
   and "[{mandant}] Backup" in t)
ok("keine Knotenart (Platte) beim Mandanten, aber die Mandantenarten",
   "Platte fast voll" not in t and "Neubau einer Instanz fehlgeschlagen" in t)
t = cl.get("/nachrichten?tab=protokoll", headers=H).get_data(as_text=True)
ok("das Protokoll zeigt die Sendung ohne Adressen",
   "verschickt" in t and "1 / 1 / 1" in t)
t = "".join(cl.get("/nachrichten?tab=" + k, headers=H).get_data(as_text=True)
            for k in ("ausgang", "verteiler", "meldungen", "protokoll"))
ok("nichts vom fremden Mandanten", "geheim@andere.example" not in t and "Geheim" not in t)
r = post("/nachrichten/verteiler/set", {"name": "Kasse", "to": "kasse@verein.example, Mandanten-Admins",
                                        "cc": "", "bcc": "", "prefix": ""})
req = SENT[-1][0]
ok("Verteiler speichern: eine Anfrage `notify` list-set, ohne Mandant, Rollenwort umgesetzt",
   req.get("action") == "notify" and req["op"] == "list-set" and "tenant" not in req["args"]
   and "mandanten-admins" in req["args"]["to"], req)
ok("... zurück in den Reiter Verteiler", "tab=verteiler" in r.headers.get("Location", ""),
   r.headers.get("Location"))
r = post("/nachrichten/benachrichtigungen", {"r__critical": "Backup-Verantwortliche",
                                             "r__backup.failed": "Backup-Verantwortliche",
                                             "s__backup.failed": "[{mandant}] Backup"})
req = SENT[-1][0]
ok("Zuordnung: nur die geänderte Zeile reist (Kritisch), zurück in den Reiter",
   req["op"] == "route-set" and list(req["args"]["routes"]) == ["critical"]
   and "tab=meldungen" in r.headers.get("Location", ""), (req, r.headers.get("Location")))
WHO.update(role="server_admin", tenant=dflt)
H = {"X-OAAP-User": "betreiber", "X-OAAP-Roles": "server_admin"}
t = "".join(cl.get("/nachrichten?tab=" + k, headers=H).get_data(as_text=True)
            for k in ("verteiler", "meldungen", "mandanten"))
ok("server_admin am Knoten: Serverbesitzer, Knotenarten, Mandanten nur mit Namen und Anzahl",
   "Serverbesitzer" in t and "Platte fast voll" in t and "Geheim (1)" in t
   and "geheim@andere.example" not in t and "vorstand@verein.example" not in t, t[-2500:])
ok("Mandantenarten am Knoten tragen den Hinweis „nur mit eigenem Haken“",
   "nur mit eigenem Haken" in t)
portal.NOTIFY_VIEW_FILE = a.NOTIFY_VIEW_FILE
a.notify_view_write()        # no person in this test has an address
t = cl.get("/nachrichten?tab=protokoll", headers=H).get_data(as_text=True)
ok("Serverbesitzer ohne Adresse: die Warnung steht über den Reitern, in jedem Reiter",
   "erreicht im Moment" in t and "niemanden" in t
   and t.index("erreicht im Moment") < t.index('class="tabs"'), t[-2500:])
r = post("/nachrichten/verteiler/test?tenant=verein", {"name": "Backup-Verantwortliche"})
req = SENT[-1][0]
ok("Probe für einen Mandanten: der server_admin nennt ihn",
   req["op"] == "test" and req["args"].get("tenant") == tv, req)

print("\n=== Wochenbericht (RFC-0083) ===")
portal.REPORTS_DIR = a.REPORTS_DIR
portal.REPORT_SETTINGS_FILE = a.REPORT_SETTINGS_FILE
os.makedirs(os.path.join(a.REPORTS_DIR, dflt), exist_ok=True)
open(os.path.join(a.REPORTS_DIR, dflt, "2026-W46.md"), "w").write(
    "# Wochenbericht oaapx01 — KW 46\n\n## Auf einen Blick\n\n- 1 Neustart <b>x</b>\n\n"
    "| Wann | Was |\n|---|---|\n| Do | Neustart |\n")
t = cl.get("/nachrichten?tab=berichte", headers=H).get_data(as_text=True)
ok("server_admin: Reiter Berichte zeigt den Bericht gerendert, Text maskiert, und „Wann“",
   "<h2>Auf einen Blick</h2>" in t and "&lt;b&gt;x&lt;/b&gt;" in t and "<td>Neustart</td>" in t
   and 'action="/nachrichten/berichte"' in t, t[-2500:])
r = post("/nachrichten/berichte", {"day": "fri", "at": "18:30", "enabled": "on"})
req = SENT[-1][0]
ok("„Wann“ speichern: notify report-settings, zurück in den Reiter",
   req["op"] == "report-settings" and req["args"]["day"] == "fri" and req["args"]["at"] == "18:30"
   and "tab=berichte" in r.headers.get("Location", ""), req)
t = cl.get("/nachrichten?tab=meldungen", headers=H).get_data(as_text=True)
ok("Zuordnung: Spalte „nur im Bericht“, Kritisch und Backup „immer sofort“",
   "nur im Bericht" in t and 'name="d__operations"' in t and 'name="d__critical"' not in t
   and 'name="d__backup.failed"' not in t and "immer sofort" in t)
r = post("/nachrichten/benachrichtigungen", {"d__operations": "report"})
req = SENT[-1][0]
ok("„nur im Bericht“ für Betrieb: reist als delivery report",
   req["args"]["routes"] == {"operations": {"lists": [], "subject": "", "delivery": "report"}}, req)
WHO.update(role="tenant_admin", tenant=tv)
H = {"X-OAAP-User": "vorstand", "X-OAAP-Roles": "tenant_admin"}
t = cl.get("/nachrichten?tab=berichte", headers=H).get_data(as_text=True)
ok("tenant_admin: Reiter Berichte, ohne Zuordnung der Hinweis, nie der Bericht des Knotens, kein „Wann“",
   "bekommt noch keinen Wochenbericht" in t and "oaapx01 — KW 46" not in t
   and 'action="/nachrichten/berichte"' not in t, t[-2000:])

print(f"\n{'ALL PASS' if not fails else str(fails) + ' FAILURES'}")
sys.exit(1 if fails else 0)
