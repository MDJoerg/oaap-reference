#!/usr/bin/env python3
"""Was die Gesundheitsseite ueber Sicherungen sagt (RFC-0029 D2).

Vier Zustaende, und sie brauchen vier verschiedene Antworten:

    Es laeuft gerade.        -> deshalb ist deine App nicht erreichbar
    Es hat funktioniert.     -> nichts zu tun
    Es ist fehlgeschlagen.   -> jemand muss hinsehen
    Es ist nie eingerichtet worden. -> jemand muss es einrichten

Die letzten beiden sind der Grund fuer diese Datei. Eine Anzeige, die
nur Erfolge zeigt, kann sie nicht auseinanderhalten -- und genau das
sind die zwei Faelle, in denen jemand handeln muss.

Dazu die Regel aus D2: **jede Wahrheit nur einmal.** Ob eine Kopie
ankam und ob die Pruefsumme stimmte, weiss nur die abholende Seite.
Der Knoten, der das Archiv weggegeben hat, darf es nicht behaupten.

Braucht jinja2 (wie test_instance_page.py), kein Docker, keinen Knoten.

Run: python3 test/test_backup_state_page.py
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services", "portal"))

try:
    from jinja2 import Template
except ImportError:
    print("SKIP: jinja2 ist nicht installiert (pip install jinja2)")
    sys.exit(0)

REG = tempfile.mkdtemp(prefix="oaap-bkstate-")
os.makedirs(os.path.join(REG, "backup-pulls"), exist_ok=True)

ok_n = fail_n = 0


def ok(label, cond, detail=""):
    global ok_n, fail_n
    if cond:
        ok_n += 1
        print(f"PASS  {label}")
    else:
        fail_n += 1
        print(f"FAIL  {label} {detail}")


# Nur die Lesefunktionen und die Vorlage werden gebraucht -- der Rest
# des Portals zieht Flask nach, das hier nicht noetig ist. Deshalb wird
# die Datei als Text gelesen und die zwei Teile herausgeschnitten, die
# geprueft werden sollen: so wird die ECHTE Vorlage geprueft und nicht
# eine Abschrift, die auseinanderlaufen kann.
SRC = open(os.path.join(HERE, "..", "platform", "services", "portal", "app.py"),
           encoding="utf-8").read()

start = SRC.index('<div class="card">\n  <h2>Sicherung</h2>')
end = SRC.index('<p class="muted">Geprüft wird aus Sicht des Portals', start)
CARD = Template(SRC[start:end])

ns = {"json": json, "os": os, "datetime": __import__("datetime").datetime}
block = SRC[SRC.index('BACKUP_LAST = "/apps-registry'):SRC.index("def recent_deploys(")]
block = block.replace('"/apps-registry/backup-last.json"',
                      repr(os.path.join(REG, "backup-last.json")))
block = block.replace('"/apps-registry/backup-schedule.json"',
                      repr(os.path.join(REG, "backup-schedule.json")))
block = block.replace('"/apps-registry/backup-pulls"',
                      repr(os.path.join(REG, "backup-pulls")))
ns["_ago"] = lambda s: f"{int(s // 60)} Minuten" if s >= 90 else f"{int(s)} Sekunden"
exec(compile(block, "portal-backup-block", "exec"), ns)  # noqa: S102
backup_state = ns["backup_state"]


def render():
    # Zeilenumbrueche der Vorlage vereinheitlichen: Geprueft wird, was
    # ein Mensch liest, und der sieht keinen Umbruch mitten im Satz.
    import re
    return re.sub(r"\s+", " ", CARD.render(bk=backup_state()))


def write(name, data):
    with open(os.path.join(REG, name), "w", encoding="utf-8") as f:
        json.dump(data, f)


def clear():
    for f in os.listdir(REG):
        p = os.path.join(REG, f)
        if os.path.isfile(p):
            os.remove(p)
    for f in os.listdir(os.path.join(REG, "backup-pulls")):
        os.remove(os.path.join(REG, "backup-pulls", f))


print("=== nie eingerichtet ===")
clear()
html = render()
ok("sagt, dass hier noch nie gesichert wurde", "noch nie" in html, html[:300])
ok("und dass kein Zeitplan eingerichtet ist", "Kein Zeitplan" in html)
ok("und nennt den Weg dorthin", "install-backup-timer.sh" in html)
ok("es steht keine Erfolgsmeldung da", "Zuletzt" not in html)

print("\n=== es hat funktioniert ===")
clear()
write("backup-last.json", {"schema": "0.2", "state": "ok",
                          "finished": "2026-09-05T11:10:01+00:00",
                          "archive": "/var/backups/oaap/x.tar.gz",
                          "bytes": 8401433578, "instances": 12,
                          "downtime_seconds": 32, "total_seconds": 245})
write("backup-schedule.json", {"schema": "0.1", "enabled": True, "at": "03:30",
                               "keep": 2, "target": "/var/backups/oaap",
                               "next": "2026-09-06T03:34:38+00:00"})
html = render()
ok("nennt den Zeitpunkt", "2026-09-05 11:10:01" in html, html[:400])
ok("nennt die Groesse", "8012 MB" in html or "8013 MB" in html, html[:400])
ok("nennt die Zahl der Instanzen", "12 Instanz" in html)
ok("und sagt vor allem, wie lange die Apps STANDEN",
   "32 Sekunden" in html, html[:500])
ok("getrennt von der Gesamtdauer des Laufs",
   "4 Minuten 5 Sekunden" in html, html[:500])
ok("der geplante naechste Lauf steht da",
   "03:30" in html and "2026-09-06 03:34:38" in html)
ok("mit Ziel und Aufbewahrung", "/var/backups/oaap" in html and "2 neuesten" in html)

print("\n=== es laeuft gerade ===")
clear()
import datetime as _dt  # noqa: E402
began = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=3)
write("backup-last.json", {"schema": "0.2", "state": "running",
                           "started": began.isoformat(),
                           "target": "/var/backups/oaap", "instances": 12})
html = render()
ok("sagt, dass es laeuft", "Läuft seit" in html, html[:300])
ok("und seit wann", "3 Minuten" in html, html[:300])
ok("und WARUM die Apps gerade nicht antworten",
   "stehen die Apps" in html, html[:400])
ok("es behauptet dabei keinen letzten Erfolg", "Zuletzt" not in html)

print("\n=== es ist fehlgeschlagen ===")
clear()
write("backup-last.json", {"schema": "0.2", "state": "failed",
                           "finished": "2026-09-05T03:31:00+00:00",
                           "message": "tar: exit 2 (kein Platz)"})
html = render()
ok("sagt, dass es fehlgeschlagen ist", "Fehlgeschlagen" in html, html[:300])
ok("und nennt den Grund", "kein Platz" in html, html[:400])
ok("es sagt NICHT 'noch nie gesichert' -- das waere die andere Lage",
   "noch nie" not in html, html[:300])
ok("und beruhigt zum Wichtigsten: die vorige Sicherung liegt noch da",
   "vorige Sicherung liegt unverändert" in html)

print("\n=== jede Wahrheit nur einmal ===")
clear()
write("backup-last.json", {"schema": "0.2", "state": "ok",
                           "finished": "2026-09-05T11:10:01+00:00",
                           "bytes": 100, "instances": 1,
                           "downtime_seconds": 5, "total_seconds": 9})
write("backup-schedule.json", {"schema": "0.1", "enabled": True, "at": "03:30",
                               "keep": 2, "target": "/var/backups/oaap",
                               "next": "2026-09-06T03:34:38+00:00"})
html = render()
ok("ein Knoten OHNE Abholungen sagt ausdruecklich, dass er nicht weiss, "
   "ob seine Sicherungen woanders angekommen sind",
   "kann es nicht wissen" in html, html[-600:])
ok("und behauptet keine Pruefsumme", "bestätigt" not in html)

with open(os.path.join(REG, "backup-pulls", "oaapx01.json"), "w",
          encoding="utf-8") as f:
    json.dump({"schema": "0.1", "kind": "pull", "source_node": "oaapx01",
               "result": "ok", "finished": "2026-09-05T10:35:12Z",
               "message": "8002 MB, checksum yes", "checksum_verified": "yes",
               "generations": {"daily": 1, "weekly": 0, "monthly": 0}}, f)
html = render()
ok("der abholende Knoten zeigt, was er geholt hat", "oaapx01" in html, html[-800:])
ok("und dass die Pruefsumme bestaetigt wurde", "bestätigt" in html)
ok("mit den Generationen", "daily: 1" in html)
ok("und sagt, warum das hier steht und nicht drueben",
   "weiß nur" in html and "abholende Seite" in html, html[-900:])

print("\n=== eine kaputte Datei legt die Seite nicht lahm ===")
clear()
with open(os.path.join(REG, "backup-last.json"), "w", encoding="utf-8") as f:
    f.write("{kein json")
html = render()
ok("die Seite rendert trotzdem", "Sicherung" in html)
ok("und faellt auf 'noch nie gesichert' zurueck statt etwas zu erfinden",
   "noch nie" in html, html[:300])

print(f"\n{ok_n} bestanden, {fail_n} fehlgeschlagen")
print("ALLE PRUEFUNGEN BESTANDEN" if not fail_n else "FEHLGESCHLAGEN")
sys.exit(1 if fail_n else 0)
