# Architektur

Wie der Wealthfolio Broker Sync aufgebaut ist und wie ein Abruf abläuft. Für die Bedienung siehe [README.md](README.md). Die Regeln, die bei Änderungen gelten (Invarianten, neuen Broker hinzufügen, Release), stehen in [CLAUDE.md](CLAUDE.md). Dieses Dokument erklärt das *Warum* dahinter und wiederholt sie nicht.

## Überblick

```mermaid
flowchart LR
  subgraph LXC["LXC-Container (Proxmox)"]
    UI["Weboberfläche<br/>brokersync serve :8443 (HTTPS)"]
    Timer["systemd-Timer<br/>brokersync run (täglich 06:00)"]
    Sync["Syncer<br/>sync.py"]
    subgraph data["/opt/wealthfolio-broker-sync/data"]
      Cfg["config.enc + notify.enc"]
      Vault["secrets.enc"]
      State["state.db (SQLCipher)"]
    end
    Key["/etc/wealthfolio-broker-sync<br/>key.cred (systemd-creds), tls/"] -. Credential beim Start .-> Sync
    UI --> Sync
    Timer --> Sync
    Sync --- Cfg & Vault & State
  end
  Sync -- "FinTS (read-only)" --> DKB[(DKB)]
  Sync -- "FinTS (read-only)" --> DB[(Deutsche Bank)]
  Sync -- "WebSocket via pytr (read-only)" --> TR[(Trade Republic)]
  Sync -- "Scalable CLI sc (read-only)" --> SC[(Scalable Capital)]
  Sync -- "REST-API (read-only)" --> CD[(comdirect)]
  Sync -- "REST /api/v1" --> WF[(Wealthfolio)]
  Sync -- "HTTP" --> Ntfy[(ntfy)]
  Import["CSV-/PDF-Import<br/>(von Hand, optional)"] -. bucht ebenfalls .-> WF
```

Es gibt zwei Prozesse mit demselben Code und demselben Datenverzeichnis:

| Prozess | systemd-Unit | Aufgabe |
|---|---|---|
| `brokersync serve` | `wealthfolio-broker-sync.service` | Weboberfläche: Einrichtung, Broker-Login mit TAN oder App-Bestätigung, Abruf per Knopf, Listen (unbekannte Buchungen, Duplikate, Wertpapiere) |
| `brokersync run` | `wealthfolio-broker-sync-run.service` + `.timer` | Ein Abruf aller aktiven Broker. Exit-Code 1, wenn ein Broker fehlschlug, damit systemd es anzeigt |

Beide laufen als Systembenutzer `brokersync` mit systemd-Härtung (`ProtectSystem=strict`, `PrivateDevices`, `CapabilityBoundingSet=` leer, `RestrictNamespaces`, `SystemCallFilter=@system-service`, `ProtectProc=invisible`, `UMask=0077` …; `systemd-analyze security` 1,5; der Abruf-Lauf endet nach 45 Minuten, #107) und dürfen nur in `data/` schreiben. Den Schlüssel bekommen sie von systemd als Credential (`$CREDENTIALS_DIRECTORY/brokersync-key`, Drop-in `10-key.conf` von `setup.sh`); `/run/wealthfolio-broker-sync` (RAM, `RuntimeDirectoryPreserve=yes`) teilen sie sich für den entsperrten Passphrasen-Schlüssel. Eine Lock-Datei (`data/sync.lock`, `flock`) sorgt dafür, dass immer nur ein Abruf läuft, auch wenn Timer und Weboberfläche gleichzeitig starten.

## Module

```mermaid
flowchart TB
  cli["cli.py"] --> sync
  web["web/<br/>app.py + routes/"] --> sync
  sync["sync.py<br/>Ablauf eines Abrufs"] --> adapters
  sync --> mapping["mapping.py<br/>Transaction → NewActivity"]
  sync --> dedup["dedup.py<br/>ExistingIndex"]
  sync --> wf["wealthfolio.py<br/>REST-Client"]
  sync --> reconcile["reconcile.py<br/>Bestandsabgleich"]
  sync --> assets["assets.py<br/>ISIN → Wealthfolio-Asset"]
  sync --> repair["repair.py<br/>$CASH-Asset entfernen (einmalig)"]
  sync --> retired["retired.py<br/>entfernte Broker aufräumen"]
  sync --> notify["notify.py<br/>ntfy"]
  sync --> logos["logos.py<br/>Logos von logo.dev (wahlweise)"]
  sync --> updates["updates.py<br/>neues Release? (einmal am Tag)"]
  sync --> lookthrough["lookthrough.py<br/>Positionslisten der ETFs, Taxonomien (wahlweise)"]
  lookthrough --> xray["xray.py<br/>Durchschau, Überschneidung, Klumpenrisiken"]
  web --> fire["fire.py<br/>FIRE mit deutschen Netto-Werten"]
  sync --> state["state.py"] & vault["vault.py"] & config["config.py"]
  web --> duplicates["duplicates.py<br/>Bereinigung"]
  subgraph adapters["adapters/"]
    base["base.py<br/>BrokerAdapter"] --- fints["fints.py<br/>FintsAdapter"] & tr["tr.py"] & scalable["scalable.py"] & comdirect["comdirect.py"]
    fints --- dkb["dkb.py<br/>Profil"] & deutschebank["deutschebank.py<br/>Profil"] & fintsbank["fintsbank.py<br/>jede andere Bank"]
  end
  adapters --> model["model.py<br/>Transaction, Kind, Position, CashBalance"]
  mapping --> model
```

Die Schichten hängen nur in eine Richtung voneinander ab:

1. **Adapter** sprechen mit dem Broker und liefern ausschließlich broker-neutrale Objekte aus `model.py`. Sie wissen nichts von Wealthfolio.
2. **Mapping** übersetzt eine `Transaction` in eine oder mehrere Wealthfolio-Aktivitäten (Buchungsmodell unten). Es kennt keinen Broker-Code, nur `Kind`.
3. **Sync** verbindet beides und entscheidet, was neu ist.

So kommt ein neuer Broker ohne Änderungen an Mapping und Sync aus.

## Ablauf eines Abrufs

`Syncer.run()` holt sich die Sperre, schließt Läufe, die ein abgestürzter Prozess offen gelassen hat (`aborted`), und arbeitet die Broker nacheinander ab. Jeder Broker läuft in `_run_broker` in einem eigenen `try`. Ein Fehler wird gemeldet und protokolliert, die übrigen Broker laufen weiter.

**Abrufmodus (#85):** Im Modus `safe` (`Config.sync_mode`, Seite *Sicherheit*) ruft `Syncer.run` nur mit `manual=True` ab, also nach einer Anmeldung in der Oberfläche; Timer und CLI erinnern nur per ntfy. Nach jedem Broker-Lauf (auch nach Fehlern) löscht `end_sessions` die Sitzung bis auf den Sperrschutz (`adapters.base.SAFETY_KEYS`). „Jetzt abrufen“ führt im sicheren Modus (und bei einer FinTS-Bank ohne gespeicherte PIN) über `/brokers/<key>/login?run=1`; „Alle abrufen“ legt die Reihenfolge in `session["run_queue"]` ab, führt durch die Anmeldungen (überspringbar) und startet dann einen Lauf für alle. Eine nicht gespeicherte PIN (`CredentialField.ask`) fragt die Anmeldung ab; sie liegt nur in `AppContext.pins` und geht als `pins` an den Lauf.

`_sync_broker` ist eine Folge benannter Schritte; was sie einander weitergeben, steht in `RunContext` (#75). Die Reihenfolge ist fachlich wichtig:

1. `_plan_window`: Adapter mit gespeicherter Session, Abrufzeitraum (ganze Historie, einmaliger Backfill, *Ab Startdatum neu abrufen*, Sparkonto ab Startdatum), schon Synchronisiertes.
2. `_fetch`: Login, Transaktionen, Anfangsbestände, hochgeladene Kontoauszüge, Kontostand, Positionen; die Session wird auch nach einem Fehler gespeichert.
3. `_prepare`: Neues bestimmen, Unbekanntes melden, Konten, Zuordnungen, Überträge-Muster, Sparkonto-Paare.
4. Mit Wealthfolio: `_repair_cash_assets` (vor dem Abgleich, damit er reparierte Aktivitäten sieht), `_add_before_history` (Importe vor der Bankhistorie), Aktivitäten laden, `_book` (Bankseite von Wertpapierbuchungen zuletzt), `_relink` (eine schon übernommene Buchung, deren Aktivitäten fehlen, die ein Import aber enthält, wird mit dessen Aktivität verknüpft statt als fehlend gemeldet), `_compare` (coverage.py), `_backfill_tax` (einmalig), `_learn_assets`, `_check_holdings` (nur nach einem Lauf ohne Fehler).
5. `_commit_flags`: offene Wertpapier-Gegenbuchungen melden, verschwundene Unbekannte auflösen, einmalige Flags nur nach einem Lauf ohne Fehler zurücksetzen.

```mermaid
sequenceDiagram
  participant S as Syncer
  participant A as Adapter
  participant B as Broker
  participant W as Wealthfolio
  participant N as ntfy
  S->>A: login() mit gespeicherter Session
  alt Session abgelaufen, Timer-Lauf
    A->>N: on_user_action("Bitte in der App bestätigen")
    A->>B: wartet auf die Bestätigung (DKB ≤ 3 min, TR ≈ 2 min)
  else Bestätigung nötig, Weboberfläche
    A-->>S: AuthRequired(Challenge) → Status needs_auth, ntfy mit Login-Link
  end
  S->>A: get_transactions(since), get_cash(), get_positions()
  A->>B: nur lesende Abfragen
  S->>S: Session verschlüsselt speichern (auch nach Fehlern)
  S->>S: schon synchronisierte Ids (state.synced) weglassen
  S->>S: Kind.UNKNOWN speichern und einmal melden
  S->>W: einmalig je Broker: $CASH-Asset alter Cash-Aktivitäten entfernen (repair.py)
  S->>W: Aktivitäten der Konten im Zeitraum laden (ExistingIndex)
  loop jede neue Transaktion, chronologisch
    alt SECURITIES_CASH
      S->>S: Gegenbuchung der Wertpapierseite suchen (±0,02, ≤ 6 Tage)
    else
      S->>S: mapping.to_activities()
      S->>S: ExistingIndex.find() – schon per CSV/PDF importiert?
      S->>W: POST /activities je Teil (Duplikat-Antwort = schon da)
    end
  end
  S->>S: früher Gebuchtes mit Wealthfolio vergleichen (coverage.py) → Seite Prüfung
  S->>W: Assets lernen, GET /holdings
  S->>S: Abweichungen vergleichen, nach 2 Läufen melden
  S->>N: Zusammenfassung von Fehlern, Unbekanntem, Abweichungen
```

### Abrufzeitraum (`since`)

| Situation | `since` |
|---|---|
| Adapter mit `full_history` (Scalable) | jeder Lauf ab `start_date` (sonst alles); Details nur für neue Transaktionen |
| Erster Lauf | `start_date` des Brokers, sonst alles, was der Broker liefert (bei der DKB 89 Tage, damit keine TAN nötig ist) |
| Danach | letzter erfolgreicher Lauf minus 7 Tage (`OVERLAP`), weil Broker spät buchen |
| Offene Wertpapier-Gegenbuchungen | Das Fenster bleibt bis einen Tag vor der ältesten offenen Buchung offen (`State.oldest_open`) |
| Broker mit Positionen, einmalig | die ganze Historie ab `start_date`, um für den Abgleich zu jeder ISIN das Wealthfolio-Asset zu lernen (`assets-learned:<broker>`) |
| *Ab Startdatum neu abrufen* (Broker-Einstellungen) | einmal wieder ab `start_date` (`refetch:<broker>`); das Flag fällt nach einem Lauf ohne Fehler weg |

Ein Lauf mit Fehlern zählt nicht als Erfolg. Der nächste Lauf fängt deshalb wieder vor ihm an.

## Doppelte Buchungen vermeiden

Dieselbe Buchung kann auf drei Wegen schon in Wealthfolio sein: Der Sync hat sie früher angelegt, ein Lauf brach mitten in einer mehrteiligen Buchung ab, oder ein CSV-/PDF-Import hat sie schon angelegt. Drei Schichten fangen das ab:

| Schicht | Wo | Erkennt |
|---|---|---|
| 1. Sync-Status | `state.synced` | Transaktionen, die vollständig angelegt oder als vorhanden erkannt wurden. Sie werden gar nicht erst verarbeitet |
| 2. Wealthfolio-Fingerprint | Wealthfolio (`409 Duplicate activity detected` → `Duplicate`) | Teile, die ein abgebrochener Lauf schon angelegt hat. Der Kommentar mit `[SYNC <broker>:<id>]` gehört zum Fingerprint und darf daher nie umformuliert werden |
| 3. Abgleich mit Importen | `dedup.ExistingIndex` | Aktivitäten aus CSV-/PDF-Importen: gleiches Konto und gleicher Typ, keine verschiedenen Währungen, ≤ 36 h Abstand, gleiche Stückzahl, Betrag ±0,02; von mehreren passenden die zeitlich nächste, dann die mit dem nächsten Betrag (#100). Das Symbol muss nicht übereinstimmen (Import: zugeordnetes Tickersymbol, Sync: ISIN). Dieselben Regeln nutzt das Import-Addon in die Gegenrichtung; `contract/dedup/cases.json` hält sie als gemeinsame Fälle für beide Repos (die CI vergleicht die Kopien) |

Geht der Sync-Status verloren (`state.db` gelöscht), findet Schicht 2 alles wieder. `test_lost_state_is_recovered_from_the_comments` prüft das.

`state.synced` entscheidet nur, was **neu** ist. Ob eine früher gebuchte Transaktion noch in Wealthfolio steht, prüft jeder Lauf danach gegen Wealthfolio (`coverage.py`): über die `[SYNC …]`-Referenz und die gespeicherten Aktivitäts-Ids. Fehlt sie ganz oder teilweise, steht sie auf der Seite *Prüfung*; der Nutzer wählt *Wieder anlegen* (der Eintrag in `synced` wird gelöscht, der nächste Lauf behandelt sie als neu, mit allen drei Schichten) oder *Ignorieren* (Status `ignored`). Von selbst legt der Sync nichts wieder an, weil eine Löschung in Wealthfolio Absicht sein kann. Bei `full_history` meldet er außerdem Aktivitäten mit `[SYNC …]`, deren Transaktion der Broker nicht mehr auflistet (z. B. storniert) - löschen muss sie der Nutzer.

Eine Transaktion wird erst als synchronisiert markiert, wenn **alle** ihre Aktivitäten existieren. Ein Kauf besteht zum Beispiel aus Übertrag raus, Übertrag rein und dem Kauf selbst. Bricht der Lauf nach dem ersten Teil ab, legt der nächste Lauf die fehlenden Teile an, und die vorhandenen kommen als `Duplicate` zurück.

Duplikate, die ältere Versionen trotzdem erzeugt haben, findet `duplicates.py` (Seite *Duplikate*). Gelöscht wird nur die Kopie des Syncs und nur nach Bestätigung.

## Buchungsmodell

Jeder Broker hat in Wealthfolio zwei Konten: ein **Cash-Konto** und ein **Depot** (FinTS-Banken wahlweise dazu ein **Tagesgeld-Konto**: `Accounts.savings`, Buchungen mit `Transaction.account = SAVINGS`, Umbuchungen zwischen Giro und Tagesgeld als ein Übertragspaar). `mapping.py` setzt das um:

| `Kind` | Aktivitäten |
|---|---|
| `BUY` | Cash `TRANSFER_OUT` → Depot `TRANSFER_IN` (gemeinsame `sourceGroupId`), Depot `BUY` |
| `SELL`, `DIVIDEND` | Depot `SELL`/`DIVIDEND`, dann zurück aufs Cash-Konto (Übertragspaar) |
| `BUY` mit `bonus_funded` (Saveback) | `BUY` ohne Übertrag vom Cash-Konto |
| `BUY`/`SELL` mit `external_cash` (comdirect, Anfangsbestand) | wie oben, dazu vorher ein `DEPOSIT` des Kaufbetrags bzw. danach ein `WITHDRAWAL` des Erlöses auf dem Cash-Konto |
| `DEPOSIT`, `WITHDRAWAL`, `INTEREST`, `FEE`, `TAX` | eine Aktivität auf dem Cash-Konto (ohne Asset, Menge und Preis 1) |
| `TAX_REFUND` | `CREDIT` mit Subtyp `TAX_REFUND` |
| `WITHDRAWAL` auf ein eigenes Konto (*Überträge*) | `TRANSFER_OUT` → `TRANSFER_IN` auf das Zielkonto |
| `SECURITIES_CASH` | nichts. Der Sync sucht nur den Übertrag, den die Wertpapierseite gebucht hat |
| `UNKNOWN` | nichts. Gespeichert, auf der Seite *Unbekannt* gelistet, einmal gemeldet; dort vervollständigt (#132) bucht der nächste Lauf es unter derselben Id als Kauf, Verkauf, Ein- oder Ausbuchung bzw. Split |
| `SPLIT` | eine SPLIT-Aktivität auf dem Depotkonto, `amount` = neue Stücke je alte (#133) |

**Cash-Aktivitäten tragen kein Asset**, auch die Überträge nicht. Mit Asset bucht Wealthfolio ein `TRANSFER_IN`/`TRANSFER_OUT` als Wertpapierübertrag dieses Assets, und es fließt kein Geld. Versionen vor 0.3.6 haben Cash-Aktivitäten mit dem Asset `$CASH-<Währung>` angelegt. `repair.py` ändert die eigenen davon einmal je Broker auf „kein Asset“ (`PUT /activities` mit `asset: {}`), statt sie zu löschen und neu anzulegen; Ids, Sync-Status und Übertragspaare bleiben so erhalten. Bis das geklappt hat, bucht der Sync für diesen Broker nichts. Ein erneut gesendeter Übertrag ohne Asset würde sonst nicht als dieselbe Aktivität erkannt und doppelt gebucht. Erledigt ist die Reparatur, wenn das Flag `cash-assets-repaired:<broker>` gesetzt ist.

Beträge sind durchgehend `Decimal`. Gebühr und Steuer stehen in eigenen Feldern, der Betrag eines Kaufs oder Verkaufs ist `trade_final_cash(...)`. Dieselben Regeln sollte ein CSV-/PDF-Import befolgen, damit der Abgleich (Schicht 3) seine Buchungen erkennt.

## Adapter

Alle Adapter erben von `BrokerAdapter` (`adapters/base.py`) und **lesen nur**: Sie haben keine Methode, die handelt, Geld bewegt oder Einstellungen ändert.

**Login in zwei Schritten.** `login()` versucht die gespeicherte Session. Braucht der Broker die Person (TAN, Code, App-Bestätigung), gibt es zwei Wege:
- In der Weboberfläche wirft der Adapter `AuthRequired(Challenge)`; die Oberfläche zeigt die Abfrage und ruft `complete_login(code)`.
- Bei Timer-Läufen ist `on_user_action` gesetzt; der Adapter schickt eine ntfy-Nachricht und wartet selbst auf die Bestätigung.

**Session.** `session_state()` wird nach jedem Lauf verschlüsselt gespeichert, auch nach Fehlern oder einem halben Login. Bei der DKB ist das der Zustand von python-fints, bei Trade Republic sind es die Cookies.

**Schutz vor Sperre.** Lehnt der Broker die PIN ab, setzt der Adapter `pin_rejected` und kontaktiert ihn nicht mehr, bis die Zugangsdaten neu gespeichert sind. Bei der DKB gilt das auch für eine vorübergehende Sperre. „Zu viele Versuche“ oder Netzfehler setzen das Flag nicht.

| Adapter | Protokoll | Besonderheiten |
|---|---|---|
| `dkb` | FinTS über python-fints (`FintsAdapter`) | Freigabe in der DKB-App (decoupled). Ids sind Hashes des Buchungsinhalts plus Zähler. Wertpapier-Gegenbuchungen auf dem Giro werden zu `SECURITIES_CASH`. Depotbestand per HKWPD (Depot ohne IBAN, nur in der UPD); `reports_positions`. Depotumsätze (HKWDU/MT536) bietet die DKB an, python-fints kann sie nicht |
| `deutschebank` | FinTS (`FintsAdapter`), Profil mit Bankleitzahl als Zugangsfeld; Depotumsätze aus dem hochgeladenen CSV-Export (`deutschebank_csv.py`, Zeilen ohne Konto-/Depotnummern in `state.statements`) | BestSign (decoupled). Girokonto ohne IBAN-Angabe nach Kontoart (1–9), Umsätze per MT940, sonst camt. CSV: Dividende, Kauf, Verkauf, Eingang (= Kauf mit `external_cash`), Käufe/Verkäufe vor dem ersten gelesenen Girotag (`meta history-start:<broker>`) mit `external_cash` (Dividenden nie: ihr Geld bleibt auf dem Verrechnungskonto), Rest `UNKNOWN`; Ids `dbcsv-<tag>-<hash>-<n>`. Bankseitige Gegenbuchungen werden im selben Lauf gegen die eben gebuchten Überträge geprüft (Wertpapier-Gegenbuchungen zuletzt). Depotbestand per HKWPD, auch für ein Depot ohne IBAN, das nur in den Benutzerparametern (UPD) steht; `reports_positions`. Beim Login loggt der Adapter, was die Bank anbietet (Umsätze, camt, Depotbestand, Depotumsätze, je Konto Produktname und Art, keine Nummern) |
| `tr` | WebSocket der Trade-Republic-App über pytr (fest gepinnt) | Web-Login mit Bestätigung in der App. Timeline (Transaktionen und Aktivitätslog, seitenweise bis `since`), Details in Batches zu 20. Geparst mit pytrs `Event.from_dict` |
| `comdirect` | Offizielle REST-API (`api.comdirect.de`, httpx) | Passwort-Grant → Session-Objekt → TAN-Challenge (`validate`) → Aktivierung (`PATCH`) → `cd_secondary`-Token. photoTAN-Push: Web-UI „bestätigt“, Timer-Lauf ntfy + Abfrage des Status-Links; TAN zum Eintippen nur in der Web-UI. Token (10 min) und Refresh-Token in der Session. Sperrschutz: `open_challenges` (vor jeder Challenge hochgezählt, < 5) und `wrong_tans`. Girobuchungen + Depotumsätze; Käufe/Verkäufe mit der Girobuchung (*Wertpapier*), die sie abgerechnet hat |
| `scalable` | Scalables offizielles CLI `sc` (Unterprozess, `--json`) | Gerätecode-Login mit `--local-read-only`: Link und Code im Browser bestätigen. Das Konfigurationsverzeichnis des CLI (Sitzung mit rotierendem Refresh-Token, DPoP-Schlüssel) liegt nur während des Laufs in einem temporären Verzeichnis, sein Inhalt verschlüsselt im Tresor (`session["files"]`). Transaktionen seitenweise, Details für Trades und Ausschüttungen – nur für noch nicht übernommene (`known_ids`), mit 1 s Abstand; bei `rate_limited` warten und wiederholen, hält das Limit an, kommen die restlichen Trades beim nächsten Lauf. `sc` installiert `brokersync install-sc` aus Scalables signiertem Release |

Einen Test-Broker liefert der Dienst nicht aus; die Tests nutzen `tests/fake_broker.py`. Den früheren Test-Broker „Dummy“ (bis 0.3.6) räumt `retired.py` beim Start auf: Einstellungen, Zugangsdaten, Sync-Status, Läufe, unbekannte Buchungen und Abgleich werden gelöscht. Seine Buchungen in Wealthfolio bleiben; die Übersicht zeigt einmal, wie viele es sind und wie man sie findet (`[SYNC dummy:`).

**FinTS-Banken** teilen sich `FintsAdapter` (`adapters/fints.py`): Login mit Freigabe in der App oder mit TAN-Eingabe, PIN-Schutz, Session, Abruf und die Einordnung der Giro-Buchungen. Eine Bank ist nur ein Profil (`dkb.py`, `deutschebank.py`): Bankleitzahl (fest oder als Zugangsfeld, wenn sie je Filiale verschieden ist), Server, Namen in den Meldungen und, falls die Buchungstexte abweichen, eigene Muster für Wertpapier, Zins und Gebühr. Die Ids hängen nur an der Buchung, nicht am Profil. Eine Bank ohne eigenes Profil nutzt `fintsbank.py` (#136): Bankleitzahl und FinTS-Adresse sind Zugangsfelder (die Adresse kommt sonst aus `institutes.lookup`: die Institutsliste der Deutschen Kreditwirtschaft, komprimiert und verschleiert in `fints_institutes.bin`), Standard-Muster; feste Plätze `fints1`…`fints3`, damit der Key einer eingerichteten Bank (Konfiguration, Tresor, Zustand, `[SYNC …]`) nie wechselt. Zugangsfelder mit fester Form (`blz`, `server` nur https, `iban`, `savings_iban`) prüft `routes/brokers.CREDENTIAL_CHECKS`. Den Depotbestand (HKWPD) liest `adapters/mt535.py` statt python-fints' eigenem Parser; gefragt werden nur Konten, für die die Bank einen Bestand anbietet, und das Log nennt je Depot die Zahl der Positionen bzw. bei leerer Antwort ihren Aufbau (nur Feldnamen).

Jeder Adapter hat `replay(recording)` für Contract-Tests (`tests/contract/<adapter>/`). Er antwortet dann aus einer Aufzeichnung statt vom Broker.

## Bestandsabgleich

Nach jedem Lauf ohne Fehler vergleicht `reconcile.py` den Kontostand und, bei Adaptern mit `reports_positions`, die Positionen des Brokers mit `GET /holdings` in Wealthfolio:

- **Zusätzliche Prüfungen:** Cash auf dem Depotkonto muss 0 sein, weil jede Zahlung dort vom Cash-Konto kommt oder dorthin zurückgeht. Eine `$CASH`-Position auf einem der beiden Konten weist auf einen alten Übertrag hin, der kein Geld bewegt hat.
- **Positionen ohne ISIN:** Wealthfolio-Positionen tragen keine ISIN. `assets.py` lernt deshalb aus den Aktivitäten von Käufen und Dividenden, unter welchem Asset eine ISIN gebucht ist. Eigene Zuordnungen auf der Seite *Wertpapiere* haben Vorrang.
- **Neuberechnung abwarten:** Wurde etwas angelegt oder repariert, wartet der Sync kurz (`BROKERSYNC_RECALC_WAIT`, Standard 5 s), weil Wealthfolio die Bestände im Hintergrund neu berechnet.
- **Gemeldet wird nur, was bleibt:** erst eine Abweichung, die zwei Läufe in Folge besteht, und dieselbe Menge von Abweichungen nur einmal.

## Daten und Geheimnisse

**Gleichzeitig: Web-Prozess und Timer (0.20.0).**
- **Datenbank:** Jeder Thread hat seine eigene Datenbankverbindung (`State.db` ist threadlokal).
- **Dateien:** Jede Datei wird über eine eigene Temp-Datei geschrieben (`crypto.write_atomic` mit `mkstemp`).
- **Sperren:** Einstellungen und Tresor schreiben unter `secrets.lock`, derselben Sperre wie die Neuverschlüsselung.
- **Broker-Sitzungen** gehen nur über `Vault.store_session` zurück in den Tresor. Hat seit dem Lesen jemand anderes geschrieben, gewinnen die höheren Sperrschutz-Zähler. Ist die Sitzung leer, weil neue Zugangsdaten gespeichert wurden, gewinnt der leere Stand.
- **Sitzungs-Cookies** tragen `session_tag` aus Passwort-Hash und Sitzungsversion (siehe *Weboberfläche*).

**Aufgeräumt (0.23.0).** `mapping.to_activities` bucht über `_Booking`, eine Methode je Art (Bargeld, eigener Übertrag, Auszahlung mit Mustern, Zinsen, Dividende, Kauf, Verkauf) - Kommentare, Reihenfolge und Ids wie zuvor. Importe vor dem Bankverlauf und Anfangsbestände stehen in `history.py`. `Position` trägt keinen Marktwert mehr (#118). Die Version steht nur in `pyproject.toml`; `brokersync.__version__` liest sie aus den Paket-Metadaten. Ein Adapter mit `consent` (Trade Republic, inoffizielle Schnittstelle) startet keine Anmeldung, bevor der Nutzer in den Broker-Einstellungen zugestimmt hat (`BrokerConfig.consented`, #112).

**Abschottung und Betrieb (0.22.0).**
- **`sc` in der Sandbox (#93):** `brokersync.sandbox` startet sich selbst (`python -I -m brokersync.sandbox --rw … -- sc …`), schränkt sich mit Landlock ein und führt dann `sc` aus. Es darf das System lesen (`/usr`, `/etc`, `/proc` …) und sein eigenes Verzeichnis, schreiben nur sein Temp-Verzeichnis; `data/`, `/run/credentials` und der Entsperr-Cache bleiben zu, das Netz offen. Die Umgebung enthält nur `PASS_ENV` und die eigenen Pfade. Die Units erlauben dafür `@sandbox`. Ohne Landlock im Kernel läuft `sc` ohne, *Sicherheit* sagt es.
- **Wealthfolio mit eigenem Zertifikat (#95):** `wealthfolio._Pinned` (ein `ssl.SSLContext`) prüft im Handshake den SHA-256 des Zertifikats gegen `Config.wealthfolio_fingerprint`, bevor ein Byte der Anfrage – das Passwort – rausgeht. Alle Clients entstehen über `wealthfolio.connect`. Die Seite zeigt bei einem Zertifikat ohne bekannten Herausgeber dessen Fingerabdruck (`server_fingerprint`) zum Vergleichen; gespeichert wird er erst mit *Speichern*. *Nur testen* speichert nichts.
- **Ereignisprotokoll (#102):** Tabelle `events` (Migration 2): Zeit, Ereignis, Adresse, Ergebnis, Broker, Detail (maskiert, nie ein Geheimnis). Jede Sicherheitsaktion der Oberfläche schreibt über `AppContext.event`, das Entsperren über das Gate; nach jedem Lauf fallen Einträge über 180 Tage weg. Eine Anmeldung von einer neuen Adresse meldet ntfy.
- **Sichern und Wiederherstellen (#109, `backup.py`):** `backup` schreibt `data/<Datei>` als tar.gz (die Datenbank per SQLite-Backup, also konsistent) unter Sync- und Tresor-Sperre. `restore` entpackt nur reguläre Dateien `data/<Name>` in ein Temp-Verzeichnis, prüft mit dem aktuellen Schlüssel und der Passphrase (`crypto.data_key_for`), dass sich alles öffnen lässt, sichert den alten Stand als `before-restore-*.tar.gz` und tauscht erst dann die Dateien. Die laufende Oberfläche hält `ui.lock`; solange lehnt `restore` ab.
- **Schutzstufe (#94):** `setup.sh` verschlüsselt den Schlüssel mit `systemd-creds --with-key=tpm2+host`, wenn ein TPM da ist, sonst `host`, und merkt sich das in `key-protection` → `BROKERSYNC_KEY_SOURCE` `systemd-creds-tpm2` oder `systemd-creds`. Ein root-only `secret.key` zieht es um, sobald `systemd-creds` verschlüsseln kann. Fußzeile und *Sicherheit* nennen die Datenbank nur mit SQLCipher verschlüsselt.

**Gleichzeitig, Teil 2 (0.21.0).**
- **Einstellungen** ändert die Oberfläche nur über `config.update` (`AppContext.update`): lesen, ändern, schreiben in einem Schritt unter `secrets.lock` (#117). Vorher ging ein Speichern in einem zweiten Tab verloren.
- **Sync-Datenbank:** Jeder Schreibzugriff läuft über `State._writing` unter `state.WRITES`. Ein Passphrasenwechsel hält diese Sperre, während er `state.db` kopiert; ein Schreiben wartet so lange. Wurde die Datei inzwischen ersetzt (anderer Inode), lehnt `_writing` mit `StateReplaced` ab, statt in die alte Datei zu schreiben (#120).
- **Tresor:** `Vault.load` entschlüsselt nur, wenn sich die Datei geändert hat (Inode, Zeit, Größe); sonst gibt es eine Kopie des letzten Stands zurück (#104).
- **Zeitpunkte** aus Wealthfolio und dem State liest nur `model.when`: immer UTC, ohne Zone als UTC, ein bloßer Tag als dessen Mitternacht (#119).

Alles liegt verschlüsselt in `BROKERSYNC_DATA` (Standard `/opt/wealthfolio-broker-sync/data`, Modus 0700, Dateien 0600); der Schlüssel liegt nicht dort (`crypto.py`).

**Schlüssel.** Der *Host-Schlüssel* (32 Zufallsbytes) kommt in dieser Reihenfolge aus `$CREDENTIALS_DIRECTORY/brokersync-key` (systemd: `LoadCredentialEncrypted=` aus `/etc/wealthfolio-broker-sync/key.cred`, mit `systemd-creds` verschlüsselt; wo das nicht geht `LoadCredential=` aus einer root-only Datei), `$BROKERSYNC_KEY_FILE` oder – nur Entwicklung, Tests und Installationen vor 0.9.0 – `data/secret.key`. Daraus leitet HKDF-SHA256 zwei Stufen ab:

| Stufe | Schlüssel | Schützt |
|---|---|---|
| Host | Host-Schlüssel | `notify.enc` – damit ein gesperrter Dienst per ntfy Bescheid sagen kann |
| Daten | Host-Schlüssel + argon2id(Master-Passphrase), falls gesetzt | `secrets.enc`, `config.enc`, `state.db` |

Mit Passphrase hält `keyring.json` Salz, argon2id-Parameter und einen Prüfwert (nie die Passphrase). Entsperren legt das argon2id-Ergebnis in `/run/wealthfolio-broker-sync/unlock.key` (RAM, weg nach Neustart). Ist der Dienst gesperrt, zeigt die Weboberfläche nur `/unlock` (`web/gate.py`), und `brokersync run` meldet sich per ntfy und endet mit 1. Setzen, Ändern und Entfernen (`security.change_passphrase`) verschlüsseln alles der Datenstufe neu, unter dem Sync-Lock.

**Neu verschlüsseln ohne Datenverlust (#91).** Passphrasenwechsel und Tausch des Host-Schlüssels gehen in drei Schritten (`security.py`):

1. **Bereitlegen:** Jede Datei der Datenstufe wird neu verschlüsselt als `<name>.rekey` neben die alte geschrieben (`_stage_data_tier`). `state.db` wird kopiert und erst die Kopie per `PRAGMA rekey` umgeschlüsselt, nie die Datenbank selbst. Die alten Dateien bleiben unberührt.
2. **Umschalten:** Das Journal `rekey.json` legt fest, was gilt. Beim Passphrasenwechsel ist das Schreiben des Journals der Umschaltpunkt. Beim neuen Host-Schlüssel schaltet erst `setup.sh` um, wenn es den Schlüssel ablegt: `rotate-key` legt nur bereit und schreibt ins Journal einen Prüfwert, der mit dem neuen Host-Schlüssel versiegelt ist.
3. **Tauschen:** `os.replace` je Datei, danach `fsync` des Verzeichnisses, zuletzt wird das Journal gelöscht.

`security.recover` läuft vor allem anderen (`brokersync serve`, `run`, `migrate`, `recover`) und braucht nur den Host-Schlüssel, geht also auch im gesperrten Zustand:

- Bereitgelegte Dateien ohne Journal stammen von einem Versuch, der nie umgeschaltet hat: Sie werden gelöscht.
- Ein Journal vom Passphrasenwechsel wird zu Ende geführt.
- Ein Journal vom Host-Schlüssel wird zu Ende geführt, wenn der Prüfwert mit dem Schlüssel in Gebrauch aufgeht; sonst wird es verworfen.

Ein Stromausfall an beliebiger Stelle lässt die Daten so vollständig auf dem alten oder dem neuen Stand, nie gemischt. `test_security` bricht an jedem der Schritte ab.

| Datei | Inhalt | Format |
|---|---|---|
| `secrets.enc` | Wealthfolio-Passwort, logo.dev-Schlüssel, Zugangsdaten und Sessions der Broker (bei FinTS der Zustand von python-fints: System-ID, Bank- und Nutzerparameter samt Kontonummern, TAN-Verfahren – nie die PIN, `test_security` prüft die Schlüssel des Blobs), argon2id-Hash des UI-Passworts, Signierschlüssel der UI-Session | AES-256-GCM, `vault.py`, atomar und mit Lock |
| `config.enc` | Wealthfolio-URL, je Broker Konten, `enabled`, `start_date`, Wertpapier-Zuordnungen, Übertrags-Muster, Logos an/aus | AES-256-GCM, `config.py` |
| `notify.enc` | ntfy-Server, -Topic, -Token, Details an/aus, öffentliche URL | AES-256-GCM, Host-Stufe |
| `state.db` | siehe unten | SQLCipher (AES-256); nur ohne SQLCipher-Wheel (weder x86-64 noch ARM64) unverschlüsselt; eine unverschlüsselte Datei verschlüsselt der nächste Start mit SQLCipher (`encrypt_plain`), die Seite *Sicherheit* sagt es |
| `keyring.json` | Parameter der Master-Passphrase | Klartext, nichts Geheimes |
| `*.lock` | Sperrdateien | — |

Jede verschlüsselte Datei ist `BSE1 | Nonce | Chiffrat`; ihr Zweck (`secrets`, `config`, …) ist als zusätzliche Daten authentifiziert, eine Datei lässt sich also nicht gegen eine andere tauschen. `brokersync migrate` (von `setup.sh` aufgerufen) bringt Daten von vor 0.9.0 – Fernet-`secrets.enc`, `config.json`, unverschlüsselte `state.db` – auf diesen Stand; danach löscht `setup.sh` `data/secret.key` und ersetzt Sicherungen, die ihn noch enthielten.

**Tabellen in `state.db`** (Schema-Version in `PRAGMA user_version`: `SCHEMA` ist der Stand von 0.13.x, jede spätere Änderung ein Schritt in `state.MIGRATIONS`, der beim Öffnen in einer Transaktion läuft; eine Datenbank einer neueren Version wird nicht geöffnet):

| Tabelle | Inhalt |
|---|---|
| `synced` | `(broker, tx_id)`, Status `imported`/`existing`/`ignored` und die angelegten Aktivitäts-Ids |
| `gaps` | Ergebnis des letzten Vergleichs mit Wealthfolio: `missing`, `partial`, `ignored`, `orphan` |
| `openings` | Anfangsbestände, die der Nutzer auf *Prüfung* einträgt (ISIN, Kaufdatum, Stück, Kurs, Gebühr); jeder Lauf bucht sie als Kauf `start-<isin>-<tag>` mit Einzahlung |
| `runs` | Verlauf der Abrufe: `running`, `ok`, `needs_auth`, `error`, `aborted` und die Zähler |
| `unknown_events` | unbekannte Buchungen und offene Wertpapier-Gegenbuchungen, mit einer Nutzlast ohne persönliche Daten |
| `completions` | was der Nutzer aus einer unbekannten Buchung gemacht hat (Art, ISIN, Stück, Betrag, Verhältnis; Migration 3) |
| `balances`, `reconcile` | letzter Kontostand des Brokers (je Währung und Konto: leer = Verrechnungskonto, `savings` = Sparkonto) und Abweichungen, samt dem, was schon gemeldet wurde |
| `assets` | ISIN → Wealthfolio-Asset je Broker |
| `meta` | Flags und Werte; jeder Schlüssel steht mit Art (`flag`/`text`/`json`) und Broker-Bezug in `meta_keys.py`, `forget_broker`/`reset_broker` löschen genau die broker-bezogenen |

Geheimnisse stehen nur im Vault. In Logs, Fehlermeldungen auf dem Bildschirm und ntfy-Nachrichten schwärzt `redact.py` alles, was der Vault an Zugangsdaten und Tokens hält, sowie IBANs und lange Nummern; ntfy-Nachrichten tragen ohne *Details* nur Titel und Link.

## Weboberfläche

FastAPI mit Jinja2-Vorlagen (`web/templates/`), Texte auf Deutsch oder Englisch, eigener Login mit dem UI-Passwort (argon2id).

**Login-Bremse (`web/gate.Throttle.attempt`, #90).** Durch sie laufen UI-Passwort, Master-Passphrase und Einrichtungscode:

- Sie zählt jeden Versuch **vor** der argon2id-Prüfung (64 MiB) und nimmt ihn nur bei Erfolg zurück.
- Höchstens zwei Prüfungen laufen gleichzeitig, weitere bekommen sofort eine Absage. So erschöpfen parallele Anfragen weder den Speicher, noch kommen sie an der Bremse vorbei.
- Grenzen: fünf Fehlversuche je Client in 15 Minuten (IPv6 je /64) und 30 von allen zusammen. Die Zahl der gemerkten Adressen ist begrenzt.
- `X-Forwarded-For` zählt nur, wenn `BROKERSYNC_TRUST_PROXY` den Proxy nennt (`serve.proxy_options`).

**Erstes Passwort (#89).** Es braucht den Einmal-Code aus `vault.new_setup_code`. Nur die Konsole zeigt ihn: am Ende der Installation (`ct/`, per `pct exec … brokersync-cli setup-code`), mit `brokersync-cli setup-code` und nach `reset-ui-password`. Er gilt 24 Stunden; im Tresor liegt nur sein SHA-256, ins Log kommt er nie. `/setup-password` steht deshalb nicht in `PUBLIC`: Die Middleware lässt die Seite ohne Login durch, der POST braucht aber das CSRF-Token.

**Sitzungen (#96).** Das Cookie trägt `session_tag`: einen Hash aus Passwort-Hash und Sitzungsversion `ui_session_gen` im Tresor. Abmelden, ein Passwortwechsel (*Sicherheit → Passwort der Oberfläche*) und *Alle anderen Sitzungen beenden* erhöhen die Version; danach meldet ein kopiertes oder älteres Cookie niemanden mehr an. Die Middleware beendet eine Sitzung nach 30 Minuten ohne Anfrage (`IDLE_SECONDS`, `seen` im Cookie) und nach 12 Stunden insgesamt (`MAX_SECONDS`, `since`). Zugangsdaten ändern, *Neu aufsetzen* und der Abrufmodus verlangen das Passwort erneut, wenn es länger als 15 Minuten her ist (`AppContext.reconfirm`, `auth` im Cookie; die Vorlagen zeigen das Feld über `_forms.reauth_field`). `vault.weak_password` lehnt Passwörter und Passphrasen aus `common-passwords.txt` ab (10 000 häufige plus deutsche), auch mit Ziffern oder Zeichen davor und dahinter.

**Formulare (#103).** Das Broker-Formular kommt bei einem Fehler mit Status 422 zurück, mit den Eingaben (nie Geheimnisse) und dem Fehler am Feld (`_forms.html`: `invalid`, `error`). Gespeichert wird erst, wenn alles stimmt. Flash-Meldungen haben `role="alert"` bzw. `"status"`.

**Lange Aktionen (#104).** Abruf, *Neu aufsetzen*, *Logos jetzt holen* und die Kontenprüfung von *Prüfung* laufen als Hintergrundjob (`overview.start_job` bzw. `launch`): immer nur einer, im Thread; das Ergebnis erscheint als Meldung der nächsten Seite (`AppContext.job_result`). *Prüfung* zeigt den letzten Stand der Kontenprüfung (im Speicher, höchstens 10 Minuten alt, sonst startet die Seite eine neue) mit *Neu prüfen*. Ein Lauf schreibt Broker, „n von m“ und Schritt (Anmeldung, Abruf, Buchen, Abgleich, Warten auf die App) nach `meta` `progress`; die Übersicht zeigt das in einem `aria-live`-Bereich und lädt sich alle 10 Sekunden neu, abschaltbar mit `?still=1`. Wealthfolios Konten und der nächste Lauf des Timers werden eine Minute gemerkt.

**Fehlerseiten (#106).** Fehler zeigen eine Seite im Layout (`app._error_pages`) statt JSON. Ein unerwarteter Fehler zeigt nur eine Fehler-ID mit 6 Zeichen; die steht auch im Log, der Fehlertext erscheint nie auf der Seite.

Verbindung: `brokersync serve` (`web/serve.py`) spricht HTTPS mit dem Zertifikat aus `/etc/wealthfolio-broker-sync/tls` (`tls.py`: eine CA des Containers, 2 Jahre, deren Name Constraints genau seine Namen und jede seiner Adressen einzeln erlauben – kein Netz, keine lokale Domäne (#92); passen sie nicht mehr, entsteht eine neue CA; das Serverzertifikat samt Kette signiert sie, `brokersync renew-cert` erneuert es bei `setup.sh` und täglich als root (`wealthfolio-broker-sync-cert.timer`, Neustart der Oberfläche nur bei Änderung), weitere Namen aus `extra-names`; `/ca.crt` liefert das CA-Zertifikat zum Import) und leitet Port 8090 dorthin um. Vor der App sitzt `web/gate.Gate`: Anfragen über 6 MB werden abgewiesen, bevor etwas gelesen wird (auch Login und Ersteinrichtung), Sicherheits-Header (CSP ohne Skripte und ohne Inline-Styles – Abstände über CSS-Klassen –, `frame-ancestors 'none'`, `Cross-Origin-Resource-Policy`, `no-store`, HSTS), die Prüfung des Host-Namens (#107: `gate.Hosts` lässt IP-Adressen durch und Namen nur, wenn sie bekannt sind – Containername auch mit lokaler Endung wie `.fritz.box`, Namen im Zertifikat, die Adresse aus den Benachrichtigungen, `BROKERSYNC_ALLOWED_HOSTS`; das schützt vor DNS-Rebinding, und die Weiterleitung von Port 8090 nutzt dieselbe Liste), die Entsperrseite und der Neuaufbau der App nach einem Schlüsselwechsel. Die Session liegt in einem signierten Cookie (`SameSite=strict`, `Secure`, höchstens 12 h). Jede POST-Anfrage prüft per Dependency ein CSRF-Token aus der Session. Aussehen wie Wealthfolio (#83): Flexoki-Farben hell und dunkel (nach dem Gerät; das Symbol im Header – ein Formular wie das Abmelden, `POST /theme` – kehrt sie für die Sitzung um: `session["flip"]` → `data-flip` an `<html>`, auf jeder Seite bis zum Abmelden, #123), Inter und Merriweather aus `web/static/fonts/`, Karten; auf dem Handy (unter 700 px) ist das Hauptmenü eine Leiste unten. Bei einer neuen Einrichtung steht oben eine Checkliste (#110: Wealthfolio, erster Broker, Benachrichtigungen, wahlweise Master-Passphrase und Zertifikat), ausblendbar, sobald alles erledigt ist (`meta` `checklist-hidden`). Die Übersicht zeigt je Broker eine Ampel (`views.broker_light`: rot = Abruf fehlgeschlagen oder Anmeldung nötig, gelb = Abweichungen, Unbekanntes, Fehlendes oder seit zwei Tagen kein Abruf, grün, grau = noch nie abgerufen) mit kurzen Gründen, die dorthin verlinken, wo sie erledigt werden (Anmeldung, *Unbekannt*, *Prüfung*); als Knöpfe nur *Jetzt abrufen* und ein Zahnrad zu den Einstellungen des Brokers; Meldungen und Abweichungen aufklappbar, die letzten drei Läufe, ältere aufklappbar. *Prüfung* fasst je Broker Abgleich, Anfangsbestände und Kontenprüfung in einer Karte zusammen. *Sicherheit*: Master-Passphrase und Zertifikat oben, die Prüfungen als Zusammenfassung mit den offenen Punkten. Das Hauptmenü hat fünf Punkte: Übersicht, Prüfung, Durchschau, Unbekannt (mit Anzahl) und Einstellungen; die Einstellungsseiten (`SETTINGS_PAGES` in `web/context.py`) haben darunter eine eigene Leiste. Datum und Zeit zeigen alle Seiten gleich, in deutscher Zeit (`web/views.py`: Filter `day` → `TT.MM.JJJJ`, `local` → `TT.MM.JJJJ HH:MM`; Wealthfolio und `state.db` speichern UTC, das steht nur in `<time datetime>`). Unbekannte Buchungen zeigt `/unknown` als Art, Betrag, Wertpapier, Stück und Text (`views.describe_unknown` kennt die Payload-Schlüssel aller Adapter), die Rohdaten nur aufklappbar, dazu je Buchung ein vorausgefülltes GitHub-Issue (`views.report_url`, #86) mit den geschwärzten Daten - geöffnet vom Nutzer, der Dienst sendet nichts. Tabellen stehen in `.table`, mit `<caption>` nur für Screenreader, `<thead>`/`<tbody>` und `scope="col"`; Auswahl-Checkboxen nennen ihre Buchung (#105); auf schmalen Bildschirmen wird jede Zeile zu einem Block, jede Zelle mit ihrem Spaltennamen aus `data-label`. Jedes Formularfeld prüft der Server selbst (`web/inputs.py`: Adressen nur http/https, Datumsangaben, ISIN und IBAN mit Prüfziffer, MIC, Symbole, Längen); welche Sicherheitsbibliotheken der Dienst nutzt und warum, steht in `SECURITY.md`.

Sprachen (#87): Deutsch ist Standard, Englisch die zweite Sprache (`Config.ui_language`, Seite *Darstellung*). `web/i18n.py` arbeitet wie gettext mit dem deutschen Text als Schlüssel: Vorlagen schreiben `{{ _("Speichern") }}` (eine Jinja-Funktion, die `lang` aus dem Kontext liest; der Schlüssel darf eigenes Inline-HTML enthalten, Parameter werden escapt), Python `tr(request, "…")` (`web/context.py`) oder `translate(text, lang)`; Texte, die vor der Sprache feststehen (`STATUS_LABEL`, `SETTINGS_PAGES`, die Prüfungen auf *Sicherheit*, Feldnamen von `web/inputs.py`), markiert `N_` und übersetzt die Seite. Das deutsche Ergebnis ist unverändert der Schlüssel; `i18n.EN` liefert das Englische, ein fehlender Eintrag zeigt Deutsch. Veränderliche Teile sind `{name}`-Platzhalter, die nach dem Nachschlagen gefüllt werden; Mehrzahl über zwei Schlüssel, die der Aufrufer wählt. Flash-Meldungen werden übersetzt gespeichert. Nicht übersetzt: ntfy-Nachrichten, Texte der Adapter (TAN-Abfragen, Fehlermeldungen, Feldnamen der Zugangsdaten), fremde Fehlertexte, Log-Zeilen, Kommentare der Buchungen in Wealthfolio (Teil des Duplikat-Fingerabdrucks), der GitHub-Bericht und die Entsperrseite (die Einstellung ist dann verschlüsselt). `tests/test_i18n.py` sammelt alle Schlüssel aus Vorlagen und Quelltext und verlangt für jeden einen englischen Eintrag.

Aufbau (#76): `web/app.py` baut die App (`create_app` mit Gate, Vorlagen und Filtern, Session, Login-Prüfung als Middleware, CSRF-Dependency) und bindet die Router aus `web/routes/` ein, einen `APIRouter` je Bereich, in der Reihenfolge von `routes.ROUTERS`: `setup.py` (`/setup-password`, `/login`, `/logout`, `/theme`, `/healthz`, `/metrics`, `/ca.crt`), `overview.py` (`/`, `/run`, `/settings`, `/unknown`, `/unknown/complete`, `/retired/dismiss`), `settings.py` (`/setup/wealthfolio`, `/notifications`, `/securities`, `/logos`), `brokers.py` (`/brokers`, `/brokers/{key}`, `…/refetch`, `…/statement`, `…/reset`), `broker_login.py` (`/brokers/{key}/login`), `check.py` (`/check`, `/check/{key}`, `/check/{key}/opening`), `lookthrough.py` (`/lookthrough…`, `/lookthrough/fire`), und `security.py` (`/security…`); `transfers.py` (`/transfers`) steht unter *Einstellungen* nur, solange Muster gespeichert sind; `duplicates.py` (`/duplicates`) steht vorerst in `routes.HIDDEN` statt in `ROUTERS` (#84). Was die Seiten teilen, steht in einem `AppContext` (`web/context.py`: Vault, State, Syncer, Vorlagen, Gate, Login-Bremse, wartende Broker-Logins, laufender Abruf; Methoden `render`, `cfg`, `update`, `reconfirm`, `wf_client`, `wf_accounts`, `finish_login`; dazu der laufende Hintergrundjob und die letzte Kontenprüfung), den `_create_app` einmal je Aufbau anlegt und in `app.state.ctx` ablegt; jede Route bekommt ihn per Dependency (`ctx: Ctx`). `redirect`, `csrf_guard` und `accounts_of` sind Funktionen dort. Keine Funktion unter `web/` liegt über der C901-Grenze.

| Route | Zweck |
|---|---|
| `GET /healthz` | Health-Check ohne Login |
| `GET /metrics` | Kennzahlen im Prometheus-Format (Läufe je Broker und Ergebnis, letzter Lauf) – nach Login oder mit `Authorization: Bearer $BROKERSYNC_METRICS_TOKEN` (≥ 32 Zeichen, OPS-04) |
| `/setup-password`, `/login`, `POST /logout` | UI-Passwort festlegen (mit Einrichtungscode, `?code=` füllt ihn vor), anmelden, abmelden |
| `GET /` | Übersicht: Status je Broker, letzte Läufe, Kontostand, Abgleich |
| `GET /settings` | Übersicht der Einstellungsseiten (Wealthfolio, Broker, Benachrichtigungen, Wertpapiere, Überträge, Logos, Sicherheit, Duplikate) |
| `POST /run` | Abruf starten, für alle oder einen Broker (`broker=`). Läuft im Hintergrund-Thread |
| `POST /checklist/hide` | Einrichtungs-Checkliste ausblenden |
| `/setup/wealthfolio` | Wealthfolio-URL, -Passwort und wahlweise Fingerabdruck des Zertifikats; speichern mit Verbindungstest oder nur testen (`test=1`) |
| `/brokers`, `/brokers/{key}` | Broker-Liste, Zugangsdaten, Konten, Startdatum, automatischer Abruf |
| `/brokers/{key}/login`, `POST …/login/start` | Login mit TAN, Code oder App-Bestätigung. Der GET fragt den Broker nie: Er zeigt die offene Anmeldung wieder oder eine Startseite. Erst der POST startet sie, unter der Sync-Sperre (#99). Nach 15 Minuten wird eine offene Anmeldung geschlossen |
| `/brokers/{key}/refetch` | nächster Abruf einmal wieder ab dem Startdatum |
| `/transfers` | Muster für Überträge auf eigene Konten; unter *Einstellungen* nur, solange welche gespeichert sind (#84) |
| `/notifications` | ntfy-Server, -Topic, -Token, öffentliche URL |
| `/securities` | Zuordnung ISIN → Tickersymbol und Börse |
| `/logos` | Logos von logo.dev an/aus, Schlüssel, Durchlauf jetzt (`logos.py`, auch nach jedem Abruf) |
| `/unknown` | unbekannte Buchungen und offene Wertpapier-Gegenbuchungen; `/unknown/complete` vervollständigt eine (#132) |
| `/duplicates` | Duplikate finden und die Kopien des Syncs nach Bestätigung löschen – **vorerst ausgeblendet** (`routes.HIDDEN`, #84) |
| `/check`, `POST /check/audit` | Prüfung: Abgleich Broker ↔ Wealthfolio aus dem letzten Lauf (`coverage.py`), dazu nur lesend, wo im Depotkonto Bargeld stehen bleibt und welche Überträge kein Gegenstück haben (`audit.py`, im Hintergrund, *Neu prüfen*) |
| `POST /check/{key}` | fehlende Transaktionen wieder anlegen lassen (`action=rebook`) oder ignorieren (`action=ignore`) |
| `POST /check/{key}/opening` | Anfangsbestand eintragen oder (noch nicht gebucht) entfernen |
| `/brokers/{key}/reset` | *Neu aufsetzen*: Der GET zeigt, wie viele Buchungen des Dienstes gelöscht würden; der POST braucht den Namen des Brokers und löscht im Hintergrund, dann wird der Status zurückgesetzt (`reset.py`, #110) |
| `/security`, `POST /security/passphrase`, `POST /security/lock`, `POST /security/password`, `POST /security/sessions` | Prüfungen (`security.status`), Master-Passphrase setzen/ändern/entfernen, sperren, Passwort der Oberfläche ändern, alle anderen Sitzungen beenden, Ereignisse |
| `/lookthrough`, `POST /lookthrough/refresh` | Durchschau (#139): Positionen aller Konten (im Hintergrund gelesen, 10 Minuten gemerkt), Klumpenrisiken, Positionslisten, Aktien über alle Positionen, Überschneidung der ETFs |
| `POST /lookthrough/upload`, `…/source`, `…/fetch` | Positionsliste hochladen (CSV/XLSX), iShares-Produktseite speichern, Positionslisten jetzt abrufen (Hintergrundjob, `Syncer.update_funds`) |
| `POST /lookthrough/settings`, `…/accept`, `…/taxonomies/remove` | Abruf und Eintragen in Wealthfolio an/aus, Grenzen der Klumpenrisiken; ein Risiko als gewollt markieren oder wieder melden; die Zuordnungen des Dienstes aus Wealthfolio entfernen |
| `/lookthrough/fire` | FIRE-Rechner mit deutschen Netto-Werten (`fire.py`), Eingaben in `Config.fire` |
| `/unlock` | Entsperren mit der Master-Passphrase (nur solange gesperrt) |
| `/retired/dismiss` | Hinweis zu einem entfernten Broker (z. B. dem Dummy) ausblenden |

**Durchschau (#139).** `lookthrough.read_depot` liest alle offenen Positionen aller Konten (Wert in der Basiswährung, Einstandswert, Bargeld je Konto). Eine Position ist ein Fonds, sobald seine Positionsliste in `state.funds` liegt (Migration 4); sonst eine Zeile für sich. `xray.unify` gibt Zeilen ohne ISIN (iShares-Listen) die ISIN derselben Firma aus einer anderen Liste (bereinigter Name), `xray.exposures` rechnet jede Firma einmal mit „davon über“, `xray.findings` die Klumpenrisiken über den Grenzen (`xray.LIMITS`, `Config.xray_limits`). Nach jedem Lauf (`Syncer._update_funds`, nur mit Positionslisten oder eingeschaltetem Abruf): fällige Listen holen (`lookthrough.update`: Xtrackers per ISIN als XLSX, iShares per Produkt-ID aus dem Fondsfinder von iShares (`ISHARES_SCREENER`, einmal je Lauf, nach ISIN) oder der gespeicherten Produktseite als CSV; nur `HOSTS` über https, Antworten bis 20 MB, eine Liste muss vollständig wirken; eine hochgeladene bleibt), wahlweise Länder und GICS-Sektoren in Wealthfolios Taxonomien `regions`/`industries_gics` schreiben (`write_taxonomies`, Quelle `BROKERSYNC`, nur wo keine Zuordnung des Nutzers steht – Wealthfolios Yahoo-Klassifizierung lässt Nicht-`AUTO`-Zuordnungen ebenfalls stehen), dann neue Klumpenrisiken einmal per ntfy (`meta` `xray:notified`; gewollte in `xray:accepted`). Nichts davon ändert das Ergebnis eines Brokers.

## Fehlerbehandlung

| Fehler | Ergebnis | Meldung |
|---|---|---|
| Broker will TAN oder Bestätigung | `needs_auth` | ntfy mit Link auf `/brokers/<key>/login` |
| `AdapterError` (Broker nicht erreichbar, PIN abgelehnt, Konto fehlt) | `error` | ntfy mit Link auf die Übersicht |
| `WealthfolioError` (nicht erreichbar, Passwort falsch, alte Überträge nicht reparierbar) | `error` | ebenso |
| unerwartete Exception im Adapter | `error` mit „Unexpected error“; die Session bleibt gespeichert, die anderen Broker laufen weiter | ebenso, plus Stacktrace im Journal |
| einzelne Aktivität abgelehnt | `error` mit Zähler `failed`; die Transaktion bleibt offen und wird im nächsten Lauf wiederholt | Liste der ersten fünf |
| Kontostand oder Positionen nicht lesbar, Wealthfolio liefert keine Bestände | Lauf bleibt `ok`, nur der Abgleich entfällt | — |

## Installation und Update

- **Installation:** Ein Befehl in der Proxmox-Shell. `ct/wealthfolio-broker-sync.sh` setzt `COMMUNITY_SCRIPTS_URL` auf dieses Repository und `COMMUNITY_SCRIPTS_CORE_URL` auf einen festen, geprüften Commit von community-scripts/core (die Engine läuft als root auf dem Host; `core-watch.yml` meldet neuere Stände) und lädt dann die Engine (`build.func`). Die Engine legt das LXC an und führt `install/wealthfolio-broker-sync-install.sh` darin aus.
- **Einrichtung im Container:** Das Install-Skript lädt das neueste GitHub-Release nach `/opt/wealthfolio-broker-sync/app` und ruft `deploy/setup.sh` auf. Das legt Benutzer, venv (nur die Versionen aus `requirements.txt`, mit Hash-Prüfung und nur als Wheels; das Paket selbst ohne weitere Abhängigkeiten) und systemd-Units an und installiert mit `brokersync install-sc` Scalables CLI nach `/opt/wealthfolio-broker-sync/bin/sc` (nur mit gültiger Signatur von Scalables Release-Schlüssel; schlägt das fehl, läuft alles außer Scalable).
- **Update:** Der Befehl `update` im Container stoppt Dienst und Timer und sichert `data/` nach `/opt/wealthfolio-broker-sync/backup-<Zeitstempel>.tar.gz` (unter `umask 077`, nur für root lesbar; die letzten drei bleiben). Dann lädt er das neueste Release und ruft wieder `setup.sh` auf. Schlägt die Installation fehl, kommt das vorherige venv zurück und läuft weiter.
- **Schlüssel, Zertifikat, Migration (`setup.sh`):** legt den Host-Schlüssel einmalig in `/etc/wealthfolio-broker-sync` an (aus `data/secret.key` übernommen, sonst neu; mit `systemd-creds` verschlüsselt, geprüft durch Entschlüsseln, sonst root-only), schreibt die Drop-ins `10-key.conf`, erzeugt oder erneuert das Zertifikat (`brokersync renew-cert --no-restart`), schaltet dessen täglichen Timer ein und führt `brokersync-cli migrate` aus (als `brokersync`, mit dem Schlüssel per `systemd-run`). Einmalig (Marker `key-rotated`) tauscht es den Host-Schlüssel in vier Schritten:

1. einen neuen erzeugen und als `key.cred.new` ablegen,
2. `brokersync-cli rotate-key` (neuer Schlüssel über stdin) legt alles neu verschlüsselt neben die alten Dateien,
3. `key.cred.new` ersetzt den alten Schlüssel – das ist der Umschaltpunkt,
4. `brokersync-cli recover` tauscht die Dateien ein.

Stockt es dazwischen, erledigt der nächste Start den Rest (siehe *Neu verschlüsseln ohne Datenverlust*). Bis 0.9.0 war der Schlüssel aus `data/secret.key` nur verschoben worden.
- **Release (#88):** `release.yml` ruft zuerst die ganze CI auf (`ci.yml` per `workflow_call`, derselbe Commit). Nur danach legt der Job `publish` – der einzige mit Schreibrechten – `v<version>` an, sobald eine neue Version in `pyproject.toml` auf `main` landet. Er hängt drei Dinge an:
  - das Archiv `wealthfolio-broker-sync-<version>.tar.gz`,
  - `SHA256SUMS`, je Datei des Commits eine Zeile (`git archive`, also derselbe Inhalt wie GitHubs Quell-Tarball, den die Engine lädt),
  - eine Build-Provenance-Attestation für beide (`gh attestation verify`).

  Releases sind unveränderlich (Repo-Einstellung).
- **Prüfung beim Laden (#88):** Installation und Update prüfen den entpackten Stand in `/opt/wealthfolio-broker-sync/app` mit `verify_release` (dieselbe Funktion in `ct/` und `install/`) gegen das `SHA256SUMS` genau dieses Releases (Version aus der Versionsdatei der Engine, `~/.wealthfolio-broker-sync`), bevor etwas davon läuft:
  - Jede Datei muss passen, und keine darf dazukommen. Ausgenommen sind nur `build/` und `*.egg-info` von pip.
  - Schlägt die Prüfung fehl, startet das Update die alte Version wieder, deren venv unverändert ist.
  - `VERSION=x.y.z update` holt eine bestimmte Version. Ohne das lehnt `setup.sh` eine ältere Version als die installierte ab (`BROKERSYNC_ALLOW_DOWNGRADE`).
- **Lieferkette:** `requirements.txt` (von `scripts/lock.sh` aus `pyproject.toml` und `requirements-build.in` erzeugt) hält jede Laufzeit-Abhängigkeit mit Version und Hashes; Dependabot schlägt wöchentlich Updates vor (für pytr nur Sicherheitsupdates), die CI testet genau diese Versionen. Die Test- und Lint-Werkzeuge der CI liegen ebenso mit Hashes in `requirements-dev.txt` (#97). `pip-audit` prüft `requirements.txt` bei jedem Push und wöchentlich auf bekannte Lücken; Pull Requests bekommen ein Dependency Review. Die CI prüft außerdem mit `ruff` (inklusive Komplexitätsgrenze C901 ≤ 15, seit 0.23.0 ohne Ausnahme, #80) und `mypy`, misst die Testabdeckung (mindestens 90 %) und vergleicht `contract/dedup/cases.json` mit der Kopie im Addon (ist die nicht lesbar, gibt es eine sichtbare Warnung). Sie scannt die ganze Historie mit gitleaks (`.gitleaks.toml`), alle Actions sind auf Commits gepinnt. Sicherheitslücken meldet man privat (`SECURITY.md`).

## Tests

`pytest -q` läuft ohne Netz und ohne echte Broker:

| Datei | Prüft |
|---|---|
| `tests/fakes.py` | ein Wealthfolio im Speicher (`httpx.MockTransport`): Login, Fingerprint-Duplikate, Suche, Ändern, Löschen von Übertragspaaren, Bestände (Überträge mit Asset sind Wertpapierüberträge) |
| `test_mapping.py` | Buchungsregeln (Zwei-Konten-Modell, Beträge, Überträge) |
| `test_sync.py` | Ablauf, Wiederholung nach Teilfehlern, verlorener Status, CSV-/PDF-Importe (auch Steuer nachtragen), Sperre, Fehler eines Brokers, Reparatur alter `$CASH`-Überträge |
| `test_fints.py` | was jedes FinTS-Profil bekommt, für DKB, Deutsche Bank und ein erfundenes Profil: Bankleitzahl, App-Freigabe, TAN-Eingabe, PIN-Schutz, Meldungen, Muster |
| `test_dkb.py`, `test_tr.py` | Adapter gegen nachgebaute python-fints- bzw. pytr-Clients: Login, PIN-Schutz, Fehlerpfade, WebSocket-Paging, Abgleich, Duplikate |
| `test_scalable.py` | Scalable gegen ein nachgebautes `sc` (`fixtures/scalable/fake_sc.py`): Gerätecode-Login, Sitzung im Tresor, abgelaufene Sitzung im Timer-Lauf, CLI nicht freigeschaltet, ganzer Abruf |
| `test_comdirect.py` | comdirect gegen eine nachgebaute API: photoTAN-Push in Web-UI und Timer-Lauf, Refresh, Sperrschutz (offene Challenges, falsche TANs, abgelehnte PIN), mobileTAN, Zuordnung Trade ↔ Girobuchung |
| `test_sc_install.py` | Installation von `sc`: Scalables echte Signatur, manipulierte Releases werden abgelehnt |
| `fake_broker.py` | ein erfundener Broker mit TAN-Schritt, nur für die Tests |
| `test_wealthfolio.py` | REST-Client gegen aufgezeichnete Antworten (`tests/fixtures/wealthfolio/`) |
| `test_web.py` | Oberfläche von der ersten Seite bis zum ersten Abruf, Login und CSRF, Aufräumen des alten Dummys |
| `test_cli.py` | `run`, `serve`, `reset-ui-password`, Exit-Codes |
| `test_vault_notify.py` | Tresor, ntfy |
| `test_security.py` | nichts Lesbares in `data/` und im Log nach einem echten Lauf, Migration von 0.8, Schlüssel nur von systemd, Master-Passphrase (sperren, entsperren, Timer-Lauf gesperrt), Login-Bremse (parallel, global, IPv6, Proxy), absturzsichere Neuverschlüsselung, Schwärzung ohne angehäufte Sitzungen, argon2id statt scrypt, Header und Cookies, Schwärzung, ntfy ohne Details, Zertifikat, HTTP→HTTPS |
| `test_packaging.py` | community-scripts-Dateien, Installationszeile, Versionen, Prüfung gegen `SHA256SUMS` (geänderte/zusätzliche Datei), Downgrade-Schutz |
| `contract/` | jeder Adapter gegen erfundene Aufzeichnungen; jede Transaktion lässt sich buchen |
| `test_dedup_contract.py` | `dedup.ExistingIndex` gegen die gemeinsamen Fälle in `contract/dedup/cases.json` (dieselben prüft das Addon) |
