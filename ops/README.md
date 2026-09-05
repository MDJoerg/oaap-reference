# ops/ — Betriebsskripte für unsere eigene Flotte

Hier liegt, was **noch keine Plattform-Fähigkeit ist**: Werkzeuge, mit
denen wir unsere eigenen Knoten betreiben, bevor die Form bewiesen ist.

Die Reihenfolge ist Absicht. `oaap.data.backup` 0.1 kann genau eine
Sache — ein vollständiges Archiv auf Zuruf. Zeitplan, Aufbewahrung,
Auslagerung und Sichtbarkeit stehen dort ausdrücklich als „spätere
Version". Diese Skripte sind der **Probelauf** dafür: Sie machen die
Sache jetzt, an echten Knoten, mit echten Daten — und was sich dabei
als richtig erweist, wandert danach in die Spezifikation und in die
Plattform, statt vorher geraten zu werden. Genauso ist die
Instanz-Konfiguration entstanden.

*English summary:* operator scripts for our own fleet — a nightly full
platform backup on a node, and a pull of those archives to a second
node's off-site storage, with generation retention. They are the trial
run for `oaap.data.backup` 0.2; nothing here is a platform capability
yet.

## Die Aufteilung

```text
  oaapx01 (extern, beim Hoster)          oaap-demo (intern, hinter der Firewall)
  ───────────────────────────────        ────────────────────────────────────────
  backup-nightly.sh   (Timer 03:30)      backup-pull.sh   (Timer 04:30)
    oaap backup create                     holt per SSH ab
    hält N Archive lokal                   prüft die Prüfsumme
    schreibt status.json                   legt Generationen an  → /mnt/backup
                                           schreibt status.json
```

**Die Richtung ist die Sicherheitsaussage.** Der interne Knoten holt,
der externe gibt nichts nach innen: `oaapx01` hält keinen Schlüssel ins
Heimnetz, und wer `oaapx01` übernimmt, kommt darüber nicht an die
Sicherungen. Der Preis: Der abholende Knoten muss den gebenden
erreichen. Für einen Knoten hinter fremder Firewall (Bernd) trägt das
nicht — dafür ist der umgekehrte Weg vorgesehen, siehe „Andersherum"
unten.

## Einrichten

Auf dem Knoten, der gesichert wird:

```sh
sudo bash ops/install-backup-timer.sh              # 03:30 lokal, 2 Archive lokal
sudo bash ops/install-backup-timer.sh --at 02:00 --keep 3
sudo oaap-backup-nightly                           # einmal von Hand
systemctl list-timers oaap-backup.timer
```

Auf dem abholenden Knoten:

```sh
# einmalig: eigenes Schlüsselpaar, öffentlichen Teil beim Gebenden hinterlegen
ssh-keygen -t ed25519 -N "" -C "backup-pull@oaap-demo" -f ~/.ssh/oaap_backup_pull
# beim Gebenden in ~/.ssh/authorized_keys, mit Zwang auf genau einen Befehl:
#   command="/usr/local/bin/oaap-backup-serve",restrict ssh-ed25519 AAAA... backup-pull@oaap-demo
sudo bash ops/install-backup-pull.sh --node oaapx01 --host oaap.joomp.de \
     --user oaap-admin --key ~/.ssh/oaap_backup_pull --to /mnt/backup
```

## Aufbewahrung

Gesichert wird **immer vollständig** — es gibt keine inkrementellen
Stufen, und das ist eine Entscheidung, keine Lücke: Ein Vollarchiv ist
für sich allein wiederherstellbar, eine Kette ist es nur so weit, wie
ihr schwächstes Glied reicht. Bei ~9 GB Nutzdaten und 1,2 TB Platz
kostet uns diese Einfachheit nichts.

„Wöchentlich" und „monatlich" sind daher **aufgehobene Generationen**,
keine anderen Sicherungsarten:

| Generation  | Woraus                        | Voreinstellung |
| ----------- | ----------------------------- | -------------- |
| `daily/`    | jeder Lauf                    | 7 Stück        |
| `weekly/`   | der Lauf von Sonntag          | 4 Stück        |
| `monthly/`  | der Lauf vom Ersten des Monats| 6 Stück        |

Auf dem gebenden Knoten bleiben die **N neuesten** Archive liegen
(Voreinstellung 2) — nicht null. Ein Archiv, das sofort nach der
Übertragung gelöscht wird, lässt bei einer stillen Beschädigung der
Kopie nichts übrig; zwei Generationen vor Ort sind der billigste
Rückweg, und sie kosten Plattenplatz, der da ist.

## Was in einem Archiv steckt — und was das bedeutet

Alles, was die Plattform nicht selbst wiederherstellen kann: Benutzer
samt Passwort-Hashes, die Instanz-Registry, **alle Geheimnisse im
Klartext** (`instance.env`, Deploy-Token, fremde Zugangsschlüssel), der
Mandantenspeicher und die Daten jeder Instanz — **aller Mandanten**.

Zwei Folgerungen, die man aussprechen muss:

1. Das Ziel muss **exklusiv berechtigt** sein. Bei einer CIFS-Freigabe
   wirkt `file_mode` nur clientseitig und schützt gar nichts; es zählt
   allein, wer die Freigabe mounten darf (Befund 2026-08-07).
2. Ein Ganzknoten-Archiv trägt **Kundendaten** an den Ort des
   Betreibers. Auf einem Knoten mit mehreren Mandanten ist das eine
   bewusste Entscheidung, keine technische Nebenwirkung — und der Grund,
   warum „Sicherung je Mandant" in RFC-0029 als eigene Frage steht.

## Andersherum (vom gesicherten Knoten ausgelöst)

Ist vorgesehen und hier bewusst noch nicht gebaut. Sie wird gebraucht,
sobald ein Knoten hinter fremder Firewall steht und niemand ihn
erreichen kann — Bernds Werkstatt ist genau dieser Fall. Dann schiebt
der Knoten selbst, und die Sicherheitsaussage dreht sich mit: Er hält
dann ein Zugangsrecht zum Ziel, also darf dieses Recht **nur anlegen,
nie lesen und nie löschen**. RFC-0029 entscheidet die Form, bevor sie
gebaut wird.

## Wiederherstellen

Der Weg zurück ist unverändert `oaap.data.backup` 0.1: eine vorbereitete
Maschine plus Archiv, Installer im Modus `restore`. Er baut **alle**
Instanzen neu — einzeln geht heute nicht.

**Eine ungeprüfte Sicherung ist keine.** Der Probelauf gehört in den
Kalender, nicht in die Absicht.
