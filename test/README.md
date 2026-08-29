# test/

Zwei Arten von Prüfung liegen hier:

- **`vm.sh`** — Test-VM aufsetzen und die Plattform darauf installieren.
  Der Ende-zu-Ende-Weg auf echter Maschine.
- **`test_*.py`** — schnelle Prüfungen der Host-Logik ohne Docker, ohne
  Netz, ohne Maschine. Sie laufen gegen den echten `appctl.py` in einem
  Wegwerf-Datenverzeichnis; Store-Listen liefert ein lokaler
  HTTP-Server im Test selbst, damit nichts gegen eine Fixture-Datei
  auseinanderläuft.
- **`klicktest.py`** — die Oberfläche an einem **laufenden Knoten**:
  anmelden, Store-Katalog, Filter, Objektseite, Quellenverwaltung, die
  Kachel im Launchpad und der Schreibweg über den Spool-Worker. Braucht
  Zugangsdaten in `.env` (nicht im Git) und einen Portal-Benutzer mit
  Rolle `server_admin`.

```bash
python3 test/test_manifest_version.py
python3 test/test_store_sources.py
python3 test/test_store_view.py
python3 test/test_tile.py
python3 test/test_instance_page.py   # braucht jinja2
python3 test/test_artifact_deploy.py
python3 test/test_tenant.py

python3 test/klicktest.py            # braucht einen laufenden Knoten
```

Jede gibt `ALL PASS` bzw. `ALLE PRUEFUNGEN BESTANDEN` aus und endet mit
Rückgabewert 0, sonst 1.

Was sie festhalten, ist jeweils eine **Regel aus einem RFC**, nicht das
heutige Verhalten des Codes: Manifest-Versionstoleranz und
`must_understand` (RFC-0012 §8.2), Quellen als Objekte, Auflösung nach
Vertrauensklasse statt nach Reihenfolge, Bestätigung und Protokoll bei
ungeprüften Quellen, und der Umzug mitgelieferter Quellen (RFC-0012
§2/§3/§4, Befunde B2/B3/B4), sowie die Regeln der Store-Seite: welcher
Eintrag gewinnt, wenn zwei Listen dieselbe App führen, was mit einem
unbekannten Vokabular-Wert geschieht, welche Bilder überhaupt geladen
werden und was standardmäßig ausgefiltert ist (§1.2/§3/§6), sowie wer
eine Kachel im Launchpad bekommt — die App entscheidet über ihr
Manifest, der Betreiber übersteuert je Instanz, und Verstecken ist
ausdrücklich keine Zugriffskontrolle (§1.2 mit Nachtrag zu §1.3,
Runtime-Spec 2.10). Wer eine dieser Regeln ändern will, ändert zuerst
den RFC.

`test_instance_page.py` prüft die **Objektseite einer Instanz** gegen
die Design-Guidelines 6.2.1/6.2.2: Kopfbereich ohne Formular, Reiter als
Links statt JavaScript, genau ein sichtbarer Abschnitt bei allen im
Dokument (fällt das Stylesheet aus, ist die Seite lang statt kaputt),
jedes Formular trägt seinen Reiter mit, die anstehende Bestätigung steht
über den Reitern, und leere Abschnitte begründen ihre Leere. Der Test
liest die Vorlage per `ast` aus `app.py` und rendert sie mit Jinja2 —
ohne Flask, ohne Container. Was er **nicht** prüfen kann, ist der
Rückweg nach dem Speichern (dazwischen liegt der Spool-Worker); das
macht `klicktest.py` an einer echten Maschine.

`test_store_view.py` läuft gegen `store_view.py`, `test_tile.py` gegen
`instance_view.py` — beide Regelwerke liegen bewusst außerhalb von
`app.py`, damit man sie ohne Flask, ohne Anfrage und ohne Knoten lesen
und prüfen kann. `test_tile.py` vergleicht zusätzlich die Antworten von
`appctl.py` und `instance_view.py`: die Regel steht zwangsläufig
zweimal da — auf dem Wirt und im Portal-Container, der `appctl` nicht
importieren kann —, und zwei Kopien driften auseinander.

Was `test_tile.py` **nicht** prüfen kann, weil es Docker bräuchte: dass
die Übersteuerung ein erneutes Deployment übersteht, während die Klasse
selbst aus dem neuen Manifest neu gelesen wird. Das gehört auf eine
echte Maschine.
