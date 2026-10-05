#!/usr/bin/env python3
"""Der Store ist geschlossen, bevor ihm jemand traut (oaap.data.store 0.2).

RFC-0057 Stufe 0: Bevor je eine App an das Postgres der Plattform kommt,
wird es an drei Türen geschlossen, die nicht voneinander abhängen --

    das Verbindungsrecht auf die gemeinsame Datenbank gehört der Gruppe
    der Plattform, nicht mehr jedem;
    die Anmelderegel lässt nur diese Gruppe in die gemeinsame Datenbank
    und jede andere Rolle nur in die Datenbank ihres eigenen Namens;
    der Superuser meldet sich über kein Netz an.

Diese Datei verteidigt, was ohne Postgres prüfbar ist: die Regeln selbst
(reine Funktionen), die Reihenfolge des Schließens gegen einen
nachgestellten Store, das Zurücklesen, und dass keine Tür zu einer Rolle
an der Gruppe vorbeiführt. Der Köder ist jedes Mal eine Rolle mit dem
Vorsatz `app_`: sie sieht aus wie eine der Plattform und darf nie in die
Gruppe.

Was diese Datei NICHT prüfen kann: dass Postgres die Regeln so auswertet,
wie sie gemeint sind. Das ist an der Maschine gemessen
(`program/messungen/store-geschlossen-messung.sh`, oaap-test).

Run: python3 test/test_store_closed.py
"""
import contextlib
import io
import os
import re
import sys
import tempfile
from argparse import Namespace

HERE = os.path.dirname(os.path.abspath(__file__))
PLATFORM = os.path.join(HERE, "..", "platform")
os.environ["OAAP_DATA_DIR"] = tempfile.mkdtemp(prefix="oaap-store-closed-")
sys.path.insert(0, PLATFORM)

import appctl as m  # noqa: E402

ok_n = fail_n = 0


def ok(label, cond, detail=""):
    global ok_n, fail_n
    if cond:
        ok_n += 1
        print(f"PASS  {label}")
    else:
        fail_n += 1
        print(f"FAIL  {label} {detail}")


IMAGE_HBA = """# PostgreSQL Client Authentication Configuration File
local   all             all                                     trust
host    all             all             127.0.0.1/32            trust
host    all             all             ::1/128                 trust
local   replication     all                                     trust
host    replication     all             127.0.0.1/32            trust
host    replication     all             ::1/128                 trust

host all all all scram-sha-256
"""


def rules(text):
    return [tuple(l.split()) for l in text.splitlines()
            if l.strip() and not l.strip().startswith("#")]


print("=== die Anmelderegeln (rein) ===")
new = m.store_hba_rewrite(IMAGE_HBA)
net = [r for r in rules(new) if r[0] == "host" and r[3] not in
       ("127.0.0.1/32", "::1/128")]
ok("die Sammelregel des Images ist weg",
   ("host", "all", "all", "all", "scram-sha-256") not in rules(new), new)
ok("genau die drei Regeln der Plattform bleiben als Netzregeln",
   tuple(net) == m.STORE_HBA_RULES, str(net))
ok("der Superuser wird ZUERST abgewiesen -- vor jeder Regel, die ihn "
   "einlassen könnte", net and net[0] == ("host", "all", "postgres", "all",
                                          "reject"), str(net))
ok("die gemeinsame Datenbank nur für die Gruppe (mit '+', also Mitglieder)",
   ("host", "postgres", "+oaap_platform", "all", "scram-sha-256") in net)
ok("jede andere Rolle nur in die Datenbank ihres Namens",
   ("host", "sameuser", "all", "all", "scram-sha-256") in net)
ok("der Socket des Containers bleibt, wie er war (darüber arbeitet der Host)",
   ("local", "all", "all", "trust") in rules(new))
ok("zweimal schreiben ändert nichts", m.store_hba_rewrite(new) == new)
old_block = IMAGE_HBA.replace(
    "host all all all scram-sha-256",
    m.STORE_HBA_BEGIN + "\nhost all all all trust\n" + m.STORE_HBA_END)
ok("ein alter eigener Block wird ersetzt, nicht behalten",
   m.store_hba_rewrite(old_block) == new)
for label, line in [
    ("eine fremde Netzregel", "host all all 10.0.0.0/8 md5"),
    ("eine fremde Sammelregel mit TLS", "hostssl all all all scram-sha-256"),
    ("eine Sammelregel mit Zusatz", "host all all all ldap ldapserver=x"),
]:
    try:
        m.store_hba_rewrite(IMAGE_HBA + line + "\n")
        ok(f"{label} wird nicht stillschweigend übernommen oder gelöscht",
           False, line)
    except ValueError as e:
        ok(f"{label} wird nicht stillschweigend übernommen oder gelöscht",
           line.split()[0] in str(e))

print("\n=== wer in die Gruppe gehört (rein) ===")
got = m.store_platform_roles(
    schemas=["twin_t1", "oaap_model", "app_ab12cd", "probe_t1"],
    logins=["twin_t1", "app_ab12cd", "probe_t1", "fremd", "app_ef34"])
ok("eine Rolle mit eigenem Schema gehört hinein",
   "twin_t1" in got and "probe_t1" in got, str(got))
ok("KÖDER: eine Rolle 'app_...' nie -- auch nicht mit Schema ihres Namens",
   not any(r.startswith("app_") for r in got), str(got))
ok("eine Anmelderolle ohne eigenes Schema nicht", "fremd" not in got)
ok("ein Schema ohne Rolle (das Modell) nicht", "oaap_model" not in got)


class Store:
    """Ein nachgestellter Store: genug Postgres, um zu sehen, WAS in
    WELCHER Reihenfolge geschieht, und um zurückzulesen."""

    def __init__(self, hba=IMAGE_HBA, logins=(), schemas=(), members=(),
                 broken_hba=False):
        self.hba = hba
        self.loaded = hba
        self.logins = list(logins)
        self.schemas = list(schemas)
        self.group = False
        self.members = set(members)
        self.public = {"postgres": True, "template1": True}
        self.group_connect = False
        self.broken_hba = broken_hba
        self.log = []

    def psql(self, sql, database="postgres"):
        self.log.append(sql)
        out = ""
        if sql.startswith("SELECT 1 FROM pg_roles WHERE rolname"):
            out = "1" if self.group else ""
        elif sql.startswith("CREATE ROLE") and "NOLOGIN" in sql:
            self.group = True
        elif sql.startswith("CREATE ROLE"):
            self.logins.append(re.search(r'ROLE "([^"]+)"', sql).group(1))
        elif sql.startswith("CREATE SCHEMA"):
            self.schemas.append(re.search(r'SCHEMA "([^"]+)"', sql).group(1))
        elif sql.startswith("SELECT rolname FROM pg_roles WHERE rolcanlogin"):
            out = "\n".join(self.logins)
        elif "FROM pg_auth_members" in sql:
            out = "\n".join(sorted(self.members))
        elif sql.startswith("SELECT nspname FROM pg_namespace"):
            out = "\n".join(sorted(self.schemas))
        elif sql.startswith(f'GRANT "{m.STORE_PLATFORM_ROLE}" TO'):
            self.members.add(re.search(r'TO "([^"]+)"', sql).group(1))
        elif sql.startswith("SELECT has_database_privilege"):
            db = re.search(r"'public', '(\w+)'", sql).group(1)
            out = "t" if self.public[db] else "f"
        elif sql.startswith("REVOKE CONNECT ON DATABASE"):
            self.public[re.search(r'DATABASE "(\w+)"', sql).group(1)] = False
        elif sql.startswith("GRANT CONNECT ON DATABASE postgres"):
            self.group_connect = True
        elif sql == "SHOW hba_file":
            out = "/pgdata/pg_hba.conf"
        elif sql.startswith("SELECT count(*) FROM pg_hba_file_rules"):
            out = "1" if self.broken_hba else "0"
        elif "FROM pg_hba_file_rules ORDER BY" in sql:
            rows = []
            for r in rules(self.hba):
                if r[0] == "local":
                    rows.append(f"local|{r[1]}|{r[2]}|||{r[3]}|")
                else:
                    addr = r[3].split("/")[0]
                    rows.append(f"{r[0]}|{r[1]}|{r[2]}|{addr}||{r[4]}|")
            out = "\n".join(rows)
        elif sql == "SELECT pg_reload_conf()":
            self.loaded = self.hba
        return Namespace(stdout=out + "\n")

    def run(self, cmd, **kw):
        self.log.append(" ".join(cmd[5:]) if cmd[:2] == ["docker", "exec"]
                        else " ".join(cmd))
        if cmd[-2:-1] == ["cat"]:
            return Namespace(stdout=self.hba)
        if "sh" in cmd:
            self.hba = kw["input"]
            self.log.append("WRITE pg_hba.conf")
        return Namespace(stdout="")

    def use(self):
        m._store_psql = self.psql
        m.run = self.run
        m._store_running = lambda: True
        m.has_profile = lambda p: True
        return self

    def first(self, needle):
        return next((i for i, l in enumerate(self.log) if needle in l), -1)


print("\n=== schließen: Reihenfolge und Ergebnis ===")
s = Store(logins=["twin_t1", "app_ab12cd", "fremd"],
          schemas=["twin_t1", "oaap_model", "app_ab12cd"]).use()
ok("vorher ist der Store offen -- an mehr als einer Tür",
   len(m.store_open_doors()) >= 3, str(m.store_open_doors()))
changed, strangers = m.store_close()
i_member = s.first(f'GRANT "{m.STORE_PLATFORM_ROLE}" TO "twin_t1"')
i_revoke = s.first('REVOKE CONNECT ON DATABASE "postgres"')
i_write = s.first("WRITE pg_hba.conf")
i_reload = s.first("pg_reload_conf")
ok("die Zwillingsrolle ist in der Gruppe, BEVOR das Recht entzogen wird "
   "(sonst wäre der Zwilling ausgesperrt)", 0 <= i_member < i_revoke,
   f"{i_member} {i_revoke}")
ok("das Recht ist entzogen, BEVOR die Anmelderegel wechselt",
   0 <= i_revoke < i_write < i_reload, f"{i_revoke} {i_write} {i_reload}")
ok("KÖDER: die Rolle 'app_ab12cd' ist nicht in der Gruppe",
   "app_ab12cd" not in s.members, str(s.members))
ok("die unbekannte Rolle wird genannt, nicht aufgenommen",
   strangers == ["fremd"] and "fremd" not in s.members, str(strangers))
ok("die Gruppe hat das Verbindungsrecht, alle anderen nicht mehr",
   s.group_connect and not s.public["postgres"] and not s.public["template1"])
ok("die neuen Regeln sind geladen, nicht nur geschrieben",
   s.loaded == s.hba and "sameuser" in s.loaded)
ok("zurückgelesen: keine Tür mehr offen", m.store_open_doors() == [],
   str(m.store_open_doors()))
n = len(s.log)
changed2, _ = m.store_close()
ok("ein zweiter Lauf ändert nichts und schreibt nichts",
   changed2 == [] and "WRITE pg_hba.conf" not in s.log[n:], str(changed2))

print("\n=== zurücklesen findet jede einzelne Tür ===")
s.public["postgres"] = True
ok("jeder darf wieder in die gemeinsame Datenbank -> gemeldet",
   any("'postgres'" in d for d in m.store_open_doors()))
s.public["postgres"] = False
s.members.add("app_ab12cd")
ok("KÖDER: eine App-Rolle in der Gruppe -> gemeldet",
   any("app_ab12cd" in d for d in m.store_open_doors()))
s.members.discard("app_ab12cd")
s.hba = s.hba.replace("host  all  postgres  all  reject\n", "")
ok("die Abweisung des Superusers fehlt -> gemeldet",
   any("pg_hba" in d for d in m.store_open_doors()), s.hba)
s.hba = m.store_hba_rewrite(IMAGE_HBA) + "host all all all trust\n"
ok("eine zusätzliche Sammelregel -> gemeldet",
   any("pg_hba" in d for d in m.store_open_doors()))

print("\n=== eine Regeldatei, die Postgres nicht annimmt ===")
s = Store(logins=["twin_t1"], schemas=["twin_t1"], broken_hba=True).use()
try:
    m.store_close()
    ok("schließen meldet den Fehler", False)
except RuntimeError:
    ok("schließen meldet den Fehler", True)
ok("die alte Datei liegt wieder da", s.hba == IMAGE_HBA)
ok("und es wurde NICHT neu geladen", s.first("pg_reload_conf") == -1)

print("\n=== eine fremde Regel in der Datei ===")
s = Store(hba=IMAGE_HBA + "host all all 10.0.0.0/8 md5\n",
          logins=["twin_t1"], schemas=["twin_t1"]).use()
buf = io.StringIO()
exited = False
with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
    try:
        m.cmd_data(Namespace(object="store", action="close", arg1=None,
                             arg2=None, yes=False, quiet=False))
    except SystemExit:
        exited = True
ok("'oaap data store close' bricht ab und nennt die Regel",
   exited and "10.0.0.0/8" in buf.getvalue(), buf.getvalue())
ok("die Datei ist unberührt", "WRITE pg_hba.conf" not in s.log)

print("\n=== jede Tür, durch die eine Rolle entsteht ===")
src = open(os.path.join(PLATFORM, "appctl.py"), encoding="utf-8").read()
logins = re.findall(r'CREATE ROLE "[^"]*" LOGIN', src)
ok("im ganzen appctl legt genau EINE Stelle eine Anmelderolle an",
   len(logins) == 1, str(logins))
body = src[src.index("def _store_platform_login"):]
body = body[:body.index("\ndef ")]
ok("und diese Stelle nimmt sie in die Gruppe auf",
   f'GRANT "{{STORE_PLATFORM_ROLE}}" TO' in body, body)

m.load_tenants = lambda: {"t1": {"label": "eins"}}
for label, kw in [
    ("'create' über die Kommandozeile",
     {"action": "create", "arg1": "probe", "arg2": "t1"}),
    ("'copy' über die Kommandozeile",
     {"action": "copy", "arg1": "twin_t1", "arg2": "twin_t1_r_x"}),
]:
    s = Store(logins=["twin_t1"], schemas=["twin_t1"]).use()
    m._schema_size_kb = lambda schema: 0
    m._store_free_kb = lambda: 10 ** 9
    with contextlib.redirect_stdout(io.StringIO()):
        m.cmd_data(Namespace(object="store", yes=False, quiet=False, **kw))
    made = kw["arg2"] if kw["action"] == "copy" else "probe_t1"
    ok(f"{label}: die neue Rolle ist in der Gruppe", made in s.members,
       str(s.members))
    ok(f"{label}: und der Store war vorher geschlossen worden",
       0 <= s.first("WRITE pg_hba.conf") < s.first(f'CREATE ROLE "{made}"'))

s = Store().use()
m._twin_secrets_load = lambda: {}
m._twin_secrets_save = lambda x: None
try:
    with contextlib.redirect_stdout(io.StringIO()):
        m._twin_ensure_schema("t9")
except Exception:  # der Rest der Funktion braucht ein echtes Postgres
    pass
ok("der Zwilling: seine neue Rolle ist in der Gruppe",
   "twin_t9" in s.members, str(s.members))
ok("der Zwilling: ein frischer Store ist geschlossen, bevor er seinen "
   "ersten Klienten bekommt",
   0 <= s.first("WRITE pg_hba.conf") < s.first('CREATE ROLE "twin_t9"'))

print("\n=== der Vorsatz 'app_' ist vergeben ===")
for label, kw in [
    ("'create app <mandant>'", {"action": "create", "arg1": "app", "arg2": "t1"}),
    ("'copy ... app_x'", {"action": "copy", "arg1": "twin_t1", "arg2": "app_x"}),
]:
    s = Store(logins=["twin_t1"], schemas=["twin_t1"]).use()
    buf = io.StringIO()
    exited = False
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        try:
            m.cmd_data(Namespace(object="store", yes=False, quiet=False, **kw))
        except SystemExit:
            exited = True
    ok(f"KÖDER: {label} wird abgelehnt", exited and "reserved" in buf.getvalue(),
       buf.getvalue())
    ok(f"{label}: nichts wurde angelegt",
       not any(l.startswith("CREATE") for l in s.log), str(s.log))

print("\n=== das Update schließt bestehende Knoten ===")
mig = open(os.path.join(PLATFORM, "migrate.sh"), encoding="utf-8").read()
ok("migrate.sh ruft 'data store close --quiet'",
   "data store close --quiet" in mig)
ok("und nur, wenn der Store antwortet (kein Aufruf gegen nichts)",
   mig.index("pg_isready", mig.index("oaap.data.store 0.2"))
   < mig.index("data store close --quiet"))

print(f"\n{ok_n} bestanden, {fail_n} fehlgeschlagen")
print("ALLE PRUEFUNGEN BESTANDEN" if not fail_n else "FEHLGESCHLAGEN")
sys.exit(1 if fail_n else 0)
