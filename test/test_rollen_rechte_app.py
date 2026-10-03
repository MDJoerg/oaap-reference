#!/usr/bin/env python3
"""Die App "Rollen & Rechte" gegen den ECHTEN Identity-Dienst
(oaap.core.authorization 0.3, RFC-0045 A7).

Zwei Prozesse-in-einem: der echte Identity-Dienst (Flask-Testclient), davor
ein winziges "Gateway" (HTTP), davor die App (ihr echter HTTP-Server). Die
App spricht ueber ihr echtes `door.py` -- kein Fake auf ihrer Seite. So sieht
man, was ein Mensch im Browser sehen wuerde, und vor allem, was NICHT:

    - ein normaler Benutzer bekommt "Kein Zugriff" und keine Namen;
    - ein tenant_admin eines ANDEREN Mandanten bekommt dasselbe;
    - ein Formular von einer fremden Seite (oder ohne Herkunft) aendert nichts;
    - ein normaler Benutzer, der das Formular abschickt, aendert nichts;
    - ein Zuruecksprung-Ziel ausserhalb der App wird nicht befolgt;
    - Namen und Anzeigenamen werden in der Seite maskiert;
    - der volle Weg: Rolle -> Sammlung -> Zuordnung -> "gilt" -> Beenden ->
      Ausblenden; und dass jede Ablehnung des Dienstes als Satz ankommt.

Aufruf: python3 test/test_rollen_rechte_app.py
"""
import importlib
import importlib.util
import json
import os
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
IDENTITY_DIR = os.path.join(HERE, "..", "platform", "services", "identity")
SERVICES = os.path.join(HERE, "..", "platform", "services")
APP_DIR = os.path.normpath(os.path.join(HERE, "..", "..", "oaap-apps", "apps",
                                        "rollen-rechte"))
fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:700]}")


try:
    import flask  # noqa: F401
except ImportError:
    print("SKIP  flask fehlt.")
    sys.exit(0)
if not os.path.isfile(os.path.join(APP_DIR, "app.py")):
    print("SKIP  oaap-apps/apps/rollen-rechte fehlt (kein Wurzel-Checkout).")
    sys.exit(0)

DATA = tempfile.mkdtemp(prefix="oaap-rr-test-")
os.environ.update({"SESSION_SECRET": "s", "SETUP_TOKEN": "t",
                   "INTERNAL_API_KEY": "k", "OAAP_IDENTITY_DATA_DIR": DATA})
sys.path.insert(0, SERVICES)
sys.path.insert(0, IDENTITY_DIR)
sys.modules.pop("app", None)
m = importlib.reload(importlib.import_module("app"))
for attr, name in (("USERS_FILE", "users.json"), ("KEYS_FILE", "api-keys.json"),
                   ("THROTTLE_FILE", "login-throttle.json"),
                   ("AUDIT_LOG", "audit.jsonl"), ("TENANTS_FILE", "tenants.json"),
                   ("AUTHZ_FILE", "authorization.json"),
                   ("AUTHZ_LOCK_FILE", "authorization.lock")):
    setattr(m, attr, os.path.join(DATA, name))
with open(m.TENANTS_FILE, "w", encoding="utf-8") as f:
    json.dump({"tenants": {"t-default": {"label": "default"},
                           "t-vp": {"label": "vp"},
                           "t-sgl": {"label": "sgl"}}}, f)


def user(uid, name, roles, tenant, kind="human", display=""):
    return {"id": uid, "username": name, "display_name": display or name,
            "password_hash": "", "kind": kind, "roles": roles, "groups": [],
            "tenant": tenant, "active": True, "session_epoch": 0}


with open(m.USERS_FILE, "w", encoding="utf-8") as f:
    json.dump([
        user("u-vp-chef", "vp_chef", ["tenant_admin", "user"], "t-vp"),
        user("u-vp-ben", "ben", ["user"], "t-vp"),
        user("u-vp-carla", "carla", ["user"], "t-vp",
             display="<img src=x onerror=alert(1)>"),
        user("u-sgl-chef", "sgl_chef", ["tenant_admin", "user"], "t-sgl"),
        user("u-sgl-max", "max", ["user"], "t-sgl"),
        user("u-inst-rr", "instance:rr", ["user"], "t-vp", "machine"),
    ], f)

ic = m.app.test_client()
HI = {"X-OAAP-Internal-Key": "k"}
DECL = {"objects": [{"key": "news", "title": "News",
                     "activities": ["read", "publish"],
                     "fields": [{"key": "area",
                                 "values": ["news", "sponsoring"]}]}],
        "role_templates": [{"key": "bereich", "title": "Bereich",
                            "grants": [{"object": "news",
                                        "activities": ["read", "publish"],
                                        "area": "$value"}]}]}
assert ic.post("/internal/authz/register", headers=HI, json={
    "app": "vereinsportal", "version": "1", "declaration": DECL}
).status_code == 200
with m.authz_rw() as st:
    st["instances"]["vp-prod"] = {"app": "vereinsportal", "tenant": "t-vp"}
    st["instances"]["rr"] = {"app": "rollen-rechte", "tenant": "t-vp",
                             "administer": True}
    m.save_authz(st)
_r, KEY = m.issue_key(m.load_users(), "instance:rr", ["user"],
                      "oaap.authz.admin", "admin", 90, "root")


# ---- das "Gateway": reicht /authz/* an den echten Identity-Dienst weiter
class Relay(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _go(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else None
        r = ic.open(self.path, method=self.command, data=body,
                    headers={k: v for k, v in self.headers.items()
                             if k.lower() in ("authorization", "content-type")})
        raw = r.get_data()
        self.send_response(r.status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    do_GET = do_POST = do_DELETE = _go


relay = HTTPServer(("127.0.0.1", 0), Relay)
threading.Thread(target=relay.serve_forever, daemon=True).start()
os.environ["OAAP_AUTHZ_URL"] = f"http://127.0.0.1:{relay.server_port}/authz"
os.environ["OAAP_AUTHZ_ADMIN_KEY"] = KEY

sys.path.insert(0, APP_DIR)
spec = importlib.util.spec_from_file_location("rr_app",
                                              os.path.join(APP_DIR, "app.py"))
rr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rr)
srv = ThreadingHTTPServer(("127.0.0.1", 0), rr.Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_port}"
HOST = f"127.0.0.1:{srv.server_port}"


class NoFollow(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


opener = urllib.request.build_opener(NoFollow)


def req(method, path, who="u-vp-chef", form=None, origin=True, roles="user",
        headers=None):
    h = {"X-OAAP-User-Id": who, "X-OAAP-User": who}
    if roles:
        h["X-OAAP-Roles"] = roles
    if origin is True:
        h["Origin"] = "http://" + HOST
    elif origin:
        h["Origin"] = origin
    h.update(headers or {})
    data = None
    if form is not None:
        from urllib.parse import urlencode
        data = urlencode(form, doseq=True).encode()
        h["Content-Type"] = "application/x-www-form-urlencoded"
    r = urllib.request.Request(BASE + path, data=data, method=method, headers=h)
    try:
        resp = opener.open(r, timeout=10)
    except urllib.error.HTTPError as e:
        resp = e
    return resp.status if hasattr(resp, "status") else resp.code, \
        resp.headers, resp.read().decode("utf-8", "replace")


def state():
    return json.loads(json.dumps(m.load_authz()))


print("Wer die Seite sieht")
s, _h, b = req("GET", "/healthz", roles="")
ok("der Gesundheitspfad geht ohne Sitzung und meldet die Tuer",
   s == 200 and json.loads(b)["door"] is True, b)
s, _h, b = req("GET", "/", roles="")
ok("ohne Sitzung (keine Plattform-Kopfzeilen): 403", s == 403)
s, _h, b = req("GET", "/")
ok("der Mandanten-Admin sieht seine Personen", s == 200 and "ben" in b
   and "carla" in b, b[:300])
ok("KOEDER: keine Person eines anderen Mandanten, kein Maschinenkonto",
   "u-sgl-max" not in b and ">max<" not in b and "instance:rr" not in b)
ok("KOEDER: ein Anzeigename wird maskiert, nicht ausgefuehrt",
   "<img src=x" not in b and "&lt;img src=x" in b, b[:600])
s, _h, b = req("GET", "/", who="u-vp-ben")
ok("KOEDER: ein normaler Benutzer sieht 'Kein Zugriff' und KEINE Namen",
   s == 403 and "Kein Zugriff" in b and "carla" not in b, b[:300])
s, _h, b = req("GET", "/", who="u-sgl-chef")
ok("KOEDER: der Admin eines ANDEREN Mandanten kommt hier nicht hinein",
   s == 403 and "Kein Zugriff" in b and "carla" not in b)

print("Wer aendern darf")
before = state()
s, _h, b = req("POST", "/do/role-add", form={
    "app": "vereinsportal", "template": "bereich", "name": "X",
    "v_area": "news"}, origin=False)
ok("KOEDER: ein Formular ohne Herkunft aendert nichts", s == 403
   and state() == before)
s, _h, b = req("POST", "/do/role-add", form={
    "app": "vereinsportal", "template": "bereich", "name": "X",
    "v_area": "news"}, origin="https://boese.example")
ok("KOEDER: ein Formular von einer fremden Seite aendert nichts", s == 403
   and state() == before)
s, _h, b = req("POST", "/do/role-add", who="u-vp-ben", form={
    "app": "vereinsportal", "template": "bereich", "name": "X",
    "v_area": "news"})
ok("KOEDER: ein normaler Benutzer, der das Formular abschickt, aendert nichts",
   s == 403 and "Kein Zugriff" in b and state() == before)

print("Der volle Weg")
s, _h, b = req("GET", "/roles")
ok("die Rollen-Seite bietet die Vorlage mit den Werten der App an",
   s == 200 and "Bereich" in b and "value='sponsoring'" in b, b[:400])
s, h, b = req("POST", "/do/role-add", form={
    "app": "vereinsportal", "template": "bereich", "name": "<b>Redaktion</b>",
    "v_area": ["news"], "back": "/roles"})
ok("eine Rolle anlegen: 303 zurueck zur Seite", s == 303
   and h["Location"].startswith("/roles?ok=role-add"), (s, dict(h)))
ok("die Rolle steht beim Mandanten des Schluessels", any(
    r["name"] == "<b>Redaktion</b>" and r["tenant"] == "t-vp"
    for r in state()["roles"].values()))
s, _h, b = req("GET", "/roles?ok=role-add")
ok("die Seite sagt es, und der Name ist maskiert", "Rolle angelegt" in b
   and "&lt;b&gt;Redaktion&lt;/b&gt;" in b and "<b>Redaktion</b>" not in b)
rid = next(r["id"] for r in state()["roles"].values())
s, h, b = req("POST", "/do/collection-add", form={
    "name": "Redaktion", "roles": [rid], "back": "/collections"})
ok("eine Sammlung aus der Rolle", s == 303, (s, b[:200]))
cid = next(c["id"] for c in state()["collections"].values())
s, h, b = req("POST", "/do/assign", form={
    "user": "u-vp-ben", "collection": cid, "back": "https://boese.example/x"})
ok("zuordnen; KOEDER: ein fremdes Zuruecksprung-Ziel wird NICHT befolgt",
   s == 303 and h["Location"].startswith("/?ok=assign"), dict(h))
s, _h, b = req("GET", "/person?id=u-vp-ben")
ok("Bens Seite: er darf, seit wann, von wem",
   s == 200 and "news" in b and "publish" in b and "gilt" in b
   and "vp_chef" in b, b[:500])
aid = next(a["id"] for a in state()["assignments"].values())
s, _h, b = req("POST", "/do/collection-retire", form={"id": cid,
                                                     "back": "/collections"})
ok("Ausblenden mit lebender Zuordnung: der Dienst lehnt ab, der Satz kommt an",
   s == 200 and "live assignment" in b and "Redaktion" in b, b[:400])
s, h, b = req("POST", "/do/revoke", form={"id": aid,
                                         "back": "/person?id=u-vp-ben"})
ok("beenden", s == 303 and h["Location"].startswith("/person?id=u-vp-ben&ok="),
   dict(h))
s, _h, b = req("GET", "/person?id=u-vp-ben")
ok("Bens Seite danach: nichts mehr, die Zeile bleibt als 'beendet'",
   "beendet" in b and "Im Moment nichts" in b, b[:500])
s, h, b = req("POST", "/do/collection-retire", form={"id": cid})
ok("jetzt geht das Ausblenden", s == 303, (s, b[:200]))
s, _h, b = req("GET", "/collections")
ok("die Sammlung ist aus der Liste", "Redaktion" not in b.split("Neue Sammlung")[0]
   .split("<table>")[1], b[:600])
s, _h, b = req("GET", "/collections?alle=1")
ok("mit 'alle' ist sie wieder da, als ausgeblendet", "ausgeblendet" in b)

print("Gruppen und Protokoll")
s, h, b = req("POST", "/do/collection-add", form={
    "name": "Zweite", "roles": [rid]})
ok("eine Rolle bleibt nutzbar, auch wenn eine ihrer Sammlungen ausgeblendet "
   "ist", s == 303, (s, b[:200]))
cid2 = next((c["id"] for c in state()["collections"].values()
             if c["name"] == "Zweite"), "")
s, h, b = req("POST", "/do/map-add", form={"group": "Verein/Redaktion",
                                          "collection": cid2})
ok("eine Gruppe zuordnen", s == 303, (s, b[:200]))
s, _h, b = req("GET", "/mappings")
ok("die Seite zeigt Pfad und Sammlung und sagt, wann gelesen wird",
   "Verein/Redaktion" in b and "Zweite" in b
   and "bei jeder Anmeldung" in " ".join(b.split()),
   b[:500])
s, _h, b = req("GET", "/log")
ok("das Protokoll nennt die Person, nicht die App",
   s == 200 and "vp_chef" in b and "authz.assign" in b, b[:500])
s, _h, b = req("GET", "/log", who="u-vp-ben")
ok("KOEDER: das Protokoll sieht ein normaler Benutzer nicht", s == 403
   and "authz.assign" not in b)

print("Wenn etwas fehlt")
os.environ["OAAP_AUTHZ_ADMIN_KEY"] = ""
s, _h, b = req("GET", "/")
ok("ohne Schluessel: eine Seite, die es sagt (kein Absturz, keine Namen)",
   s == 200 and "Verwaltungsschluessel" in b and "carla" not in b, b[:300])
os.environ["OAAP_AUTHZ_ADMIN_KEY"] = KEY
os.environ["OAAP_AUTHZ_URL"] = "http://127.0.0.1:9/authz"
s, _h, b = req("GET", "/")
ok("Identity nicht erreichbar: ein Satz, keine Namen", s == 200
   and "nicht erreichbar" in b and "carla" not in b, b[:300])

print("")
print("ALLE PRUEFUNGEN BESTANDEN" if not fails else f"{fails} FEHLER")
sys.exit(1 if fails else 0)
