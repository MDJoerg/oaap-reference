#!/usr/bin/env python3
"""Der Byte-Speicher von RFC-0034, Stufe 1 (`oaap.data.files`).

Diese Schicht sieht nie einen Titel, einen Verweis oder einen
Eigentuemer. Sie legt Inhalte ab und gibt sie ueber ihren Hash zurueck.
Die Dokumenthaelfte -- Identitaet, Metadaten, Aufbewahrung, Zwilling --
ist Stufe 2 und steht hier nicht. Genau dieser Schnitt (D1) macht die
Stufe fuer sich pruefbar.

Zwei Eigenschaften tragen alles Weitere:

* **Der Name IST der Inhalt.** Deshalb kann der Speicher gefragt
  werden, ob er noch haelt, was er zu halten behauptet -- ohne eine
  zweite Aufzeichnung, gegen die man vergleichen muesste.
* **Der Mandant steht im PFAD, nicht in einer Spalte.** Die Bytes des
  einen Kunden sind aus dem Speicher des anderen nicht adressierbar,
  und die blosse Anwesenheit eines Hashes beantwortet keine Frage ueber
  fremde Daten.

Aufruf: python3 test/test_data_files.py
"""
import argparse
import contextlib
import io
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-files-test-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, os.path.join(HERE, "..", "platform"))

import appctl as m                                            # noqa: E402

m.reload_gateway = lambda: None

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:400]}")


SRC = tempfile.mkdtemp(prefix="oaap-files-src-")


def write(name, text):
    p = os.path.join(SRC, name)
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)
    return p


DEFAULT = m.ensure_default_tenant()
with contextlib.redirect_stdout(io.StringIO()):
    m.cmd_tenant(argparse.Namespace(
        action="create", name="cls", target=None, title="Kunde",
        account="", account_name="", grace_days=30, yes=True, count=50))
CLS, _t = m.tenant_by_label("cls")

print("Der Name ist der Inhalt")

a = write("protokoll.txt", "Wartung am 22.09.2026\n")
sha, stored = m.files_put(DEFAULT, a)
ok("ein Inhalt wird abgelegt", stored and len(sha) == 64, sha)
ok("und unter seinem Hash gefunden",
   m.files_get(DEFAULT, sha) == m.files_path(DEFAULT, sha))
ok("der Pfad faechert nach den ersten zwei Zeichen auf",
   os.path.basename(os.path.dirname(m.files_path(DEFAULT, sha))) == sha[:2],
   "ein Verzeichnis mit hunderttausend Eintraegen ist auf jedem "
   "Dateisystem langsam und fuer Menschen unlesbar")

sha2, stored2 = m.files_put(DEFAULT, a)
ok("derselbe Inhalt ein zweites Mal wird nicht noch einmal geschrieben",
   sha2 == sha and stored2 is False)

b = write("kopie.txt", "Wartung am 22.09.2026\n")
sha3, stored3 = m.files_put(DEFAULT, b)
ok("gleicher Inhalt unter anderem Dateinamen ist derselbe Eintrag",
   sha3 == sha and stored3 is False,
   "der Dateiname gehoert der Dokumentschicht, nicht dieser")

print("")
print("Der Mandant steht im Pfad")

sha4, stored4 = m.files_put(CLS, a)
ok("derselbe Inhalt bei einem anderen Mandanten wird ERNEUT abgelegt",
   sha4 == sha and stored4 is True,
   "eine mandantenuebergreifende Entdoppelung machte die Speicherkosten "
   "und das Loeschen des einen Kunden von den Daten des anderen abhaengig")
ok("er liegt in seinem eigenen Baum",
   m.files_path(CLS, sha) != m.files_path(DEFAULT, sha)
   and os.path.isfile(m.files_path(CLS, sha)))

fremd = write("nurbeicls.txt", "Nur dieser Kunde hat das\n")
sha5, _ = m.files_put(CLS, fremd)
ok("was nur der eine hat, findet der andere nicht",
   m.files_get(DEFAULT, sha5) is None,
   "schon die Auskunft 'diesen Inhalt gibt es' waere bei vielen "
   "Dokumenten die Information selbst")

ok("ein Hash, der keiner ist, wird gar nicht erst gesucht",
   m.files_get(DEFAULT, "keinhash") is None)
try:
    m.files_put("", a)
    refused = False
except m.FilesRefused as e:
    refused, why = True, str(e)
ok("ohne Mandanten wird abgelehnt, nicht in einen Vorgabe-Topf gelegt",
   refused and "belongs to a tenant" in why, why if refused else "")

print("")
print("Eine beschaedigte Uebertragung wird nicht als Tatsache abgelegt")

try:
    m.files_put(DEFAULT, write("kaputt.txt", "andere bytes\n"),
                expect="0" * 64)
    refused = False
except m.FilesRefused as e:
    refused, why = True, str(e)
ok("ein erwarteter Hash, der nicht stimmt, lehnt ab",
   refused and "damaged" in why, why if refused else "")
ok("und legt die Bytes NICHT unter ihrem echten Hash ab",
   m.files_get(DEFAULT, m._sha256_file(os.path.join(SRC, "kaputt.txt")))
   is None,
   "sonst wuerde aus einer kaputten Uebertragung ein erfolgreicher Upload")

print("")
print("Der Speicher kann gefragt werden, ob er haelt, was er sagt")

checked, problems = m.files_verify()
ok("ein gesunder Speicher meldet nichts", problems == [], problems)
# Drei, nicht vier: derselbe Inhalt liegt bei zwei Mandanten (also
# zweimal), dazu der eine, den nur cls hat. Die abgelehnte Uebertragung
# hat nichts hinterlassen -- und genau das steht eine Pruefung weiter
# oben schon einzeln da.
ok("und zaehlt, was er gelesen hat", checked == 3, checked)

with open(m.files_path(DEFAULT, sha), "w", encoding="utf-8") as f:
    f.write("jemand hat hineingeschrieben\n")
checked, problems = m.files_verify()
ok("stille Veraenderung wird gefunden",
   len(problems) == 1 and sha[:12] in problems[0], problems)
ok("und beim Namen genannt: das ist die Korruption, wofuer es Sicherungen gibt",
   "restore this file from a backup" in problems[0], problems)

# wiederhergestellt, damit die folgenden Pruefungen von ihm ausgehen
os.remove(m.files_path(DEFAULT, sha))
m.files_put(DEFAULT, a)

fremdes = os.path.join(m.files_tenant_dir(DEFAULT), "zz", "keinhash.txt")
os.makedirs(os.path.dirname(fremdes), exist_ok=True)
with open(fremdes, "w", encoding="utf-8") as f:
    f.write("das hat hier nichts zu suchen\n")
_c, problems = m.files_verify()
ok("etwas, das nicht die Plattform geschrieben hat, faellt auf",
   any("not a content hash" in p for p in problems), problems)
os.remove(fremdes)

verlegt = os.path.join(m.files_tenant_dir(DEFAULT), "zz", sha)
os.makedirs(os.path.dirname(verlegt), exist_ok=True)
with open(verlegt, "w", encoding="utf-8") as f:
    f.write("Wartung am 22.09.2026\n")
_c, problems = m.files_verify()
ok("eine Datei im falschen Faecherverzeichnis auch",
   any("wrong fan-out" in p for p in problems), problems)
ok("mit dem Grund, der zaehlt: so wird sie ueber ihren Hash nie gefunden",
   any("will not be found by its hash" in p for p in problems), problems)
os.remove(verlegt)

print("")
print("Und er liegt dort, wo die Sicherung ihn mitnimmt")

# Der Fund beim Bauen, und er ist die Gestalt vom 05.09.2026: „es liegt
# im Datenverzeichnis, also ist es in der Sicherung" stimmt hier NICHT.
# Gesichert wird eine aufgeschriebene LISTE von Pfaden. Ein neues
# Unterverzeichnis ist einer Liste, die niemand nachgezogen hat,
# unsichtbar -- und zwar auf die einzige Art, die zaehlt: der Befehl
# gelingt weiter und das Archiv spielt weiter zurueck.
APPCTL = open(os.path.join(HERE, "..", "platform", "appctl.py"),
              encoding="utf-8").read()
INSTALL = open(os.path.join(HERE, "..", "install.sh"), encoding="utf-8").read()
ok("`oaap backup create` nimmt den Byte-Speicher mit",
   'paths.append("files")' in APPCTL,
   "sonst faellt genau das Verzeichnis heraus, in dem die Dokumente liegen")
ok("bei ausgenommenen Mandanten auch hier je Mandant",
   'f"files/{t}"' in APPCTL,
   "die Bytes eines ausgenommenen Kunden sind seine Bytes (RFC-0029 D5b)")
ok("das Mandantenarchiv traegt sie ebenfalls",
   'os.path.join("files", tid)' in APPCTL)
ok("und die Wiederherstellung holt sie zurueck",
   'RESTORE_PATHS="$RESTORE_PATHS files"' in INSTALL)
ok("gefragt wird dabei das ARCHIV, nicht die Version",
   'tar -tzf "$RESTORE_FILE" files' in INSTALL,
   "ein Archiv von vor 0.1.113 hat kein files/, und ein tar mit einem "
   "Pfad, den es nicht enthaelt, laesst die ganze Wiederherstellung "
   "scheitern")

print("")
print("Was die Kommandozeile sagt")

buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    m.cmd_files(argparse.Namespace(action="status", target=None, tenant="",
                                   sha256=""))
status = buf.getvalue()
ok("`oaap files status` zeigt beide Mandanten",
   "default:" in status and "cls:" in status, status)
ok("und sagt, dass der Speicher schon in der Sicherung ist",
   "in the backup" in status, status)

print("")
print(f"{'FEHLER' if fails else 'Alles gruen'} - {fails} Fehlschlag(e)")
sys.exit(1 if fails else 0)
