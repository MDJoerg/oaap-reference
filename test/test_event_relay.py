#!/usr/bin/env python3
"""Das Ereignis-Relais (oaap.data.twin 0.3, RFC-0032 Bauplan Schritt 2).

Jede Schreibung in den Zwilling legt seit RFC-0031 eine Zeile in `events`
ab; bis 0.3 las sie niemand. Das Relais (`services/twin/relay.py`, eigener
Dienst `relay` am Profil `broker`) veröffentlicht sie jetzt am Broker und
schreibt je Gruppenänderung einen Schnappschuss in `states`. Diese Datei
hält fest, was ohne Postgres, ohne Broker und ohne Docker beweisbar ist:

    relay.py: Themenbaum (RFC-0032 §1.1), dünne Nachricht (§1.3), wer
    einen `states`-Eintrag bekommt (D3), und die Reihenfolge, auf der die
    Verlustfreiheit hängt -- erst bestätigt der Broker, dann schreiben
    Schnappschuss und Wasserstand in EINER Transaktion.
    appctl.py: beide neuen Tabellen in jedem Mandantenschema, vor den
    Rechten angelegt, damit die Mandantenrolle sie bekommt.
    docker-compose.yml / Dockerfile: 'relay' am Profil 'broker', ohne
    Port, ohne Abhängigkeit von 'store'; das Geheimnis nur bei identity
    und relay.
    oaap node / migrate.sh / install.sh / update.sh: das Profil nimmt das
    Relais mit, das Geheimnis entsteht bei Installation UND Update, und
    kein 'up -d' zieht den Broker ohne sein Overlay neu hoch.
    identity: der Plattform-Prinzipal 'oaap.relay' darf nur
    veröffentlichen, nur in den Baum eines bekannten Mandanten, und gar
    nichts, wenn das Geheimnis fehlt -- mit Flasks Testclient.
    twin: '/internal/twin/outbox' liegt hinter dem Präfix-Wächter und
    liefert Zählungen, nie Werte.
    portal: relay_view.py als reine Regeln, jede Zeile der Tabelle.

Was diese Datei NICHT prüfen kann: dass Mosquitto eine verweigerte
Veröffentlichung unter MQTT v5 wirklich mit einem Fehlercode quittiert
(darauf baut relay.publish) und dass ein echter Durchlauf Ereignis ->
Broker -> `states` hält. Das gehört auf oaap-test.

Run: python3 test/test_event_relay.py
"""
import datetime
import importlib
import json
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
PLATFORM = os.path.join(ROOT, "platform")
TWIN_DIR = os.path.join(PLATFORM, "services", "twin")
PORTAL_DIR = os.path.join(PLATFORM, "services", "portal")
IDENTITY_DIR = os.path.join(PLATFORM, "services", "identity")

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:400]}")


def read(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as f:
        return f.read()


def service_block(src, name):
    """One service of docker-compose.yml, up to the next service or
    top-level key -- enough to check it without a YAML parser."""
    i = src.index(f"\n  {name}:\n")
    rest = src[i + 1:]
    m = re.search(r"\n(  [A-Za-z][\w-]*:|[A-Za-z][\w-]*:)[ \t]*\n", rest[len(name) + 4:])
    return rest if m is None else rest[:len(name) + 4 + m.start()]


def fn_body(src, name):
    return src.split(f"def {name}(", 1)[1].split("\ndef ", 1)[0]


T = "9edc585b-2161-4f8b-8a03-8dada10becbf"
OTHER = "e847c28e-2e9d-4e1a-8617-03159173bf2b"
OBJ = "3f1c2a7e-0b4d-4c55-9e1a-2b6f8d0c9a11"

# --------------------------------------------------------------- relay.py
print("=== relay.py: Themenbaum, Nachricht, Schnappschuss (RFC-0032 §1.1/§1.3/D3) ===")
sys.path.insert(0, TWIN_DIR)
import relay  # noqa: E402  -- must import WITHOUT psycopg2/paho installed

relay_src = read(TWIN_DIR, "relay.py")
ok("relay.py laedt ohne psycopg2 und paho -- beide werden erst beim Verbinden importiert",
   "psycopg2" not in sys.modules and "paho" not in sys.modules)
ok("der Prinzipal heisst 'oaap.relay'", relay.RELAY_USER == "oaap.relay")
ok("Gruppenereignis -> oaap/<mandant>/<typ>/<objekt>/<gruppe>",
   relay.topic_for(T, "Firma", OBJ, "crm.core") == f"oaap/{T}/Firma/{OBJ}/crm.core")
ok("Ereignis ohne Gruppe (Zusammenfuehren) -> Objekt-Ebene, ohne Gruppensegment",
   relay.topic_for(T, "Firma", OBJ, "") == f"oaap/{T}/Firma/{OBJ}")
ok("'type.created' traegt kein Objekt und hat keinen Platz im Baum",
   relay.topic_for(T, None, None, "crm.notiz") is None)
ok("ein Segment mit '/', '+' oder '#' wird nie zu einem Thema",
   all(relay.topic_for(T, t, OBJ, g) is None
       for t, g in (("Fir/ma", "crm.core"), ("Firma", "crm+x"), ("Firma", "crm.#"))))

row = {"id": 42, "kind": "group.written", "object_id": OBJ, "group_key": "crm.core",
       "origin": "app:partnerverwaltung", "type_key": "Firma",
       "recorded_at": datetime.datetime(2026, 9, 12, 10, 0, tzinfo=datetime.timezone.utc)}
msg = json.loads(relay.message_for(row))
ok("die Nachricht ist die duenne Ereigniszeile -- genau diese Felder, keine Werte",
   set(msg) == {"id", "kind", "object_id", "group_key", "origin", "recorded_at"}, msg)
ok("object_id im exportierten Format urn:oaap:obj:",
   msg["object_id"] == f"urn:oaap:obj:{OBJ}")
ok("recorded_at als ISO-Zeit", msg["recorded_at"].startswith("2026-09-12T10:00:00"))

topic = relay.topic_for(T, "Firma", OBJ, "crm.core")
ok("ein Gruppenereignis bekommt einen states-Eintrag", relay.wants_state(row, topic))
merged = dict(row, kind="object.merged", group_key="")
ok("ein Ereignis ohne Gruppe bekommt keinen (Koernung ist die Gruppe, D3)",
   not relay.wants_state(merged, relay.topic_for(T, "Firma", OBJ, "")))
ok("was nicht veroeffentlicht wird, bekommt auch keinen",
   not relay.wants_state(row, None))
snap = relay.snapshot_payload([{"attr_key": "name", "value": "Mueller GmbH",
                                "valid_from": datetime.date(2024, 6, 1), "valid_to": None}])
ok("Schnappschuss in der Form der Objektlesung (value/valid_from/valid_to je Attribut)",
   snap == {"name": {"value": "Mueller GmbH", "valid_from": "2024-06-01", "valid_to": None}},
   snap)
ok("nur vollstaendige twin_<mandant>-Eintraege gelten als Mandant",
   [t for t, _s, _c in relay.tenant_schemas({
       f"twin_{T}": {"role": "r", "password": "p"},
       "twin_kaputt": {"role": "r"},
       "anderes": {"role": "r", "password": "p"}})] == [T])

ok("MQTT v5 -- nur dort kommt eine verweigerte Veroeffentlichung als Fehlercode zurueck",
   "protocol=mqtt.MQTTv5" in relay_src)
pub = fn_body(relay_src, "publish")
ok("publish wartet auf die Bestaetigung UND prueft deren Fehlercode",
   "wait_for_publish" in pub and "is_failure" in pub and "retain=True" in pub
   and "qos=1" in pub)

rt = fn_body(relay_src, "relay_tenant")
i_pub = rt.find("publish(client")
i_ins = rt.find("INSERT INTO states")
i_upd = rt.find("SET last_event_id")
ok("Reihenfolge: erst veroeffentlichen, dann Schnappschuss, dann Wasserstand",
   0 < i_pub < i_ins < i_upd, (i_pub, i_ins, i_upd))
ok("Schnappschuss und Wasserstand in DERSELBEN Transaktion (kein 'with conn:' dazwischen)",
   "with conn:" not in rt[i_ins:i_upd])
ok("scheitert die Veroeffentlichung, bleibt der Wasserstand stehen (return vor dem Schreiben)",
   "except PublishFailed" in rt[i_pub:i_ins] and "return" in rt[i_pub:i_ins])
ok("ein zweites Veroeffentlichen derselben Zeile schreibt keinen zweiten Schnappschuss",
   "ON CONFLICT (event_id) DO NOTHING" in rt)
ok("der Wasserstand geht nur vorwaerts",
   "AND last_event_id < %s" in rt)
ok("in Reihenfolge der Ereignis-ID gelesen", "ORDER BY e.id" in rt)
main_src = fn_body(relay_src, "main")
ok("ohne BROKER_RELAY_KEY wird gar nicht erst verbunden (fail closed)",
   "if RELAY_KEY:" in main_src and "make_client" in main_src.split("if RELAY_KEY:", 1)[1][:80])
ok("ein Mandant mit Problemen haelt die anderen nicht auf",
   "except Exception" in main_src)

# ----------------------------------------------------------------- appctl
print("\n=== appctl.py: states und relay_watermark in jedem Mandantenschema ===")
appctl_src = read(PLATFORM, "appctl.py")
ddl = fn_body(appctl_src, "_twin_ensure_tables")
ok("states-Tabelle mit event_id UNIQUE", re.search(
    r'CREATE TABLE IF NOT EXISTS "\{schema\}"\.states \(.*?event_id bigint NOT NULL UNIQUE',
    ddl, re.S) is not None)
ok("states traegt die Spalten aus RFC-0032 §1.4",
   all(c in ddl.split(".states (", 1)[1].split(");", 1)[0]
       for c in ("object_id uuid NOT NULL", "group_key text NOT NULL",
                 "recorded_at timestamptz", "payload jsonb NOT NULL")))
ok("relay_watermark erlaubt genau eine Zeile (CHECK id = 1)",
   "CHECK (id = 1)" in ddl)
ok("die eine Zeile wird angelegt, idempotent",
   'INSERT INTO "{schema}".relay_watermark (id) VALUES (1)' in ddl
   and "ON CONFLICT (id) DO NOTHING" in ddl)
ok("beide Tabellen entstehen VOR 'GRANT ALL ON ALL TABLES' -- sonst fehlt der Mandantenrolle das Recht",
   ddl.find(".relay_watermark (") < ddl.find("GRANT ALL ON ALL TABLES")
   and ddl.find(".states (") < ddl.find("GRANT ALL ON ALL TABLES"))
ok("bestehende Mandanten bekommen sie beim Update (migrate.sh -> migrate-twin)",
   "data store migrate-twin" in read(PLATFORM, "migrate.sh"))

# ---------------------------------------------------------------- compose
print("\n=== docker-compose.yml: der Dienst 'relay' ===")
compose = read(PLATFORM, "docker-compose.yml")
relay_blk = service_block(compose, "relay")
ok("'relay' traegt das Profil 'broker' (Joergs Entscheidung) und nicht 'store'",
   'profiles: ["broker"]' in relay_blk and "store" not in relay_blk.split("profiles:", 1)[1].split("\n", 1)[0])
ok("gleiches Image wie twin, anderer Befehl",
   "build: ./services/twin" in relay_blk and "relay.py" in relay_blk)
ok("kein Port", "ports:" not in relay_blk)
dep = relay_blk.split("depends_on:", 1)[1].split("environment:", 1)[0] if "depends_on:" in relay_blk else ""
ok("haengt am Broker, NICHT an 'store' (inaktives Profil wuerde den Start verweigern)",
   "- broker" in dep and "store" not in dep, dep)
ok("haelt BROKER_RELAY_KEY", "BROKER_RELAY_KEY" in relay_blk)
ok("liest die Mandanten-Zugaenge nur lesend", '/platform-apps:ro' in relay_blk)
ok("identity haelt BROKER_RELAY_KEY (sie prueft die Anmeldung)",
   "BROKER_RELAY_KEY" in service_block(compose, "identity"))
ok("twin, broker und portal halten ihn NICHT",
   all("BROKER_RELAY_KEY" not in service_block(compose, s) for s in ("twin", "broker", "portal")))
ok("Dockerfile kopiert relay.py mit (Dateien werden einzeln gelistet)",
   re.search(r"^COPY .*\brelay\.py\b", read(TWIN_DIR, "Dockerfile"), re.M) is not None)
ok("paho-mqtt steht in den Abhaengigkeiten", "paho-mqtt" in read(TWIN_DIR, "requirements.txt"))

# ------------------------------------------------------------ node profile
print("\n=== oaap node: 'broker' nimmt das Relais mit, in beide Richtungen ===")
node_src = appctl_src.split("def cmd_node(args):", 1)[1].split("\ndef ", 1)[0]
add_half, remove_half = node_src.split("\n    else:\n", 1)
ok("add-profile broker startet broker UND relay",
   '"up", "-d", "broker", "relay")' in add_half)
ok("remove-profile broker stoppt broker UND relay",
   '_compose("stop", "broker", "relay")' in remove_half)
exp_add = add_half.split('if profile == "exposed" and has_profile("broker"):', 1)[1]
ok("'exposed' an-/abschalten zieht nur den Broker neu hoch, nicht das Relais",
   '"up", "-d", "broker")' in exp_add and "relay" not in exp_add.split("except", 1)[0])

# ---------------------------------------------------- migrate/install/update
print("\n=== migrate.sh / install.sh / update.sh ===")
migrate = read(PLATFORM, "migrate.sh")
ok("migrate.sh erzeugt BROKER_RELAY_KEY nur, wenn er fehlt",
   "! grep -q '^BROKER_RELAY_KEY=' \"$ENVF\"" in migrate)
relay_ups = [l for l in migrate.splitlines()
             if "up -d" in l and "relay" in l and not l.lstrip().startswith("#")]
ok("jedes 'up -d ... relay' in migrate.sh traegt --no-deps (sonst verliert der Broker sein Overlay)",
   relay_ups and all("--no-deps" in l for l in relay_ups), relay_ups)
ok("identity wird nach dem Erzeugen neu erstellt, damit sie den Schluessel kennt",
   "up -d --no-deps identity" in migrate)
ok("Sicherheitsnetz: fehlt oaap-relay-1, wird es gestartet",
   "name=^oaap-relay-1$" in migrate)
ok("laeuft der Broker schon, bekommt er mit 'exposed' sein Overlay erneut (Port-Heilung)",
   '"${BROKER_FILES[@]}" --profile broker up -d --no-deps broker' in migrate)
install = read(ROOT, "install.sh")
ok("install.sh erzeugt BROKER_RELAY_KEY und schreibt ihn in .env",
   'BROKER_RELAY_KEY="$(gen_secret)"' in install
   and "BROKER_RELAY_KEY=$BROKER_RELAY_KEY" in install)
update = read(PLATFORM, "update.sh")
ok("update.sh: das Overlay nur bei 'broker' UND 'exposed'",
   re.search(r'" broker ".*&&.*" exposed "', update) is not None
   and "docker-compose.broker-exposed.yml" in update)
build_part = update.split("build --quiet", 1)[0].splitlines()[-1]
ok("update.sh: 'build' bekommt dieselben Dateien", '"${COMPOSE_FILES[@]}"' in build_part, build_part)
restart = update[update.find('say "Restarting core services ..."'):][:220]
ok("update.sh: 'up -d' bekommt dieselben Dateien", '"${COMPOSE_FILES[@]}"' in restart, restart)

# --------------------------------------------------------------- identity
print("\n=== identity: der Plattform-Prinzipal 'oaap.relay' ===")
try:
    import flask  # noqa: F401
except ImportError:
    print("SKIP  flask fehlt -- der Identity-Dienst laesst sich hier nicht laden.")
    flask = None

if flask is not None:
    def load_identity(data_dir, relay_key):
        os.environ["SESSION_SECRET"] = "test-session-secret"
        os.environ["SETUP_TOKEN"] = "test-setup-token"
        os.environ["INTERNAL_API_KEY"] = "test-internal-key"
        os.environ["OAAP_IDENTITY_DATA_DIR"] = data_dir
        if relay_key is None:
            os.environ.pop("BROKER_RELAY_KEY", None)
        else:
            os.environ["BROKER_RELAY_KEY"] = relay_key
        if IDENTITY_DIR not in sys.path:
            sys.path.insert(0, IDENTITY_DIR)
        # 'app' is also the twin's module name -- make sure we get identity's.
        sys.modules.pop("app", None)
        sys.path.remove(IDENTITY_DIR)
        sys.path.insert(0, IDENTITY_DIR)
        m = importlib.import_module("app")
        m.USERS_FILE = os.path.join(data_dir, "users.json")
        m.KEYS_FILE = os.path.join(data_dir, "api-keys.json")
        m.TENANTS_FILE = os.path.join(data_dir, "tenants.json")
        with open(m.TENANTS_FILE, "w", encoding="utf-8") as f:
            json.dump({"tenants": {T: {"label": "default", "name": ""}}}, f)
        return m

    DATA = tempfile.mkdtemp(prefix="oaap-relay-test-")
    m = load_identity(DATA, "relay-secret-0123456789abcdef")
    ok("identity kennt denselben Namen wie relay.py", m.RELAY_USER == relay.RELAY_USER)
    ok("kein RFC-0027-Schluessel kann je 'oaap.relay' heissen (Schluessel-IDs sind hex)",
       m.KEY_TOKEN_RE.fullmatch("oaapk_oaap.relay_AAAAAAAAAAAAAAAAAAAAAAAA") is None)
    c = m.app.test_client()

    def getuser(u, p):
        return c.post("/mqtt-auth/getuser?k=test-internal-key",
                      json={"username": u, "password": p, "clientid": "oaap-relay"}).get_json()

    def acl(u, topic, acc):
        return c.post("/mqtt-auth/aclcheck?k=test-internal-key",
                      json={"username": u, "topic": topic, "clientid": "oaap-relay",
                            "acc": acc}).get_json()

    ok("richtiges Geheimnis -> angemeldet",
       getuser("oaap.relay", "relay-secret-0123456789abcdef")["Ok"] is True)
    ok("falsches Geheimnis -> abgelehnt", getuser("oaap.relay", "falsch")["Ok"] is False)
    ok("leeres Passwort -> abgelehnt", getuser("oaap.relay", "")["Ok"] is False)
    good = f"oaap/{T}/Firma/{OBJ}/crm.core"
    ok("veroeffentlichen (acc=2) in den Baum eines bekannten Mandanten -> erlaubt",
       acl("oaap.relay", good, 2)["Ok"] is True)
    ok("... auch auf Objekt-Ebene ohne Gruppe",
       acl("oaap.relay", f"oaap/{T}/Firma/{OBJ}", 2)["Ok"] is True)
    ok("abonnieren (acc=4) -> verweigert", acl("oaap.relay", good, 4)["Ok"] is False)
    ok("lesen (acc=1) -> verweigert", acl("oaap.relay", good, 1)["Ok"] is False)
    ok("lesen+schreiben (acc=3) -> verweigert", acl("oaap.relay", good, 3)["Ok"] is False)
    ok("Wildcard im Thema -> verweigert", acl("oaap.relay", f"oaap/{T}/#", 2)["Ok"] is False)
    ok("unbekannter Mandant -> verweigert",
       acl("oaap.relay", f"oaap/{OTHER}/Firma/{OBJ}/crm.core", 2)["Ok"] is False)
    ok("ausserhalb von oaap/ -> verweigert", acl("oaap.relay", "probe/x/y/z", 2)["Ok"] is False)
    ok("zu tief verschachtelt -> verweigert",
       acl("oaap.relay", f"{good}/extra", 2)["Ok"] is False)

    # the ordinary RFC-0027 path is unchanged
    m._save(m.USERS_FILE, [{"username": "geraet-1", "display_name": "", "password_hash": "",
                            "kind": "machine", "roles": ["user"], "groups": [],
                            "tenant": T, "active": True, "session_epoch": 0}])
    rec, token = m.issue_key(m.load_users(), "geraet-1", ["user"], "", "probe", 90, "test")
    ok("ein normaler Maschinen-Schluessel meldet sich weiter an (unveraendert)",
       getuser(rec["id"], token)["Ok"] is True)
    ok("und darf weiter in seinen eigenen Baum", acl(rec["id"], f"oaap/{T}/x", 2)["Ok"] is True)

    m2 = load_identity(tempfile.mkdtemp(prefix="oaap-relay-test-"), None)
    c = m2.app.test_client()
    ok("ohne BROKER_RELAY_KEY auf dem Knoten: auch ein leeres Passwort meldet nicht an (fail closed)",
       getuser("oaap.relay", "")["Ok"] is False)
    ok("ohne BROKER_RELAY_KEY: auch keine Themen-Freigabe",
       acl("oaap.relay", good, 2)["Ok"] is False)

# ------------------------------------------------------------------- twin
print("\n=== twin: /internal/twin/outbox ===")
twin_src = read(TWIN_DIR, "app.py")
ok("die Route liegt unter /internal/ -- der Praefix-Waechter deckt sie ab",
   '@app.get("/internal/twin/outbox")' in twin_src
   and 'if not request.path.startswith("/internal/"):' in twin_src)
ob = fn_body(twin_src, "twin_outbox")
ok("zaehlt wartende Ereignisse ab dem Wasserstand",
   "relay_watermark" in ob and "count(*)" in ob and "e.id > w.last_event_id" in ob)
ok("das Alter kommt aus der Uhr von Postgres, nicht der des Portals",
   "EXTRACT(EPOCH FROM now() - w.checked_at)" in ob)
ok("liefert nie Werte: kein states/attributes/payload in der Abfrage",
   all(w not in ob for w in ("payload", "attributes", "FROM states")))
ok("ein nicht migriertes Schema wird gemeldet statt 500",
   "except psycopg2.Error" in ob)

# ----------------------------------------------------------------- portal
print("\n=== portal: relay_view.py -- jede Zeile der Tabelle ===")
sys.path.insert(0, PORTAL_DIR)
import relay_view as rv  # noqa: E402


def t(pending=0, age=30.0, err=None, **kw):
    return dict({"tenant": T, "pending": pending, "watermark": 1,
                 "checked_age": age, "last_error": err}, **kw)


ok("ohne 'store' keine Zeile", rv.relay_state(["dev", "broker"], {"tenants": []}) is None)
r = rv.relay_state(["store"], None)
ok("Zwilling antwortet nicht -> unbekannt", r["state"] == "unknown", r)
r = rv.relay_state(["store", "broker"], {"tenants": []})
ok("kein Mandant -> ok", r["state"] == "ok", r)
r = rv.relay_state(["store", "broker"], {"tenants": [{"tenant": T, "error": "relay tables missing"}]})
ok("fehlende Tabellen -> Warnung mit Weg zurueck",
   r["state"] == "warn" and "migrate-twin" in r["detail"], r)
r = rv.relay_state(["store"], {"tenants": [t(pending=3, age=None)]})
ok("kein Broker, 3 warten -> Warnung, nennt 'add-profile broker', verspricht nichts Verlorenes",
   r["state"] == "warn" and "add-profile broker" in r["detail"]
   and "3 Ereignisse" in r["detail"] and "Verloren geht nichts" in r["detail"], r)
r = rv.relay_state(["store"], {"tenants": [t(pending=0, age=None)]})
ok("kein Broker, nichts wartet -> ok", r["state"] == "ok", r)
r = rv.relay_state(["store", "broker"], {"tenants": [t(pending=2, age=None)]})
ok("Broker da, Relais nie gemeldet, 2 warten -> Steht, nennt docker logs",
   r["state"] == "error" and "docker logs oaap-relay-1" in r["detail"], r)
r = rv.relay_state(["store", "broker"], {"tenants": [t(pending=0, age=900)]})
ok("Relais seit 15 Minuten still, nichts wartet -> Warnung 'Schweigt'",
   r["state"] == "warn" and r["label"] == "Schweigt" and "15 Minuten" in r["detail"], r)
r = rv.relay_state(["store", "broker"], {"tenants": [t(pending=5, err="Broker nicht erreichbar")]})
ok("frische Meldung mit Fehler, 5 warten -> Steht mit dem Fehlertext",
   r["state"] == "error" and "Broker nicht erreichbar" in r["detail"], r)
r = rv.relay_state(["store", "broker"], {"tenants": [t(pending=0, err="Broker nicht erreichbar")]})
ok("Fehler, aber nichts wartet -> Warnung", r["state"] == "warn", r)
r = rv.relay_state(["store", "broker"], {"tenants": [t(pending=1)]})
ok("frisch, 1 wartet -> Arbeitet (Einzahl richtig)",
   r["state"] == "ok" and r["label"] == "Arbeitet" and "1 Ereignis " in r["detail"], r)
r = rv.relay_state(["store", "broker"], {"tenants": [t(pending=0)]})
ok("frisch, nichts wartet -> Gesund", r["state"] == "ok" and r["label"] == "Gesund", r)
r = rv.relay_state(["store", "broker"], {"tenants": [t(pending=0, age=30), t(pending=0, age=4000)]})
ok("die juengste Meldung ueber alle Mandanten zaehlt", r["label"] == "Gesund", r)

portal_src = read(PORTAL_DIR, "app.py")
ok("Portal-Dockerfile kopiert relay_view.py mit -- sonst Neustart-Schleife wie in CURRENT_STATE 132",
   re.search(r"^COPY .*\brelay_view\.py\b", read(PORTAL_DIR, "Dockerfile"), re.M) is not None)
ok("app.py importiert relay_view", "\nimport relay_view\n" in portal_src)
health = fn_body(portal_src, "health")
ok("die Gesundheitsseite haengt die Zeile an", "relay_view.relay_state(" in health)
ok("/fleet/status bleibt unveraendert (nicht in _core_states)",
   "relay" not in fn_body(portal_src, "_core_states"))

print()
print("ALLE PRUEFUNGEN BESTANDEN" if not fails else f"{fails} PRUEFUNG(EN) FEHLGESCHLAGEN")
sys.exit(1 if fails else 0)
