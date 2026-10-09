# Wealthfolio Broker Sync

Holt deine Buchungen automatisch bei deinen Brokern ab und trägt sie ohne Duplikate in dein **selbst gehostetes [Wealthfolio](https://wealthfolio.app)** ein. Einmal einrichten, danach läuft der Abruf täglich von selbst.

- **Installation mit einer Zeile** in der Proxmox-Shell, wie bei den [Community-Skripten](https://community-scripts.org): eigener Container, fertig eingerichtet
- **Alles in der Weboberfläche:** keine Konfigurationsdateien, keine Kommandozeile
- **Zugangsdaten verschlüsselt** gespeichert, **nur lesender Zugriff** auf die Broker
- **Benachrichtigung aufs Handy** (ntfy), wenn eine TAN fällig ist oder etwas nicht klappt
- **Bucht wie das [Broker Importer Addon](https://github.com/waldonso2/wealthfolio-importer-addon)**: Was du schon per CSV oder PDF importiert hast, wird erkannt und nicht doppelt angelegt

> **Stand:** Version 0.1 ist die Basis mit einem **Test-Broker („Dummy“)**, mit dem du alles einmal ausprobieren kannst. Die echten Broker folgen: DKB ([#37](https://github.com/waldonso2/wealthfolio-importer-addon/issues/37)), Trade Republic ([#38](https://github.com/waldonso2/wealthfolio-importer-addon/issues/38)), Scalable Capital ([#39](https://github.com/waldonso2/wealthfolio-importer-addon/issues/39)).

## Was du brauchst

- einen **Proxmox-Server** (VE 8 oder 9)
- **Wealthfolio** als selbst gehostete Version (z. B. über das Community-Skript „Wealthfolio“) und das Wealthfolio-Passwort
- in Wealthfolio je Broker **zwei Konten**: ein Verrechnungskonto (Cash) und ein Depotkonto – dieselben wie beim Broker Importer Addon. Für den Test mit dem Dummy legst du am besten zwei Testkonten an, z. B. „Test Cash“ und „Test Depot“.
- optional die App **ntfy** auf dem Handy ([Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy), [iOS](https://apps.apple.com/app/ntfy/id1625396347))

## Installation

1. **Proxmox-Shell öffnen.** In der Proxmox-Oberfläche links deinen Server (Knoten) wählen, dann oben rechts *>_ Shell*.
2. **Diese Zeile einfügen und Enter drücken:**

   ```bash
   bash -c "$(curl -fsSL https://raw.githubusercontent.com/waldonso2/wealthfolio-broker-sync-service/main/ct/wealthfolio-broker-sync.sh)"
   ```

   Es erscheint der bekannte Assistent der Community-Skripte. *Default Settings* wählen – die Standardwerte passen: 1 CPU, 512 MB RAM, 2 GB Disk, Debian 13, unprivilegiert. Der Kopf zeigt dabei „Scripts fork: waldonso2/wealthfolio-broker-sync-service“; das ist richtig, der Dienst kommt aus diesem Repository und nicht aus der offiziellen Sammlung.
3. **Öffnen.** Am Ende zeigt die Installation die Adresse der Weboberfläche an, z. B. `http://192.168.1.51:8090`. Diese im Browser öffnen.

**Aktualisieren:** In Proxmox die Konsole des Containers öffnen und `update` eingeben. Einstellungen und Zugangsdaten bleiben erhalten; vorher wird eine Sicherung unter `/opt/wealthfolio-broker-sync/backup-*.tar.gz` angelegt (die letzten drei bleiben).

> **PVE Scripts Local:** Der Dienst erscheint dort (noch) nicht. PVE Scripts Local zeigt nur Skripte aus der offiziellen Sammlung von community-scripts.org an; ein unter *Repositories* eingetragenes eigenes Repo bringt keine neuen Skripte in den Katalog. Die Aufnahme in die offizielle Sammlung ist geplant, sobald echte Broker angebunden sind.

## Einrichtung im Browser

### 1. Passwort für die Oberfläche festlegen

Beim ersten Öffnen legst du ein Passwort fest. Es schützt deine Broker-Zugänge vor anderen im Heimnetz.

<img src="docs/screenshots/01-passwort.png" width="560" alt="Passwort festlegen">

Danach zeigt die Übersicht, was noch zu tun ist:

<img src="docs/screenshots/02-uebersicht.png" width="560" alt="Übersicht mit den Einrichtungsschritten">

### 2. Wealthfolio verbinden

Die Adresse, unter der du Wealthfolio öffnest, und das Wealthfolio-Passwort. Nach dem Speichern wird die Verbindung sofort getestet.

<img src="docs/screenshots/03-wealthfolio.png" width="560" alt="Wealthfolio verbinden">

### 3. Broker einrichten

Zugangsdaten eintragen und die beiden Wealthfolio-Konten wählen. Mit *Buchungen übernehmen ab* bestimmst du, wie weit der erste Abruf zurückgeht. *Automatisch abrufen* anhaken.

<img src="docs/screenshots/04-broker.png" width="560" alt="Broker einrichten">

### 4. Beim Broker anmelden

Nach dem Speichern meldet sich der Dienst beim Broker an. Will der Broker eine TAN oder eine Bestätigung in seiner App, fragt die Seite danach. (Beim Dummy lautet der Code `000000`.)

<img src="docs/screenshots/05-tan.png" width="560" alt="TAN eingeben">

### 5. Benachrichtigungen

ntfy-App öffnen, *Thema abonnieren* und den Themennamen von dieser Seite eintragen. Mit *Testnachricht senden* prüfen.

<img src="docs/screenshots/06-benachrichtigungen.png" width="560" alt="Benachrichtigungen einrichten">

### 6. Erster Abruf

In der Übersicht **Jetzt abrufen** klicken. Danach siehst du je Broker, was neu angelegt wurde und was schon in Wealthfolio war.

<img src="docs/screenshots/07-nach-dem-abruf.png" width="560" alt="Übersicht nach dem ersten Abruf">

Ab jetzt läuft der Abruf jeden Morgen zwischen 6:00 und 6:45 Uhr.

## Im Alltag

| Was passiert | Was du tust |
|---|---|
| Push-Nachricht „Anmeldung nötig“ | Auf die Nachricht tippen, TAN eingeben – fertig |
| Push-Nachricht „Abruf fehlgeschlagen“ | Übersicht öffnen, dort steht der Grund. Meist löst es sich beim nächsten Lauf von selbst |
| „unbekannte Buchungen“ | Der Dienst kennt eine Buchungsart noch nicht und hat sie **nicht** übernommen. Unter *Unbekannte Buchungen* steht, was es war – bei Bedarf von Hand in Wealthfolio eintragen und gern ein Issue mit dem Typ anlegen |
| Wertpapier soll in Wealthfolio unter seinem Ticker statt der ISIN laufen | Unter *Wertpapiere* die Zuordnung ISIN → Symbol eintragen (wie im Addon) |
| Passwort der Oberfläche vergessen | In Proxmox die Konsole des Containers öffnen und `brokersync-reset-password` eingeben. Beim nächsten Öffnen legst du ein neues fest |

## Wie gebucht wird

Genau wie beim Broker Importer Addon, damit sich Sync, CSV- und PDF-Import nicht in die Quere kommen:

- **Zwei Konten je Broker:** Käufe, Verkäufe und Dividenden auf dem Depotkonto, alles andere auf dem Verrechnungskonto. Das Geld für einen Kauf und der Erlös eines Verkaufs bzw. einer Dividende wandern als Übertrag (TRANSFER_OUT/TRANSFER_IN, verknüpft über `sourceGroupId`) zwischen den Konten, sodass auf dem Depotkonto kein Bargeld liegen bleibt. Wealthfolio zählt diese Überträge nicht als Ausgaben.
- **Gebühren und Steuern** stehen in den eigenen Feldern der Buchung; eine Dividende ist eine Buchung mit dem Nettobetrag und der Steuer. Eine Steuererstattung wird eine eigene Gutschrift (CREDIT/TAX_REFUND).
- **Keine Duplikate:**
  1. Der Dienst merkt sich jede übernommene Transaktion.
  2. Wealthfolio lehnt exakt gleiche Buchungen selbst ab.
  3. Vor dem Anlegen wird geprüft, ob es die Transaktion schon gibt, z. B. aus einem CSV- oder PDF-Import: gleiches Konto, gleiche Art und gleiches Wertpapier, höchstens 36 Stunden auseinander, gleiche Stückzahl, gleicher Betrag ±0,02.

  Bricht ein Lauf mittendrin ab, ergänzt der nächste die fehlenden Teile.
- Jede Buchung trägt im Kommentar `[SYNC <broker>:<id>]`.

## Technik

Für Mitwirkende: [CLAUDE.md](CLAUDE.md) beschreibt Aufbau, Regeln und wie ein neuer Broker dazukommt.

- Python-Dienst `brokersync` (FastAPI-Oberfläche auf Port 8090, `brokersync run` für den systemd-Timer) unter `/opt/wealthfolio-broker-sync`; Daten in `/opt/wealthfolio-broker-sync/data` (Konfiguration, verschlüsselte Zugangsdaten, Sync-Status), läuft als eigener Benutzer `brokersync`.
- Wealthfolio wird über seine REST-API (`/api/v1`) mit dem Wealthfolio-Passwort angesprochen.
- Releases: Ein neuer Stand wird installierbar, sobald die Version in `pyproject.toml` erhöht und nach `main` gemergt ist; der Release-Workflow legt dann das GitHub-Release an, aus dem Installation und Update laden.

## Lizenz

MIT
