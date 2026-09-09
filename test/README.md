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
python3 test/test_deploy_state.py
python3 test/test_tenant.py
python3 test/test_tenant_boundary.py
python3 test/test_rehearsal_shape.py
python3 test/test_rehearsal_data.py
python3 test/test_rehearsal_page.py  # braucht jinja2
python3 test/test_data_store.py

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

`test_rehearsal_shape.py`, `test_rehearsal_data.py` und
`test_rehearsal_page.py` halten die **Generalprobe** fest (RFC-0030,
Runtime-Spec 2.15) — in genau der Reihenfolge, in der sie gebaut wurde,
und die ist keine Geschmacksfrage:

- **Form zuerst.** `test_rehearsal_shape.py` prüft die vier
  Verweigerungen gegen ein Original, das **all das hat**: eigene
  Adresse, Alias, öffentliche Route, App-Verknüpfung und einen
  gesetzten `secret: true`-Wert. Gegen ein Original ohne diese Dinge
  würde man nur prüfen, dass aus nichts nichts wird. Wer die Kopie vor
  den Verweigerungen baut, hat die gefährliche Fassung gebaut.
  Besonders: Die `public`-Regel greift dort, wo die Gateway-Site
  geschrieben wird, und `site_body()` sucht die Antwort **selbst**,
  wenn ein Aufrufer sie nicht mitgibt — der eine vergessene Aufrufer
  fällt so in die sichere Richtung.
- **Dann die Daten.** `test_rehearsal_data.py` prüft die drei
  Fallstricke, die dieses Projekt schon einmal Geld gekostet haben: die
  alte Kennung in den Archivpfaden, `--numeric-owner` beim Entpacken,
  und die `instance.env`, die **mitkommt** und danach entleert werden
  muss. Dazu der Ablauf und die Sperre, die nicht fehlen darf: Der
  Sweep fasst **nur** Instanzen mit `rehearsal`-Block an — eine
  gewöhnliche Instanz mit einem `expires` im Datensatz überlebt ihn.
  Und er geht durch den **echten Parser** (0.1.78: eine Fähigkeit, die
  in den `choices` fehlt, ist keine), mit Gegenprobe.
- **Zuletzt die Seite.** `test_rehearsal_page.py` prüft den Satz, den
  die Seite schuldet — wie groß die Kopie wird und wie viel danach frei
  ist, mit den Zahlen **dieses** Knotens —, dass ein Knoten ohne Messung
  das sagt statt sich eine Zahl zu leihen, und dass das Portal die
  Ansicht neben der Registry liest und **keinen Mount in den
  Mandantenbaum** hat.

`test_data_store.py` hält den **Store** fest (`oaap.data.store` 0.1,
RFC-0031 Schritt 1) — ohne Docker, also ohne das echte Postgres: dass
ohne das Knotenprofil `store` jede Aktion "nicht getragen" statt eines
Fehlers meldet, dass mit Profil, aber ohne laufenden Dienst, jede
Aktion verweigert wird statt ein `docker exec` gegen nichts zu
versuchen, dass `remove-profile store` den Code kennt, der bestehende
Schemas schützt, und dass der Compose-Dienst wirklich hinter
`profiles: [store]` gattert und keinen Port veröffentlicht. Dazu die
Kollisionsprüfung, die beim Bauen den Anlass gab: `oaap store` (Paket-
Store, RFC-0012) und `oaap data store` (dieser Dienst) sind zwei
Befehle, nicht einer. Was diese Datei **nicht** prüfen kann, weil es
ein echtes Postgres bräuchte — dass `create`/`copy`/`drop`/`restore`
tatsächlich ein Schema anlegen, kopieren, löschen oder wiederherstellen
— gehört auf `oaap-test`, wie bei `klicktest.py`.
