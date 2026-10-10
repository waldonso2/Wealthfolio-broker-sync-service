# Wealthfolio Broker Sync

Holt deine Buchungen automatisch bei deinen Brokern ab und trägt sie ohne Duplikate in dein **selbst gehostetes [Wealthfolio](https://wealthfolio.app)** ein. Einmal einrichten, danach läuft der Abruf täglich von selbst.

- **Installation mit einer Zeile** in der Proxmox-Shell, wie bei den [Community-Skripten](https://community-scripts.org): eigener Container, fertig eingerichtet
- **Alles in der Weboberfläche:** keine Konfigurationsdateien, keine Kommandozeile
- **Zugangsdaten verschlüsselt** gespeichert, **nur lesender Zugriff** auf die Broker
- **Benachrichtigung aufs Handy** (ntfy), wenn eine TAN fällig ist oder etwas nicht klappt
- **Bucht wie das [Broker Importer Addon](https://github.com/waldonso2/wealthfolio-importer-addon)**: Was du schon per CSV oder PDF importiert hast, wird erkannt und nicht doppelt angelegt

> **Stand:** **Trade Republic**, **DKB** (Girokonto per FinTS), **Scalable Capital** (über Scalables offizielles CLI) und **comdirect** (Girokonto und Depot über die offizielle REST-API; neu, noch nicht lange im Einsatz).

## Was du brauchst

- einen **Proxmox-Server** (VE 8 oder 9)
- **Wealthfolio** als selbst gehostete Version (z. B. über das Community-Skript „Wealthfolio“) und das Wealthfolio-Passwort
- in Wealthfolio je Broker **zwei Konten**: ein Verrechnungskonto (Cash) und ein Depotkonto – dieselben wie beim Broker Importer Addon.
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

Zugangsdaten eintragen und die beiden Wealthfolio-Konten wählen. Mit *Buchungen übernehmen ab* bestimmst du, wie weit der erste Abruf zurückgeht; danach holt jeder Abruf ab dem letzten (minus 7 Tage). Soll später doch ein früherer Zeitraum dazukommen, Startdatum ändern und *Ab Startdatum neu abrufen* klicken – schon übernommene Buchungen werden nicht doppelt angelegt. Scalable liest bei jedem Abruf alles ab dem Startdatum, dort ist der Knopf nicht nötig. *Automatisch abrufen* anhaken.

<img src="docs/screenshots/04-broker.png" width="560" alt="Broker einrichten">

### 4. Beim Broker anmelden

Nach dem Speichern meldet sich der Dienst beim Broker an. Will der Broker eine TAN oder eine Bestätigung in seiner App, fragt die Seite danach.

<img src="docs/screenshots/05-tan.png" width="560" alt="TAN eingeben">

### 5. Benachrichtigungen

ntfy-App öffnen, *Thema abonnieren* und den Themennamen von dieser Seite eintragen. Mit *Testnachricht senden* prüfen.

<img src="docs/screenshots/06-benachrichtigungen.png" width="560" alt="Benachrichtigungen einrichten">

### 6. Erster Abruf

In der Übersicht **Alle abrufen** klicken (oder bei einem Broker **Jetzt abrufen**). Danach siehst du je Broker, was neu angelegt wurde und was schon in Wealthfolio war. Broker mit ausgeschaltetem *Automatisch abrufen* überspringt der tägliche Lauf und „Alle abrufen“ – sie rufst du mit ihrem eigenen Button ab.

<img src="docs/screenshots/07-nach-dem-abruf.png" width="560" alt="Übersicht nach dem ersten Abruf">

Ab jetzt läuft der Abruf jeden Morgen zwischen 6:00 und 6:45 Uhr.

## DKB

Der Dienst liest dein **DKB-Girokonto** per FinTS (HBCI), dieselbe Schnittstelle, die Finanzprogramme wie Hibiscus oder MoneyMoney nutzen. Er liest nur; Überweisungen kann er nicht auslösen.

**Einrichten** unter *Broker → DKB*:

- **Anmeldename und PIN:** dieselben wie im DKB-Banking.
- **IBAN des Girokontos:** nur nötig, wenn du bei der DKB mehrere Konten hast.
- **FinTS-Produkt-ID:** Banken verlangen für FinTS eine bei der Deutschen Kreditwirtschaft registrierte Produkt-ID. Sobald dieser Dienst eine hat, ist sie fest eingebaut und das Feld entfällt. Bis dahin trägst du hier eine registrierte Produkt-ID ein.
- **Konten:** dasselbe DKB-Verrechnungs- und Depotkonto wie beim PDF-Import im Addon.

**Freigabe in der DKB-App:** Bei der ersten Anmeldung und danach in Abständen (nach den PSD2-Regeln meist alle 90 Tage) will die DKB eine Bestätigung in der DKB-App. In der Oberfläche bestätigst du in der App und klickst dann *Ich habe in der App bestätigt*. Läuft gerade der tägliche Abruf, schickt der Dienst eine ntfy-Nachricht und wartet drei Minuten auf deine Freigabe.

**Was gebucht wird:**

| Auf dem Girokonto | In Wealthfolio (DKB-Verrechnungskonto) |
|---|---|
| Gutschrift, Gehalt, eingehende Überweisung | Einzahlung (DEPOSIT) – eingehendes Geld ist immer eine Einzahlung, wie im Addon |
| Kartenzahlung, Lastschrift, ausgehende Überweisung | Auszahlung (WITHDRAWAL), also eine Ausgabe |
| Ausgehende Überweisung auf ein eigenes Konto | Übertrag (TRANSFER_OUT, mit Gegenbuchung auf dem gewählten Konto), wenn unter *Überträge* eingetragen – wie die Transfer-Muster im Addon |
| Habenzinsen / Kontoführungsentgelt beim Rechnungsabschluss | Zinsen (INTEREST) / Gebühr (FEE) |
| Wertpapierabrechnung, Ertragsgutschrift, Dividende | **nichts** – siehe unten |

**Wertpapiere:** Käufe, Verkäufe und Ausschüttungen deines DKB-Depots bucht der PDF-Import des Addons, samt der Abbuchung bzw. Gutschrift auf dem Verrechnungskonto. Damit nichts doppelt zählt, bucht der Dienst diese Girokonto-Umsätze nicht noch einmal. Er prüft nur, ob der PDF-Import sie schon gebucht hat (gleicher Betrag, höchstens 6 Tage auseinander). Fehlt das Gegenstück, bekommst du eine Nachricht: PDF-Abrechnung mit dem Addon importieren, der nächste Abruf erkennt sie dann. Ob die DKB Depotbestände über FinTS liefert, ist noch nicht geprüft; bis dahin bleibt dafür der PDF-Import.

**Zeitraum:** Ohne Datum unter *Buchungen übernehmen ab* holt der erste Abruf die letzten 89 Tage. Für ältere Umsätze verlangt die DKB eine Freigabe in der App.

**Saldo:** Die Übersicht zeigt nach jedem Abruf den Saldo laut DKB. Er sollte mit dem DKB-Verrechnungskonto in Wealthfolio übereinstimmen.

**Sicherheit:** Lehnt die DKB Anmeldename oder PIN ab, versucht der Dienst es nicht noch einmal, bis du die Zugangsdaten neu speicherst. Nach drei Fehlversuchen sperrt die DKB sonst das Online-Banking.

## Trade Republic

Der Dienst liest deine Trade-Republic-Timeline über die inoffizielle Schnittstelle der App, mit dem Open-Source-Projekt [pytr](https://github.com/pytr-org/pytr). Er liest nur; Orders oder Auszahlungen kann er nicht auslösen.

> **Bitte beachten:** Trade Republic bietet keine offizielle Schnittstelle. Die genutzte kann sich jederzeit ändern, dann klappt der Abruf bis zu einem Update nicht. Ein automatisierter Zugriff ist von Trade Republic vermutlich nicht vorgesehen. Wer das nicht möchte, nutzt weiter den CSV-Import im Addon.

**Einrichten** unter *Broker → Trade Republic*:

- **Telefonnummer** mit Ländervorwahl (z. B. `+4917612345678`) und die **PIN** der App.
- **Konten:** dasselbe Trade-Republic-Verrechnungs- und Depotkonto wie beim CSV-Import im Addon.

**Anmelden:** Die Anmeldung läuft wie im Browser auf app.traderepublic.com. Trade Republic schickt eine Anfrage in die App, die bestätigst du, und dann klickst du *Ich habe in der App bestätigt*. Die Handy-App bleibt dabei angemeldet. Nutzt dein Konto eine Authenticator-App, fragt die Seite nach deren Code. Die Sitzung hält eine Weile; läuft sie ab, schickt der tägliche Abruf eine ntfy-Nachricht und wartet zwei Minuten auf deine Bestätigung in der App.

**Was gebucht wird** – nach denselben Regeln wie der CSV-Import des Addons:

| In der Timeline | In Wealthfolio |
|---|---|
| Kauf, Sparplan, Verkauf | Kauf/Verkauf auf dem Depotkonto mit Gebühr und Steuer in eigenen Feldern, Geld per Übertrag vom bzw. zum Verrechnungskonto |
| Dividende, Ausschüttung | eine Dividende mit Nettobetrag und Quellensteuer, Geld per Übertrag aufs Verrechnungskonto |
| Saveback | Bonus (CREDIT/BONUS) auf dem Depotkonto, der den Kauf bezahlt – ohne Abbuchung vom Verrechnungskonto |
| Zinsen | Zinsen (INTEREST) mit Steuer |
| Vorabpauschale / Steuerkorrektur | Steuer (TAX) / Steuererstattung (CREDIT/TAX_REFUND) |
| Einzahlung, Kartenerstattung | Einzahlung (DEPOSIT) |
| Kartenzahlung, Überweisung | Auszahlung (WITHDRAWAL) oder – mit Eintrag unter *Überträge* – Übertrag aufs eigene Konto |
| Aktiensplit, Spin-off, Tausch, Depotübertrag, Private Markets | **nicht gebucht**, als unbekannt gemeldet – diese Kapitalmaßnahmen bildet der CSV-Import im Addon ab |

Reine Hinweise (Order angelegt/storniert, Dokumente, Adressänderung …) und stornierte Buchungen übernimmt der Dienst nicht.

**CSV-Import und Dienst zusammen:** Was du schon per CSV importiert hast, erkennt der Dienst und legt es nicht noch einmal an: gleiche Art, gleicher Betrag, höchstens 36 Stunden auseinander, bei Käufen und Verkäufen dieselbe Stückzahl. Das Wertpapier darf dabei unter einem anderen Symbol stehen (im Addon zugeordneter Ticker, im Dienst die ISIN).

**Duplikate aus Version 0.3.0/0.3.1:** Diese Versionen haben per CSV importierte Käufe, Verkäufe und Dividenden ein zweites Mal angelegt. Die Seite *Duplikate* zeigt sie neben der CSV-Buchung und löscht nach deiner Bestätigung nur die Kopie des Dienstes.

## Scalable Capital

Der Dienst liest dein Scalable-Depot über **Scalables offizielles Kommandozeilenprogramm** [Scalable CLI](https://github.com/ScalableCapital/scalable-cli) (`sc`). Das ist eine von Scalable selbst angebotene Schnittstelle – kein Nachbau der Web-App. Installation und Update laden `sc` aus Scalables GitHub-Release und prüfen vorher Scalables Signatur und die Prüfsumme. Der Dienst nutzt nur lesende Befehle und meldet das CLI im Nur-Lese-Modus an (`--local-read-only`); Orders oder Sparpläne kann er nicht auslösen.

> **Bitte beachten:** Das Scalable CLI ist laut Scalable noch in der Beta und ohne Support. Ändert Scalable es, klappt der Abruf bis zu einem Update des Dienstes nicht.

**Vorher einmal in Scalable:** Im **Webportal** unter *Profil → Sicherheit → Agentic Investing* die Scalable CLI aktivieren. Ohne das lehnt Scalable die Anmeldung ab, und der Dienst sagt dir das.

**Einrichten** unter *Broker → Scalable Capital*:

- **Depot-ID** nur, wenn du mehrere Scalable-Depots hast: die `portfolioId` aus der Adresse der Depot-Seite im Browser. Mit einem Depot leer lassen.
- **Konten:** dasselbe Scalable-Verrechnungs- und Depotkonto wie beim CSV-Import im Addon.

**Anmelden:** Die Seite zeigt einen Link und einen Code. Link öffnen, bei Scalable anmelden, den Code bestätigen und dann *Ich habe bestätigt* klicken. Die Sitzung erneuert sich bei jedem Abruf selbst und liegt verschlüsselt im Tresor des Dienstes. Will Scalable doch eine neue Anmeldung, schickt der tägliche Abruf Link und Code per ntfy und wartet zehn Minuten auf deine Bestätigung.

**Was gebucht wird** – nach denselben Regeln wie der CSV-Import des Addons:

| Bei Scalable | In Wealthfolio |
|---|---|
| Kauf, Sparplan, Verkauf | Kauf/Verkauf auf dem Depotkonto mit Gebühr (Order-, Handelsplatz- und Krypto-Spread-Gebühr) und Steuer in eigenen Feldern, Geld per Übertrag vom bzw. zum Verrechnungskonto |
| Ausschüttung | eine Dividende mit Nettobetrag und – wenn Scalable sie nennt – Steuer; Stückzahl 1, weil die Schnittstelle keine nennt (wie der CSV-Import). Geld per Übertrag aufs Verrechnungskonto |
| Zinsen | Zinsen (INTEREST) |
| Steuer (z. B. Vorabpauschale) / Steuererstattung | Steuer (TAX) / Steuererstattung (CREDIT/TAX_REFUND) |
| Gebühr (z. B. PRIME+) | Gebühr (FEE) |
| Einzahlung / Auszahlung | Einzahlung (DEPOSIT) / Auszahlung (WITHDRAWAL) oder – mit Eintrag unter *Überträge* – Übertrag aufs eigene Konto |
| Depotumzug (Wertpapiere aus- und wieder eingebucht, Bargeld mit `SWITCH-`) | **nicht gebucht** – heben sich auf, wie beim CSV-Import |
| Fondstausch (Wertpapiere und Gutschrift `SWAP_OUT` am selben Tag) | Verkauf zum gutgeschriebenen Betrag, wie beim CSV-Import |
| Rückzahlung/Knock-out eines Zertifikats (Ausbuchung mit Wert 0 plus Ausschüttung am selben Tag) | Verkauf zum ausgezahlten Betrag, wie beim CSV-Import |
| Storno einer Ausschüttung | Storno und ursprüngliche Ausschüttung heben sich auf. Hatte der Dienst die Ausschüttung schon übernommen, meldet er das Storno als unbekannt – dann die Ausschüttung in Wealthfolio von Hand löschen |
| andere Stornos, einzelner Wertpapierübertrag, ELTIF, negative Zinsen, Gebührenerstattung | **nicht gebucht**, als unbekannt gemeldet – bitte von Hand prüfen bzw. eintragen |

Offene Orders übernimmt der Dienst erst, wenn sie ausgeführt sind. Der erste Abruf der ganzen Historie dauert einige Minuten: Scalable begrenzt, wie schnell die Details der Käufe und Verkäufe abgefragt werden dürfen. Wird es trotzdem zu viel, bucht der Dienst, was er hat, und holt den Rest beim nächsten Abruf. Kommt nichts an, steht im Log (`journalctl -u wealthfolio-broker-sync -n 50`), wie viele Transaktionen welcher Art Scalable geliefert hat – ohne Beträge oder Namen. Was du schon per CSV importiert hast, erkennt er wie bei Trade Republic.

## comdirect

Der Dienst liest Girokonto **und Depot** über die **offizielle REST-API der comdirect** für Privatkunden – nur lesend; Order- und Überweisungs-Schnittstellen ruft er nie auf.

**Vorher einmal bei comdirect:** Im comdirect-Banking unter *Persönlicher Bereich → Zugänge verwalten* die **REST API** freischalten. Dort bekommst du **Client-ID** und **Client-Secret**. Als TAN-Verfahren empfiehlt sich **photoTAN-Push**: Dann bestätigst du die Anmeldung einfach in der photoTAN-App.

**Einrichten** unter *Broker → comdirect*: Zugangsnummer, PIN, Client-ID, Client-Secret; die IBAN nur, wenn du mehrere Girokonten hast. Als Konten ein comdirect-Verrechnungskonto und ein Depotkonto in Wealthfolio.

**Nur das Depot wird gebucht, nicht das Girokonto.** Das comdirect-Verrechnungskonto in Wealthfolio enthält nur die Dividenden: Vor jedem Kauf bucht der Dienst eine Einzahlung in Höhe des Kaufbetrags, nach jedem Verkauf eine Auszahlung des Erlöses. Überweisungen, Kartenzahlungen, Gebühren und Zinsen des Girokontos übernimmt er nicht, und den Kontostand gleicht er nicht ab.

**Wertpapiere, die du schon vor dem Startdatum hattest,** zeigt die Seite *Prüfung* unter „Bestand ohne Kauf in Wealthfolio“. Kaufdatum, Kurs pro Stück (vorbelegt mit dem Einstandskurs von comdirect) und Gebühren eintragen – der nächste Abruf bucht den Kauf samt Einzahlung.

**Anmelden:** photoTAN-Push in der App bestätigen und *Ich habe in der App bestätigt* klicken (bei photoTAN-Grafik oder mobileTAN die TAN eintippen). comdirect hält eine Sitzung nur kurz (Zugriffsschlüssel 10 Minuten, verlängerbar, solange der Dienst sie nutzt). Der tägliche Abruf braucht deshalb meist eine **neue Bestätigung**: Er schickt dir eine ntfy-Nachricht und wartet drei Minuten auf die Freigabe in der App. Mit TAN zum Eintippen geht das nur in der Weboberfläche.

> **Sperrschutz:** comdirect sperrt das Online-Banking nach **fünf TAN-Anforderungen ohne richtige TAN** und nach **drei falschen TANs**. Der Dienst fordert nach drei unbeantworteten Anfragen oder zwei falschen TANs keine weitere an. Dann einmal auf comdirect.de mit TAN anmelden und die Zugangsdaten im Dienst neu speichern.

**Was gebucht wird:**

| Bei comdirect | In Wealthfolio |
|---|---|
| Kauf / Verkauf (Depotumsatz) | Kauf/Verkauf auf dem Depotkonto mit Stückzahl und Kurswert; als Betrag die zugehörige Wertpapier-Buchung auf dem Girokonto, die Differenz als Gebühr. Die API trennt bei Verkäufen Gebühr und Steuer nicht – beides steht dann in der Gebühr. Davor eine Einzahlung (Kauf) bzw. danach eine Auszahlung (Verkauf) auf dem Verrechnungskonto, dazwischen der Übertrag |
| Dividende / Ertrag (Zinsen / Dividenden, Wertpapier per ISIN oder WKN im Buchungstext erkennbar) | Dividende mit Nettobetrag auf dem Depotkonto, Geld per Übertrag aufs Verrechnungskonto |
| Dividende, deren Wertpapier nicht erkennbar ist | **nicht gebucht**, als unbekannt gemeldet |
| alle anderen Girobuchungen (Überweisung, Karte, Gebühren, Zinsen, …) | **nicht gebucht** |
| Depotübertrag, Kauf/Verkauf ohne Kontobuchung nach 10 Tagen | **nicht gebucht**, als unbekannt gemeldet – den Bestand legst du unter *Prüfung* an |

Vorgemerkte Umsätze und Orders übernimmt der Dienst erst, wenn sie gebucht sind. Ohne Startdatum fragt der Dienst zehn Jahre zurück (ohne Datum liefert comdirect nur ein halbes Jahr) und übernimmt, was comdirect davon herausgibt; wie weit das zurückreicht, steht im Log (`comdirect: … giro bookings (oldest …), … depot transactions (oldest …)`).

## Abgleich mit Wealthfolio

Nach jedem Abruf vergleicht der Dienst, was der Broker meldet, mit dem Stand in Wealthfolio: das Guthaben mit dem Cash des Verrechnungskontos, ob das Depotkonto kein Bargeld hält (es muss 0 sein) und keine Position „$CASH“ in einem der Konten steht, und, bei Trade Republic und Scalable Capital, jede Position mit dem Bestand des Depotkontos. Die Übersicht zeigt das Ergebnis. Eine Abweichung, die auch beim nächsten Abruf noch besteht, kommt als ntfy-Nachricht. Direkt nach neuen Buchungen rechnet Wealthfolio noch. Typische Ursachen: eine Kapitalmaßnahme, die per CSV-Import nachzuholen ist, oder Buchungen aus der Zeit vor dem ersten Abruf.

## Im Alltag

| Was passiert | Was du tust |
|---|---|
| Push-Nachricht „Anmeldung nötig“ | Auf die Nachricht tippen, TAN eingeben – fertig |
| Push-Nachricht „Abruf fehlgeschlagen“ | Übersicht öffnen, dort steht der Grund. Meist löst es sich beim nächsten Lauf von selbst |
| „unbekannte Buchungen“ | Der Dienst kennt eine Buchungsart noch nicht und hat sie **nicht** übernommen. Unter *Unbekannte Buchungen* steht, was es war – bei Bedarf von Hand in Wealthfolio eintragen und gern ein Issue mit dem Typ anlegen |
| Abgleich zeigt Bargeld im Depotkonto oder zu viel/zu wenig auf dem Verrechnungskonto | Seite *Prüfung* öffnen: Sie listet die Zeitpunkte, an denen im Depotkonto Bargeld stehen bleibt (z. B. ein Kauf ohne Übertrag oder ein Übertrag, dessen Verkauf gelöscht wurde), und Überträge ohne Gegenstück – mit Datum, Betrag, Kommentar und ob die Buchung vom Dienst oder aus einem Import stammt. Korrigiert wird in Wealthfolio; die Seite ändert nichts |
| Ein Broker soll noch einmal von vorn gebucht werden (z. B. nach geänderten Regeln) | In den Broker-Einstellungen *Neu aufsetzen*: löscht alle Buchungen des Dienstes für diesen Broker, der nächste Abruf holt alles ab dem Startdatum neu. Importe und Handeinträge bleiben |
| Push-Nachricht „Abweichung zu Wealthfolio“ | Eine schon übernommene Buchung fehlt in Wealthfolio (gelöscht?), oder eine Buchung des Dienstes gibt es beim Broker nicht mehr (z. B. storniert). Seite *Prüfung* öffnen: Fehlende mit *Wieder anlegen* zurückholen oder *Ignorieren*, wenn sie zu Recht fehlen; Stornierte in Wealthfolio löschen |
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

Für Mitwirkende: [ARCHITECTURE.md](ARCHITECTURE.md) erklärt Aufbau und Ablauf, [CLAUDE.md](CLAUDE.md) die Regeln und wie ein neuer Broker dazukommt.

- Python-Dienst `brokersync` (FastAPI-Oberfläche auf Port 8090, `brokersync run` für den systemd-Timer) unter `/opt/wealthfolio-broker-sync`; Daten in `/opt/wealthfolio-broker-sync/data` (Konfiguration, verschlüsselte Zugangsdaten, Sync-Status), läuft als eigener Benutzer `brokersync`.
- Wealthfolio wird über seine REST-API (`/api/v1`) mit dem Wealthfolio-Passwort angesprochen.
- Scalables CLI `sc` liegt unter `/opt/wealthfolio-broker-sync/bin/sc`; `brokersync install-sc` installiert bzw. aktualisiert es (Installation und Update rufen es auf).
- Releases: Ein neuer Stand wird installierbar, sobald die Version in `pyproject.toml` erhöht und nach `main` gemergt ist; der Release-Workflow legt dann das GitHub-Release an, aus dem Installation und Update laden.

## Danke

- [pytr](https://github.com/pytr-org/pytr) (MIT) für die Anbindung an Trade Republic; die Testfälle für Trade Republic folgen dem Format seiner Test-Ereignisse.
- [python-fints](https://github.com/raphaelm/python-fints) (LGPL) für FinTS.
- [Scalable CLI](https://github.com/ScalableCapital/scalable-cli) (Apache-2.0) von Scalable Capital für Scalable; die Zuordnung der Buchungsarten folgt dem [Scalable Capital Transactions Exporter](https://github.com/matthesvoss/Scalable-Capital-Transactions-Exporter) (MIT), dessen CSV das Addon importiert.

## Lizenz

MIT
