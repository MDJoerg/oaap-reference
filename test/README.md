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
  anmelden, Store-Katalog, Filter, Objektseite, Quellenverwaltung und
  der Schreibweg über den Spool-Worker. Braucht Zugangsdaten in `.env`
  (nicht im Git) und einen Portal-Benutzer mit Rolle `server_admin`.

```bash
python3 test/test_manifest_version.py
python3 test/test_store_sources.py
python3 test/test_store_view.py

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
werden und was standardmäßig ausgefiltert ist (§1.2/§3/§6). Wer eine
dieser Regeln ändern will, ändert zuerst den RFC.

`test_store_view.py` läuft gegen `store_view.py` — die Katalogregeln
liegen bewusst außerhalb von `app.py`, damit man sie ohne Flask, ohne
Anfrage und ohne Knoten lesen und prüfen kann.
