# Betrieb: Updates, Sicherheit, Sicherung, Technik

Zurück zur [README](../README.md).

## Updates

**Aktualisieren:** In Proxmox die Konsole des Containers öffnen und `update` eingeben. Einstellungen und Zugangsdaten bleiben erhalten; vorher wird eine Sicherung der verschlüsselten Daten unter `/opt/wealthfolio-broker-sync/backup-*.tar.gz` angelegt (die letzten drei bleiben). Installation und Update prüfen jede heruntergeladene Datei gegen die Prüfsummen (`SHA256SUMS`), die der Release-Workflow mit dem Release veröffentlicht. Passt eine nicht oder ist eine zu viel, brechen sie ab, und die bisherige Version läuft weiter. Eine bestimmte Version, auch eine ältere: `VERSION=<Version> update`. Ohne `VERSION` geht das Update nie auf eine ältere Version zurück.

**Neue Version:** Einmal am Tag fragt der Dienst nach einem Abruf bei GitHub, welches Release das neueste ist (gelesen wird nur die Weiterleitung von `releases/latest`, mitgeschickt wird nichts). Ist es neuer als das installierte, steht das auf der Seite *Broker*, und ntfy meldet es einmal. Abschalten: `Environment=BROKERSYNC_UPDATE_CHECK=0` per `systemctl edit wealthfolio-broker-sync-run`.

**Automatisch aktualisieren (Seite *Updates*, wahlweise):** Eingeschaltet sieht der Container selbst nach, ob ein neues Release erschienen ist – so oft, wie du dort einstellst (mindestens jede Minute, höchstens einmal am Tag) –, und installiert es mit genau dem `update` von oben: dieselbe Prüfung jeder Datei gegen `SHA256SUMS`, dieselbe Sicherung vorher. Danach meldet ntfy, welche Version jetzt läuft, und die Seite *Updates* zeigt das letzte Ergebnis. Schlägt es fehl, läuft die bisherige Version weiter, und dieselbe Version wird erst nach einer Stunde wieder versucht. Während eines Abrufs wird nicht aktualisiert. Gefragt wird dabei nur GitHub nach der neuesten Version. Das erledigt ein Timer des Containers (`wealthfolio-broker-sync-update.timer`, jede Minute, als root, weil er dasselbe tut wie `update`); ausgeschaltet installiert er nichts. Einschalten und Ausschalten fragt nach dem Passwort der Oberfläche. Was er getan hat, zeigt `journalctl -u wealthfolio-broker-sync-update` in der Konsole.

## Sicherheit

Was der Dienst speichert, ist verschlüsselt; die Seite **Sicherheit** zeigt jederzeit, was wie geschützt ist (in der Konsole: `brokersync-cli security-status`).

<picture><source media="(prefers-color-scheme: dark)" srcset="screenshots/11-sicherheit-dunkel.png"><img src="screenshots/11-sicherheit.png" width="560" alt="Seite Sicherheit"></picture>

| Was | Wie |
|---|---|
| Zugangsdaten, Sitzungen, Tokens, Wealthfolio-Passwort, logo.dev-Schlüssel | AES-256-GCM (`data/secrets.enc`) |
| Einstellungen (Konten, Wertpapiere, Überträge mit IBANs) | AES-256-GCM (`data/config.enc`) |
| Sync-Datenbank (Buchungen, Bestände, Läufe) | SQLCipher, AES-256 (`data/state.db`) |
| ntfy-Einstellungen | AES-256-GCM (`data/notify.enc`) – nur mit dem Schlüssel des Dienstes, damit ein gesperrter Dienst Bescheid sagen kann |
| Schlüssel des Dienstes | **nicht** in `data/`: mit `systemd-creds` verschlüsselt in `/etc/wealthfolio-broker-sync/key.cred` (nur root), der Dienst bekommt ihn beim Start |
| Passwort der Oberfläche | argon2id; nach fünf Fehlversuchen 15 Minuten Pause, nach 30 Fehlversuchen von allen zusammen ebenso; höchstens zwei Prüfungen gleichzeitig, damit viele Versuche auf einmal den Speicher nicht erschöpfen. Das erste Passwort nur mit dem Einrichtungscode von der Konsole |
| Oberfläche | HTTPS (Port 8443) mit einem Zertifikat der Container-eigenen CA: einmal importieren (Seite *Sicherheit*), dann vertraut der Browser. Die CA darf nur Namen im lokalen Netz bestätigen (*Name Constraints*), ihr Schlüssel bleibt root-only in `/etc/wealthfolio-broker-sync/tls/`. Ein Zertifikat eines anderen Ausstellers dort (`cert.pem`/`key.pem`) bleibt erhalten. Dazu sichere Cookies und strenge Sicherheits-Header |
| Log, Nachrichten und Fehlermeldungen | Zugangsdaten, Tokens, IBANs und Kontonummern werden geschwärzt – im Log, in ntfy-Nachrichten und in Fehlermeldungen der Oberfläche; Anfrage-Adressen werden nicht protokolliert; ntfy-Nachrichten enthalten ohne *Details* keine Beträge |
| Dienst | eigener Benutzer, systemd-Härtung (kein Schreibzugriff außer `data/`, keine Rechteausweitung, nur die Systemaufrufe eines gewöhnlichen Dienstes; `systemd-analyze security`: 1,5), Dateien nur für den Dienst lesbar; der tägliche Abruf endet spätestens nach 45 Minuten |

**Master-Passphrase (wahlweise, Seite *Sicherheit*):** Ohne sie kann jemand, der den ganzen Container samt Schlüssel kopiert (z. B. ein Proxmox-Backup), die Daten entschlüsseln. Mit ihr nicht: Der Schlüssel entsteht erst aus Schlüssel des Dienstes **und** Passphrase (argon2id). Dafür musst du den Dienst nach jedem Neustart des Containers in der Oberfläche entsperren; bis dahin ruft er nichts ab und schickt eine ntfy-Nachricht. **Ohne die Passphrase sind die Daten verloren** – dann bleibt nur, den Dienst neu einzurichten. Setzen, Ändern und Entfernen der Passphrase (und der Schlüsseltausch beim Update) verschlüsseln alles neu: erst neben den alten Dateien, dann in einem Schritt umgeschaltet. Fällt dabei der Strom aus, bringt der nächste Start die Daten auf den alten oder den neuen Stand, nie auf eine Mischung.

**Ereignisse:** Die Seite *Sicherheit* listet Anmeldungen (auch fehlgeschlagene), Abmeldungen und Änderungen an Zugangsdaten, Passwort, Passphrase und Abrufmodus der letzten 180 Tage, mit der Adresse, von der sie kamen – ohne Passwörter oder Zugangsdaten. Meldet sich jemand von einer Adresse an, von der noch nie eine Anmeldung kam, schickt der Dienst eine ntfy-Nachricht.

## Sichern und Wiederherstellen

`update` sichert `data/` vorher nach `/opt/wealthfolio-broker-sync/backup-*.tar.gz` – nur verschlüsselte Daten, ohne Schlüssel. Jederzeit geht es auch von Hand, in der Konsole des Containers:

```
(umask 077; brokersync-cli backup > /root/brokersync-$(date +%F).tar.gz)
systemctl stop wealthfolio-broker-sync
brokersync-cli restore < /root/brokersync-2026-10-15.tar.gz
systemctl start wealthfolio-broker-sync
```

`restore` prüft zuerst, ob sich die Sicherung mit dem Schlüssel dieses Containers (und der Master-Passphrase, falls gesetzt) öffnen lässt; sonst ändert es nichts. Der bisherige Stand bleibt als `data/before-restore-*.tar.gz` liegen. Das geht auch mit den Sicherungen von `update`.

**Vollständig** ist eine Sicherung nur mit dem Schlüssel aus `/etc/wealthfolio-broker-sync`: Ohne ihn ist `data/` wertlos. Am einfachsten ist deshalb das Proxmox-Backup des ganzen Containers. Es enthält Schlüssel und Daten und gehört auf ein verschlüsseltes Ziel – oder du setzt die Master-Passphrase.

**Abrufmodus (Seite *Sicherheit*):**
- **Automatisch** (Standard): Die Anmeldungen bei Brokern und Banken bleiben gespeichert, der Abruf läuft jeden Morgen von selbst.
- **Sicher:** Jeder Abruf beginnt mit deiner Anmeldung – App-Freigabe, TAN oder Gerätecode. Direkt danach meldet sich der Dienst wieder ab und löscht die Sitzung. Es bleibt also keine Anmeldung gespeichert, mit der jemand ohne dich abrufen könnte.
  - Der tägliche Lauf ruft dann nicht ab, sondern erinnert dich per ntfy.
  - **Alle abrufen** führt nacheinander durch die Anmeldungen (einzelne Broker kannst du überspringen) und ruft danach alle zusammen ab.
  - Der Modus gilt für alle Broker.

**PIN nicht speichern (DKB, Deutsche Bank):** Lässt du das PIN-Feld in den Broker-Einstellungen leer, fragt die Anmeldung jedes Mal nach der PIN. Sie bleibt nur bis zum Ende des Abrufs im Arbeitsspeicher und wird nie gespeichert. Ein Abruf startet dann immer mit der Anmeldung, auch im automatischen Modus; der tägliche Lauf kann diese Bank nicht abrufen. Eine schon gespeicherte PIN löschst du mit dem Häkchen unter dem PIN-Feld.

**Wealthfolio:** Den Dienst mit Wealthfolio möglichst per HTTPS verbinden; sonst geht das Wealthfolio-Passwort unverschlüsselt durchs Netz (die Seite *Sicherheit* weist darauf hin).

## Technik

Für Mitwirkende: [ARCHITECTURE.md](../ARCHITECTURE.md) erklärt Aufbau und Ablauf, [CLAUDE.md](../CLAUDE.md) die Regeln und wie ein neuer Broker dazukommt.

- Python-Dienst `brokersync` (FastAPI-Oberfläche per HTTPS auf Port 8443, Port 8090 leitet weiter; `brokersync run` für den systemd-Timer) unter `/opt/wealthfolio-broker-sync`; verschlüsselte Daten in `/opt/wealthfolio-broker-sync/data`, Schlüssel und Zertifikat in `/etc/wealthfolio-broker-sync`; läuft als eigener Benutzer `brokersync`. Befehle mit dem Schlüssel des Dienstes: `brokersync-cli <befehl>`.
- Wealthfolio wird über seine REST-API (`/api/v1`) mit dem Wealthfolio-Passwort angesprochen.
- Scalables CLI `sc` liegt unter `/opt/wealthfolio-broker-sync/bin/sc`; `brokersync install-sc` installiert bzw. aktualisiert es (Installation und Update rufen es auf).
- Abhängigkeiten werden nur in den Versionen aus `requirements.txt` installiert, jede mit Prüfsumme; die Installations-Engine von community-scripts ist auf einen geprüften Stand festgelegt. Sicherheitslücken bitte privat melden, siehe [SECURITY.md](../SECURITY.md).
- Releases: Ein neuer Stand wird installierbar, sobald die Version in `pyproject.toml` erhöht und nach `main` gemergt ist. Der Release-Workflow lässt dann zuerst die ganze CI laufen und legt erst danach das GitHub-Release an, aus dem Installation und Update laden. Es enthält:
  - das Quellarchiv,
  - `SHA256SUMS` (jede Datei),
  - einen Herkunftsnachweis von GitHub (SLSA Build Provenance); prüfen lässt er sich mit `gh attestation verify wealthfolio-broker-sync-<version>.tar.gz --repo waldonso2/Wealthfolio-broker-sync-service`.

  Veröffentlichte Releases lassen sich nicht mehr ändern.
