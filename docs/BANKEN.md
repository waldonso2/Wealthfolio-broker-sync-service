# Broker und Banken im Einzelnen

Was der Dienst je Broker oder Bank abruft, wie er es bucht und was du dafür einrichtest. Zurück zur [README](../README.md).

## DKB

Der Dienst liest dein **DKB-Girokonto** per FinTS (HBCI), dieselbe Schnittstelle, die Finanzprogramme wie Hibiscus oder MoneyMoney nutzen. Er liest nur; Überweisungen kann er nicht auslösen.

**Einrichten** unter *Broker → DKB*:

- **Anmeldename und PIN:** dieselben wie im DKB-Banking.
- **IBAN des Girokontos:** nur nötig, wenn du bei der DKB mehrere Konten hast.
- **Konten:** ein DKB-Verrechnungskonto und ein DKB-Depotkonto in Wealthfolio, wahlweise dazu ein **Tagesgeld-Konto** (siehe unten).

**Freigabe in der DKB-App:** Bei der ersten Anmeldung und danach in Abständen (nach den PSD2-Regeln meist alle 90 Tage) will die DKB eine Bestätigung in der DKB-App. In der Oberfläche bestätigst du in der App und klickst dann *Ich habe in der App bestätigt*. Läuft gerade der tägliche Abruf, schickt der Dienst eine ntfy-Nachricht und wartet drei Minuten auf deine Freigabe.

**Was gebucht wird:**

| Auf dem Girokonto | In Wealthfolio (DKB-Verrechnungskonto) |
|---|---|
| Gutschrift, Gehalt, eingehende Überweisung | Einzahlung (DEPOSIT) – eingehendes Geld ist immer eine Einzahlung |
| Kartenzahlung, Lastschrift, ausgehende Überweisung | Auszahlung (WITHDRAWAL), also eine Ausgabe |
| Ausgehende Überweisung auf ein eigenes Konto | Übertrag (TRANSFER_OUT, mit Gegenbuchung auf dem gewählten Konto), wenn ein Übertragsmuster passt (siehe *Im Alltag*) |
| Habenzinsen / Kontoführungsentgelt beim Rechnungsabschluss | Zinsen (INTEREST) / Gebühr (FEE) |
| Wertpapierabrechnung, Ertragsgutschrift, Dividende | **nichts** – siehe unten |

**Wertpapiere:** Das DKB-Depot liefert FinTS nicht als Umsätze. Käufe, Verkäufe und Ausschüttungen kommen deshalb über einen Import der Wertpapierabrechnungen (PDF) in Wealthfolio, der nach demselben Zwei-Konten-Modell bucht (Übertrag vom bzw. zum Verrechnungskonto). Damit nichts doppelt zählt, bucht der Dienst die zugehörigen Girokonto-Umsätze nicht noch einmal. Er prüft nur, ob der Übertrag dazu schon da ist (gleicher Betrag, höchstens 6 Tage auseinander). Fehlt er, bekommst du eine Nachricht; sobald die Abrechnung importiert ist, erkennt der nächste Abruf sie. Den **Depotbestand** liefert die DKB per FinTS: Nach jedem Abruf vergleicht der Dienst ihn mit dem DKB-Depotkonto in Wealthfolio. Fehlt dort eine Position, legst du sie auf der Seite *Prüfung* als Anfangsbestand an.

**Tagesgeld:** Wählst du in den Broker-Einstellungen ein eigenes Wealthfolio-Konto für das Tagesgeld, liest der Dienst auch dessen Umsätze und Saldo. Zinsen, Ein- und Auszahlungen landen auf diesem Konto. Umbuchungen zwischen Girokonto und Tagesgeld bucht er einmal als Übertrag zwischen den beiden Konten, nicht als Aus- und Einzahlung. Das Tagesgeld findet er selbst; klappt das nicht, trägst du seine IBAN bei den Zugangsdaten ein. Beim ersten Abruf holt er das Tagesgeld ab dem Startdatum.

**Zeitraum:** Ohne Datum unter *Buchungen übernehmen ab* holt der erste Abruf die letzten 89 Tage. Für ältere Umsätze verlangt die DKB eine Freigabe in der App.

**Ältere Wertpapierabrechnungen:** Importierst du Abrechnungen von vor dem ersten gelesenen Girokonto-Tag, hat der Dienst das Geld dafür nie gesehen. Er bucht deshalb vor jedem solchen Kauf eine Einzahlung in gleicher Höhe und nach jedem solchen Verkauf eine Auszahlung; Dividenden bleiben auf dem Verrechnungskonto. Käufe und Verkäufe ab dem ersten gelesenen Tag sind über ihre Girobuchung abgedeckt.

**Saldo:** Die Seite *Broker* zeigt nach jedem Abruf den Saldo laut DKB (und den des Tagesgelds). Er sollte mit dem DKB-Verrechnungskonto (bzw. dem Tagesgeld-Konto) in Wealthfolio übereinstimmen.

**Sicherheit:** Lehnt die DKB Anmeldename oder PIN ab, versucht der Dienst es nicht noch einmal, bis du die Zugangsdaten neu speicherst. Nach drei Fehlversuchen sperrt die DKB sonst das Online-Banking.

## Deutsche Bank

Der Dienst liest dein **Deutsche-Bank-Girokonto** und den **Bestand deines maxblue-Depots** per FinTS (HBCI) – nur lesend. Eine eigene Freischaltung im Online-Banking braucht FinTS bei der Deutschen Bank nicht mehr.

**Einrichten** unter *Broker → Deutsche Bank*:

- **Anmeldename:** deine Deutsche-Bank-ID oder Filialnummer und Kontonummer (zehnstellig, ggf. mit führenden Nullen), wie im Online-Banking. Dazu die **PIN**.
- **Bankleitzahl:** die deines Kontos (sie hängt bei der Deutschen Bank von der Filiale ab, steht in deiner IBAN an Stelle 5 bis 12).
- **IBAN des Girokontos:** nur nötig, wenn du mehrere Girokonten hast. Ohne IBAN nimmt der Dienst das Girokonto (nicht Spar- oder Depotkonto); welches, steht im Log.
- **Konten:** ein Deutsche-Bank-Verrechnungskonto und ein Depotkonto (maxblue) in Wealthfolio.

**Freigabe mit BestSign:** Bei der ersten Anmeldung und danach in Abständen will die Deutsche Bank eine Bestätigung in der Deutsche Bank App (BestSign). In der Oberfläche bestätigst du und klickst *Ich habe in der App bestätigt*; beim täglichen Abruf schickt der Dienst eine ntfy-Nachricht und wartet drei Minuten. Mit photoTAN geht die Anmeldung nur in der Weboberfläche.

**Was gebucht wird:** Die Girokonto-Umsätze wie bei der DKB (Tabelle oben): Einzahlungen, Auszahlungen, Überträge, Zinsen und Entgelte. Liefert die Bank sie nicht im MT940-Format, holt der Dienst sie im camt-Format (das Log sagt, wie viele auf welchem Weg kamen). Girokonto-Buchungen zu Wertpapiergeschäften (Kauf, Verkauf, Ertragsgutschrift) bucht er nicht selbst, sondern prüft sie gegen die Depotseite.

**Depot:** Den Bestand des Depots holt der Dienst per FinTS und vergleicht ihn nach jedem Abruf mit dem Depotkonto in Wealthfolio. Depotumsätze liefert die Deutsche Bank per FinTS nicht. Die kommen aus dem **CSV-Export**: im Online-Banking unter Depot → Umsätze als CSV exportieren und unter *Broker → Deutsche Bank* hochladen. Das geht beliebig oft, auch mit überlappenden Zeiträumen, denn doppelt gebucht wird nichts. Konto- und Depotnummern aus der Datei speichert der Dienst nicht.

| Im Export | In Wealthfolio |
|---|---|
| Dividende/Ausschüttung | Dividende mit Stückzahl, Nettobetrag und Steuer (Kapitalertragsteuer, Quellensteuer); das Geld per Übertrag aufs Verrechnungskonto, wo die Gutschrift des Girokontos dagegen geprüft wird |
| Kauf / Verkauf | Kauf/Verkauf mit Stückzahl, Spesen und Steuer; das Geld per Übertrag vom bzw. zum Verrechnungskonto |
| Eingang mit Betrag (z. B. Mitarbeiteraktien) | Kauf; das Geld kam nicht übers Girokonto, daher davor eine Einzahlung in gleicher Höhe |
| Käufe/Verkäufe vor dem ersten gelesenen Girokonto-Tag | wie oben, aber mit Einzahlung vor dem Kauf bzw. Auszahlung nach dem Verkauf – die Girobuchung dazu wurde nie gelesen. Dividenden bleiben immer auf dem Verrechnungskonto |
| Kapitaltransaktion, Ausgang, Eingang ohne Betrag | **nicht gebucht**, unter *Unbekannte Buchungen* – Kapitalmaßnahmen (z. B. eine Abspaltung) und Depotüberträge legst du auf der Seite *Prüfung* als Anfangsbestand an |

**Zeitraum und Sicherheit** wie bei der DKB: ohne Startdatum die letzten 89 Tage; nach einer abgelehnten PIN kein weiterer Versuch, bis du die Zugangsdaten neu speicherst.

## Andere Banken (FinTS)

Die meisten deutschen Banken – Sparkassen, Volks- und Raiffeisenbanken, Sparda- und PSD-Banken und viele Direktbanken – bieten FinTS mit PIN/TAN an. Jede davon lässt sich unter *Broker → Weitere Bank 1 (FinTS)* einrichten, bis zu drei gleichzeitig (*Weitere Bank 2*, *3*).

- **Anmeldename und PIN:** wie im Online-Banking deiner Bank.
- **Bankleitzahl:** die deines Kontos (8 Ziffern).
- **FinTS-Adresse:** leer lassen – der Dienst kennt die Adresse aus der Liste FinTS-fähiger Banken der Deutschen Kreditwirtschaft. Nur wenn deine Bank dort fehlt, trägst du sie ein (beginnt mit `https://`, steht auf der Website deiner Bank unter *FinTS* oder *HBCI*). Der Dienst schickt deine Zugangsdaten nur an diese Adresse.
- **IBAN, Konten:** wie bei der DKB.

Gebucht wird wie bei der DKB: Ausgaben als Auszahlung, Eingänge als Einzahlung, Zinsen und Gebühren für sich. Girobuchungen von Wertpapiergeschäften (Buchungstext mit *Wertpapier*, *Depot*, *Dividende* …) wartet der Dienst ab, bis die Wertpapierseite – etwa ein Import der Abrechnungen – in Wealthfolio steht. Meldet die Bank ein Depot, liest er dessen Bestand und vergleicht ihn; als vollständig gilt er erst, wenn die Bank bestätigt ist. Weichen die Buchungstexte deiner Bank ab oder klappt etwas nicht, melde es bitte als Issue – dann bekommt sie ein eigenes Profil.

## Banken über PSD2 (z. B. N26)

Jede Bank in der EU muss Kontodaten über eine Schnittstelle nach der Zahlungsdiensterichtlinie (PSD2) herausgeben – auch Banken ohne FinTS wie N26. Lesen dürfen dort nur zugelassene Kontoinformationsdienste. Der Dienst nutzt dafür **Enable Banking**: **Deine Kontodaten laufen dabei über Enable Banking** (ein zugelassener Anbieter aus Finnland), nicht direkt von der Bank zum Dienst. Wer das nicht möchte, bleibt bei FinTS oder dem Import.

**Eigene App bei Enable Banking (einmalig):**

1. Im Kontrollzentrum von Enable Banking ein Konto anlegen und eine App im Produktivbetrieb registrieren. Ohne Vertrag läuft sie im eingeschränkten Modus: kostenlos, aber nur für deine eigenen, verknüpften Konten.
2. Als **Redirect-URL** die Adresse dieses Dienstes mit einem beliebigen Pfad eintragen, z. B. `https://<Adresse des Dienstes>:8443/psd2`.
3. Den beim Anlegen erzeugten **privaten Schlüssel** (.pem-Datei) aufheben.
4. Dein Konto bei der Bank mit der App verknüpfen, wie es das Kontrollzentrum beschreibt.

**Einrichten** unter *Broker → Bank über PSD2 1* (bis zu drei Banken: *2*, *3*):

- **Bank:** der Name, wie Enable Banking ihn führt, z. B. `N26`. Bei einem Tippfehler nennt der Dienst ähnliche Namen.
- **Land:** leer für Deutschland, sonst der Ländercode.
- **Application-ID** und **privater Schlüssel:** aus Schritt 1 und 3; der Schlüssel liegt nur verschlüsselt im Tresor.
- **IBAN:** nur nötig, wenn die Bank mehrere Konten freigibt; sonst nimmt der Dienst das Girokonto.

**Anmelden:** Der Dienst zeigt einen Link zu deiner Bank. Dort meldest du dich an und gibst den Zugriff frei. Danach landest du auf der Redirect-URL – die Seite muss nicht laden. Kopiere ihre Adresse aus der Adresszeile in das Feld des Dienstes. Die Freigabe gilt so lange, wie die Bank es erlaubt (meist 90 oder 180 Tage); 7 Tage und 1 Tag vor dem Ende erinnert ntfy daran, dich neu anzumelden.

**Was gebucht wird:** wie bei der DKB – Ausgaben als Auszahlung, Eingänge als Einzahlung, Zinsen und Gebühren für sich; vorgemerkte Umsätze erst, wenn sie gebucht sind. PSD2 deckt nur Zahlungskonten ab, **kein Depot**: Girobuchungen von Wertpapiergeschäften wartet der Dienst ab, bis die Wertpapierseite in Wealthfolio steht.

## N26

> **Bitte beachten:** N26 bietet Privatkunden keine offizielle Schnittstelle. Der Dienst nutzt die der N26-App, wie andere Finanzprogramme auch. N26 kann sie jederzeit ändern oder solche Zugriffe untersagen; dann klappt der Abruf nicht mehr. Deshalb bestätigst du in den Broker-Einstellungen einmal, dass du diesen Hinweis gelesen hast; vorher startet keine Anmeldung.

**Einrichten** unter *Broker → N26*: E-Mail und Passwort deines N26-Kontos.

**Anmelden:** Der Dienst meldet sich an, N26 schickt eine Bestätigung in die N26-App. Du bestätigst dort und klickst *Ich habe in der App bestätigt*. Danach ruft der Dienst ohne dich ab, solange N26 die Anmeldung verlängert. Läuft sie ab, meldet ntfy *Anmeldung nötig* – eine neue Anfrage in der App löst nur die Oberfläche aus, nie der tägliche Abruf. Lehnt N26 Passwort oder E-Mail ab, versucht der Dienst es nicht noch einmal, bis du die Zugangsdaten neu speicherst.

**Was gebucht wird:** Eingänge als Einzahlung, Kartenzahlungen, Lastschriften und Überweisungen als Auszahlung; vorgemerkte Umsätze erst, wenn sie gebucht sind. Geld, das du in einen Space schiebst, erscheint als Auszahlung vom Hauptkonto – mit einem Muster auf der Seite *Überträge* (z. B. Stichwort des Space) wird es ein Übertrag auf ein eigenes Wealthfolio-Konto. Der Saldo des Hauptkontos wird nach jedem Abruf verglichen.

## Trade Republic

Der Dienst liest deine Trade-Republic-Timeline über die inoffizielle Schnittstelle der App, mit dem Open-Source-Projekt [pytr](https://github.com/pytr-org/pytr). Er liest nur; Orders oder Auszahlungen kann er nicht auslösen.

> **Bitte beachten:** Trade Republic bietet keine offizielle Schnittstelle. Die genutzte kann sich jederzeit ändern, dann klappt der Abruf bis zu einem Update nicht. Ein automatisierter Zugriff ist von Trade Republic vermutlich nicht vorgesehen. Wer das nicht möchte, importiert die Buchungen weiter per CSV. Deshalb bestätigst du in den Broker-Einstellungen einmal, dass du diesen Hinweis gelesen hast; vorher startet keine Anmeldung.

**Einrichten** unter *Broker → Trade Republic*:

- **Telefonnummer** mit Ländervorwahl (z. B. `+4917612345678`) und die **PIN** der App.
- **Konten:** ein Trade-Republic-Verrechnungskonto und ein Trade-Republic-Depotkonto in Wealthfolio.

**Anmelden:** Die Anmeldung läuft wie im Browser auf app.traderepublic.com. Trade Republic schickt eine Anfrage in die App, die bestätigst du, und dann klickst du *Ich habe in der App bestätigt*. Die Handy-App bleibt dabei angemeldet. Nutzt dein Konto eine Authenticator-App, fragt die Seite nach deren Code. Die Sitzung hält eine Weile; läuft sie ab, schickt der tägliche Abruf eine ntfy-Nachricht und wartet zwei Minuten auf deine Bestätigung in der App.

**Was gebucht wird:**

| In der Timeline | In Wealthfolio |
|---|---|
| Kauf, Sparplan, Verkauf | Kauf/Verkauf auf dem Depotkonto mit Gebühr und Steuer in eigenen Feldern, Geld per Übertrag vom bzw. zum Verrechnungskonto |
| Dividende, Ausschüttung | eine Dividende mit Nettobetrag und Quellensteuer, Geld per Übertrag aufs Verrechnungskonto |
| Saveback | Bonus (CREDIT/BONUS) auf dem Depotkonto, der den Kauf bezahlt – ohne Abbuchung vom Verrechnungskonto |
| Zinsen | Zinsen (INTEREST) mit Steuer |
| Vorabpauschale / Steuerkorrektur | Steuer (TAX) / Steuererstattung (CREDIT/TAX_REFUND); eine Steuerkorrektur, die Geld abbucht, als Steuer (TAX) |
| Empfehlungsbonus | Bonus (CREDIT/BONUS) auf dem Verrechnungskonto |
| Wertlos ausgebucht | Verkauf zu 0 auf dem Depotkonto, ohne Geldbewegung |
| Einzahlung, Kartenerstattung | Einzahlung (DEPOSIT) |
| Kartenzahlung, Überweisung | Auszahlung (WITHDRAWAL) oder – mit passendem Übertragsmuster – Übertrag aufs eigene Konto |
| Aktiensplit, Spin-off, Tausch, Reinvestition der Dividende, Depotübertrag, Private Markets | **nicht gebucht**, als unbekannt gemeldet – bitte von Hand oder per CSV-Import buchen |

Reine Hinweise (Order angelegt/storniert, Dokumente, Adressänderung …) und stornierte Buchungen übernimmt der Dienst nicht.

**CSV-Import und Dienst zusammen:** Was du schon per CSV importiert hast, erkennt der Dienst und legt es nicht noch einmal an: gleiche Art, gleicher Betrag, höchstens 36 Stunden auseinander, bei Käufen und Verkäufen dieselbe Stückzahl. Das Wertpapier darf dabei unter einem anderen Symbol stehen (beim Import zugeordneter Ticker, im Dienst die ISIN).

## Scalable Capital

Der Dienst liest dein Scalable-Depot über **Scalables offizielles Kommandozeilenprogramm** [Scalable CLI](https://github.com/ScalableCapital/scalable-cli) (`sc`). Das ist eine von Scalable selbst angebotene Schnittstelle – kein Nachbau der Web-App. Installation und Update laden `sc` aus Scalables GitHub-Release und prüfen vorher Scalables Signatur und die Prüfsumme. Der Dienst nutzt nur lesende Befehle und meldet das CLI im Nur-Lese-Modus an (`--local-read-only`); Orders oder Sparpläne kann er nicht auslösen. `sc` läuft abgeschottet (Landlock): Es sieht weder den Schlüssel noch die Daten des Dienstes und bekommt von dessen Umgebung nur die Sprache.

> **Bitte beachten:** Das Scalable CLI ist laut Scalable noch in der Beta und ohne Support. Ändert Scalable es, klappt der Abruf bis zu einem Update des Dienstes nicht.

**Vorher einmal in Scalable:** Im **Webportal** unter *Profil → Sicherheit → Agentic Investing* die Scalable CLI aktivieren. Ohne das lehnt Scalable die Anmeldung ab, und der Dienst sagt dir das.

**Einrichten** unter *Broker → Scalable Capital*:

- **Depot-ID** nur, wenn du mehrere Scalable-Depots hast: die `portfolioId` aus der Adresse der Depot-Seite im Browser. Mit einem Depot leer lassen.
- **Konten:** ein Scalable-Verrechnungskonto und ein Scalable-Depotkonto in Wealthfolio.

**Anmelden:** Die Seite zeigt einen Link und einen Code. Link öffnen, bei Scalable anmelden, den Code bestätigen und dann *Ich habe bestätigt* klicken. Die Sitzung erneuert sich bei jedem Abruf selbst und liegt verschlüsselt im Tresor des Dienstes. Will Scalable doch eine neue Anmeldung, schickt der tägliche Abruf Link und Code per ntfy und wartet zehn Minuten auf deine Bestätigung.

**Was gebucht wird:**

| Bei Scalable | In Wealthfolio |
|---|---|
| Kauf, Sparplan, Verkauf | Kauf/Verkauf auf dem Depotkonto mit Gebühr (Order-, Handelsplatz- und Krypto-Spread-Gebühr) und Steuer in eigenen Feldern, Geld per Übertrag vom bzw. zum Verrechnungskonto |
| Ausschüttung | eine Dividende mit Nettobetrag und – wenn Scalable sie nennt – Steuer; Stückzahl 1, weil die Schnittstelle keine nennt. Geld per Übertrag aufs Verrechnungskonto |
| Zinsen | Zinsen (INTEREST) |
| Steuer (z. B. Vorabpauschale) / Steuererstattung | Steuer (TAX) / Steuererstattung (CREDIT/TAX_REFUND) |
| Gebühr (z. B. PRIME+) | Gebühr (FEE) |
| Einzahlung / Auszahlung | Einzahlung (DEPOSIT) / Auszahlung (WITHDRAWAL) oder – mit passendem Übertragsmuster – Übertrag aufs eigene Konto |
| Depotumzug (Wertpapiere aus- und wieder eingebucht, Bargeld mit `SWITCH-`) | **nicht gebucht** – heben sich auf |
| Fondstausch (Wertpapiere und Gutschrift `SWAP_OUT` am selben Tag) | Verkauf zum gutgeschriebenen Betrag |
| Rückzahlung/Knock-out eines Zertifikats (Ausbuchung mit Wert 0 plus Ausschüttung am selben Tag) | Verkauf zum ausgezahlten Betrag |
| Storno einer Ausschüttung | Storno und ursprüngliche Ausschüttung heben sich auf. Hatte der Dienst die Ausschüttung schon übernommen, zeigt die Seite *Prüfung* sie unter „nicht (mehr) bei Scalable“ – dann in Wealthfolio löschen |
| andere Stornos, einzelner Wertpapierübertrag, ELTIF, negative Zinsen, Gebührenerstattung | **nicht gebucht**, als unbekannt gemeldet – bitte von Hand prüfen bzw. eintragen |

Offene Orders übernimmt der Dienst erst, wenn sie ausgeführt sind. Der erste Abruf der ganzen Historie dauert einige Minuten: Scalable begrenzt, wie schnell die Details der Käufe und Verkäufe abgefragt werden dürfen. Wird es trotzdem zu viel, bucht der Dienst, was er hat, und holt den Rest beim nächsten Abruf. Kommt nichts an, steht im Log (`journalctl -u wealthfolio-broker-sync -n 50`), wie viele Transaktionen welcher Art Scalable geliefert hat – ohne Beträge oder Namen. Was du schon per CSV importiert hast, erkennt er wie bei Trade Republic. Scalables CSV-Export enthält bei Dividenden keine Steuer; erkennt der Dienst eine solche importierte Dividende, trägt er die Steuer aus Scalables Angaben nach (nur das Steuerfeld, Betrag und Kommentar bleiben).

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
