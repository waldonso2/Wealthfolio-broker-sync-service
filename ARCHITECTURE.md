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

Beide laufen als Systembenutzer `brokersync` mit systemd-Härtung (`ProtectSystem=strict`, `PrivateDevices`, `CapabilityBoundingSet=` leer, `RestrictNamespaces`, `UMask=0077` …) und dürfen nur in `data/` schreiben. Den Schlüssel bekommen sie von systemd als Credential (`$CREDENTIALS_DIRECTORY/brokersync-key`, Drop-in `10-key.conf` von `setup.sh`); `/run/wealthfolio-broker-sync` (RAM, `RuntimeDirectoryPreserve=yes`) teilen sie sich für den entsperrten Passphrasen-Schlüssel. Eine Lock-Datei (`data/sync.lock`, `flock`) sorgt dafür, dass immer nur ein Abruf läuft, auch wenn Timer und Weboberfläche gleichzeitig starten.

## Module

```mermaid
flowchart TB
  cli["cli.py"] --> sync
  web["web/app.py"] --> sync
  sync["sync.py<br/>Ablauf eines Abrufs"] --> adapters
  sync --> mapping["mapping.py<br/>Transaction → NewActivity"]
  sync --> dedup["dedup.py<br/>ExistingIndex"]
  sync --> wf["wealthfolio.py<br/>REST-Client"]
  sync --> reconcile["reconcile.py<br/>Bestandsabgleich"]
  sync --> assets["assets.py<br/>ISIN → Wealthfolio-Asset"]
  sync --> repair["repair.py<br/>$CASH-Asset entfernen (einmalig)"]
  sync --> retired["retired.py<br/>entfernte Broker aufräumen"]
  sync --> notify["notify.py<br/>ntfy"]
  sync --> state["state.py"] & vault["vault.py"] & config["config.py"]
  web --> duplicates["duplicates.py<br/>Bereinigung"]
  subgraph adapters["adapters/"]
    base["base.py<br/>BrokerAdapter"] --- fints["fints.py<br/>FintsAdapter"] & tr["tr.py"] & scalable["scalable.py"] & comdirect["comdirect.py"]
    fints --- dkb["dkb.py<br/>Profil"] & deutschebank["deutschebank.py<br/>Profil"]
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
| 3. Abgleich mit Importen | `dedup.ExistingIndex` | Aktivitäten aus CSV-/PDF-Importen: gleiches Konto und gleicher Typ, ≤ 36 h Abstand, gleiche Stückzahl, Betrag ±0,02. Das Symbol muss nicht übereinstimmen (Import: zugeordnetes Tickersymbol, Sync: ISIN) |

Geht der Sync-Status verloren (`state.db` gelöscht), findet Schicht 2 alles wieder. `test_lost_state_is_recovered_from_the_comments` prüft das.

`state.synced` entscheidet nur, was **neu** ist. Ob eine früher gebuchte Transaktion noch in Wealthfolio steht, prüft jeder Lauf danach gegen Wealthfolio (`coverage.py`): über die `[SYNC …]`-Referenz und die gespeicherten Aktivitäts-Ids. Fehlt sie ganz oder teilweise, steht sie auf der Seite *Prüfung*; der Nutzer wählt *Wieder anlegen* (der Eintrag in `synced` wird gelöscht, der nächste Lauf behandelt sie als neu, mit allen drei Schichten) oder *Ignorieren* (Status `ignored`). Von selbst legt der Sync nichts wieder an, weil eine Löschung in Wealthfolio Absicht sein kann. Bei `full_history` meldet er außerdem Aktivitäten mit `[SYNC …]`, deren Transaktion der Broker nicht mehr auflistet (z. B. storniert) - löschen muss sie der Nutzer.

Eine Transaktion wird erst als synchronisiert markiert, wenn **alle** ihre Aktivitäten existieren. Ein Kauf besteht zum Beispiel aus Übertrag raus, Übertrag rein und dem Kauf selbst. Bricht der Lauf nach dem ersten Teil ab, legt der nächste Lauf die fehlenden Teile an, und die vorhandenen kommen als `Duplicate` zurück.

Duplikate, die ältere Versionen trotzdem erzeugt haben, findet `duplicates.py` (Seite *Duplikate*). Gelöscht wird nur die Kopie des Syncs und nur nach Bestätigung.

## Buchungsmodell

Jeder Broker hat in Wealthfolio zwei Konten: ein **Cash-Konto** und ein **Depot**. `mapping.py` setzt das um:

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
| `UNKNOWN` | nichts. Gespeichert, auf der Seite *Unbekannt* gelistet, einmal gemeldet |

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
| `dkb` | FinTS über python-fints (`FintsAdapter`) | Freigabe in der DKB-App (decoupled). Ids sind Hashes des Buchungsinhalts plus Zähler. Wertpapier-Gegenbuchungen auf dem Giro werden zu `SECURITIES_CASH` |
| `deutschebank` | FinTS (`FintsAdapter`), Profil mit Bankleitzahl als Zugangsfeld | BestSign (decoupled). Depotbestand per HKWPD, auch für ein Depot ohne IBAN, das nur in den Benutzerparametern (UPD) steht; `reports_positions`. Beim Login loggt der Adapter, was die Bank anbietet (Umsätze, camt, Depotbestand, Depotumsätze, je Konto Produktname und Art, keine Nummern) |
| `tr` | WebSocket der Trade-Republic-App über pytr (fest gepinnt) | Web-Login mit Bestätigung in der App. Timeline (Transaktionen und Aktivitätslog, seitenweise bis `since`), Details in Batches zu 20. Geparst mit pytrs `Event.from_dict` |
| `comdirect` | Offizielle REST-API (`api.comdirect.de`, httpx) | Passwort-Grant → Session-Objekt → TAN-Challenge (`validate`) → Aktivierung (`PATCH`) → `cd_secondary`-Token. photoTAN-Push: Web-UI „bestätigt“, Timer-Lauf ntfy + Abfrage des Status-Links; TAN zum Eintippen nur in der Web-UI. Token (10 min) und Refresh-Token in der Session. Sperrschutz: `open_challenges` (vor jeder Challenge hochgezählt, < 5) und `wrong_tans`. Girobuchungen + Depotumsätze; Käufe/Verkäufe mit der Girobuchung (*Wertpapier*), die sie abgerechnet hat |
| `scalable` | Scalables offizielles CLI `sc` (Unterprozess, `--json`) | Gerätecode-Login mit `--local-read-only`: Link und Code im Browser bestätigen. Das Konfigurationsverzeichnis des CLI (Sitzung mit rotierendem Refresh-Token, DPoP-Schlüssel) liegt nur während des Laufs in einem temporären Verzeichnis, sein Inhalt verschlüsselt im Tresor (`session["files"]`). Transaktionen seitenweise, Details für Trades und Ausschüttungen – nur für noch nicht übernommene (`known_ids`), mit 1 s Abstand; bei `rate_limited` warten und wiederholen, hält das Limit an, kommen die restlichen Trades beim nächsten Lauf. `sc` installiert `brokersync install-sc` aus Scalables signiertem Release |

Einen Test-Broker liefert der Dienst nicht aus; die Tests nutzen `tests/fake_broker.py`. Den früheren Test-Broker „Dummy“ (bis 0.3.6) räumt `retired.py` beim Start auf: Einstellungen, Zugangsdaten, Sync-Status, Läufe, unbekannte Buchungen und Abgleich werden gelöscht. Seine Buchungen in Wealthfolio bleiben; die Übersicht zeigt einmal, wie viele es sind und wie man sie findet (`[SYNC dummy:`).

**FinTS-Banken** teilen sich `FintsAdapter` (`adapters/fints.py`): Login mit Freigabe in der App oder mit TAN-Eingabe, PIN-Schutz, Session, Abruf und die Einordnung der Giro-Buchungen. Eine Bank ist nur ein Profil (`dkb.py`, `deutschebank.py`): Bankleitzahl (fest oder als Zugangsfeld, wenn sie je Filiale verschieden ist), Server, Namen in den Meldungen und, falls die Buchungstexte abweichen, eigene Muster für Wertpapier, Zins und Gebühr. Die Ids hängen nur an der Buchung, nicht am Profil.

Jeder Adapter hat `replay(recording)` für Contract-Tests (`tests/contract/<adapter>/`). Er antwortet dann aus einer Aufzeichnung statt vom Broker.

## Bestandsabgleich

Nach jedem Lauf ohne Fehler vergleicht `reconcile.py` den Kontostand und, bei Adaptern mit `reports_positions`, die Positionen des Brokers mit `GET /holdings` in Wealthfolio:

- **Zusätzliche Prüfungen:** Cash auf dem Depotkonto muss 0 sein, weil jede Zahlung dort vom Cash-Konto kommt oder dorthin zurückgeht. Eine `$CASH`-Position auf einem der beiden Konten weist auf einen alten Übertrag hin, der kein Geld bewegt hat.
- **Positionen ohne ISIN:** Wealthfolio-Positionen tragen keine ISIN. `assets.py` lernt deshalb aus den Aktivitäten von Käufen und Dividenden, unter welchem Asset eine ISIN gebucht ist. Eigene Zuordnungen auf der Seite *Wertpapiere* haben Vorrang.
- **Neuberechnung abwarten:** Wurde etwas angelegt oder repariert, wartet der Sync kurz (`BROKERSYNC_RECALC_WAIT`, Standard 5 s), weil Wealthfolio die Bestände im Hintergrund neu berechnet.
- **Gemeldet wird nur, was bleibt:** erst eine Abweichung, die zwei Läufe in Folge besteht, und dieselbe Menge von Abweichungen nur einmal.

## Daten und Geheimnisse

Alles liegt verschlüsselt in `BROKERSYNC_DATA` (Standard `/opt/wealthfolio-broker-sync/data`, Modus 0700, Dateien 0600); der Schlüssel liegt nicht dort (`crypto.py`).

**Schlüssel.** Der *Host-Schlüssel* (32 Zufallsbytes) kommt in dieser Reihenfolge aus `$CREDENTIALS_DIRECTORY/brokersync-key` (systemd: `LoadCredentialEncrypted=` aus `/etc/wealthfolio-broker-sync/key.cred`, mit `systemd-creds` verschlüsselt; wo das nicht geht `LoadCredential=` aus einer root-only Datei), `$BROKERSYNC_KEY_FILE` oder – nur Entwicklung, Tests und Installationen vor 0.9.0 – `data/secret.key`. Daraus leitet HKDF-SHA256 zwei Stufen ab:

| Stufe | Schlüssel | Schützt |
|---|---|---|
| Host | Host-Schlüssel | `notify.enc` – damit ein gesperrter Dienst per ntfy Bescheid sagen kann |
| Daten | Host-Schlüssel + argon2id(Master-Passphrase), falls gesetzt | `secrets.enc`, `config.enc`, `state.db` |

Mit Passphrase hält `keyring.json` Salz, argon2id-Parameter und einen Prüfwert (nie die Passphrase). Entsperren legt das argon2id-Ergebnis in `/run/wealthfolio-broker-sync/unlock.key` (RAM, weg nach Neustart). Ist der Dienst gesperrt, zeigt die Weboberfläche nur `/unlock` (`web/gate.py`), und `brokersync run` meldet sich per ntfy und endet mit 1. Setzen, Ändern und Entfernen (`security.change_passphrase`) verschlüsseln alles der Datenstufe neu (SQLCipher `PRAGMA rekey`), unter dem Sync-Lock.

| Datei | Inhalt | Format |
|---|---|---|
| `secrets.enc` | Wealthfolio-Passwort, Zugangsdaten und Sessions der Broker, argon2id-Hash des UI-Passworts, Signierschlüssel der UI-Session | AES-256-GCM, `vault.py`, atomar und mit Lock |
| `config.enc` | Wealthfolio-URL, je Broker Konten, `enabled`, `start_date`, Wertpapier-Zuordnungen, Übertrags-Muster | AES-256-GCM, `config.py` |
| `notify.enc` | ntfy-Server, -Topic, -Token, Details an/aus, öffentliche URL | AES-256-GCM, Host-Stufe |
| `state.db` | siehe unten | SQLCipher (AES-256); nur ohne SQLCipher-Wheel (nicht x86-64) unverschlüsselt, die Seite *Sicherheit* sagt es |
| `keyring.json` | Parameter der Master-Passphrase | Klartext, nichts Geheimes |
| `*.lock` | Sperrdateien | — |

Jede verschlüsselte Datei ist `BSE1 | Nonce | Chiffrat`; ihr Zweck (`secrets`, `config`, …) ist als zusätzliche Daten authentifiziert, eine Datei lässt sich also nicht gegen eine andere tauschen. `brokersync migrate` (von `setup.sh` aufgerufen) bringt Daten von vor 0.9.0 – Fernet-`secrets.enc`, `config.json`, unverschlüsselte `state.db` – auf diesen Stand; danach löscht `setup.sh` `data/secret.key` und ersetzt Sicherungen, die ihn noch enthielten.

**Tabellen in `state.db`:**

| Tabelle | Inhalt |
|---|---|
| `synced` | `(broker, tx_id)`, Status `imported`/`existing`/`ignored` und die angelegten Aktivitäts-Ids |
| `gaps` | Ergebnis des letzten Vergleichs mit Wealthfolio: `missing`, `partial`, `ignored`, `orphan` |
| `openings` | Anfangsbestände, die der Nutzer auf *Prüfung* einträgt (ISIN, Kaufdatum, Stück, Kurs, Gebühr); jeder Lauf bucht sie als Kauf `start-<isin>-<tag>` mit Einzahlung |
| `runs` | Verlauf der Abrufe: `running`, `ok`, `needs_auth`, `error`, `aborted` und die Zähler |
| `unknown_events` | unbekannte Buchungen und offene Wertpapier-Gegenbuchungen, mit einer Nutzlast ohne persönliche Daten |
| `balances`, `reconcile` | letzter Kontostand des Brokers und Abweichungen, samt dem, was schon gemeldet wurde |
| `assets` | ISIN → Wealthfolio-Asset je Broker |
| `meta` | Flags, z. B. `assets-learned:<broker>`, `cash-assets-repaired:<broker>`, `refetch:<broker>`, und der Hinweis `retired-notice:<broker>` zu einem entfernten Broker |

Geheimnisse stehen nur im Vault. In Logs, Fehlermeldungen auf dem Bildschirm und ntfy-Nachrichten schwärzt `redact.py` alles, was der Vault an Zugangsdaten und Tokens hält, sowie IBANs und lange Nummern; ntfy-Nachrichten tragen ohne *Details* nur Titel und Link.

## Weboberfläche

FastAPI mit Jinja2-Vorlagen (`web/templates/`), Texte auf Deutsch, eigener Login mit dem UI-Passwort (argon2id; nach fünf Fehlversuchen 15 Minuten Sperre je Client, `web/gate.Throttle`). `brokersync serve` (`web/serve.py`) spricht HTTPS mit dem Zertifikat aus `/etc/wealthfolio-broker-sync/tls` (`tls.py`, selbstsigniert, von `setup.sh` erneuert) und leitet Port 8090 dorthin um. Vor der App sitzt `web/gate.Gate`: Sicherheits-Header (CSP ohne Skripte, `frame-ancestors 'none'`, `no-store`, HSTS), die Entsperrseite und der Neuaufbau der App nach einem Schlüsselwechsel. Die Session liegt in einem signierten Cookie (`SameSite=strict`, `Secure`, 24 h). Jede POST-Anfrage prüft per Dependency ein CSRF-Token aus der Session.

| Route | Zweck |
|---|---|
| `GET /healthz` | Health-Check ohne Login |
| `/setup-password`, `/login`, `POST /logout` | UI-Passwort festlegen, anmelden, abmelden |
| `GET /` | Übersicht: Status je Broker, letzte Läufe, Kontostand, Abgleich |
| `POST /run` | Abruf starten, für alle oder einen Broker (`broker=`). Läuft im Hintergrund-Thread |
| `/setup/wealthfolio` | Wealthfolio-URL und -Passwort, Verbindungstest |
| `/brokers`, `/brokers/{key}` | Broker-Liste, Zugangsdaten, Konten, Startdatum, automatischer Abruf |
| `/brokers/{key}/login` | Login mit TAN, Code oder App-Bestätigung |
| `/brokers/{key}/refetch` | nächster Abruf einmal wieder ab dem Startdatum |
| `/transfers` | Muster für Überträge auf eigene Konten |
| `/notifications` | ntfy-Server, -Topic, -Token, öffentliche URL |
| `/securities` | Zuordnung ISIN → Tickersymbol und Börse |
| `/unknown` | unbekannte Buchungen und offene Wertpapier-Gegenbuchungen |
| `/duplicates` | Duplikate finden und die Kopien des Syncs nach Bestätigung löschen |
| `/check` | Prüfung: Abgleich Broker ↔ Wealthfolio aus dem letzten Lauf (`coverage.py`), dazu nur lesend, wo im Depotkonto Bargeld stehen bleibt und welche Überträge kein Gegenstück haben (`audit.py`) |
| `POST /check/{key}` | fehlende Transaktionen wieder anlegen lassen (`action=rebook`) oder ignorieren (`action=ignore`) |
| `POST /check/{key}/opening` | Anfangsbestand eintragen oder (noch nicht gebucht) entfernen |
| `POST /brokers/{key}/reset` | *Neu aufsetzen*: Buchungen des Dienstes für den Broker löschen, Status zurücksetzen (`reset.py`) |
| `/security`, `POST /security/passphrase`, `POST /security/lock` | Prüfungen (`security.status`), Master-Passphrase setzen/ändern/entfernen, sperren |
| `/unlock` | Entsperren mit der Master-Passphrase (nur solange gesperrt) |
| `/retired/dismiss` | Hinweis zu einem entfernten Broker (z. B. dem Dummy) ausblenden |

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

- **Installation:** Ein Befehl in der Proxmox-Shell. `ct/wealthfolio-broker-sync.sh` setzt `COMMUNITY_SCRIPTS_URL` auf dieses Repository und lädt dann die community-scripts-Engine (`build.func`). Die Engine legt das LXC an und führt `install/wealthfolio-broker-sync-install.sh` darin aus.
- **Einrichtung im Container:** Das Install-Skript lädt das neueste GitHub-Release nach `/opt/wealthfolio-broker-sync/app` und ruft `deploy/setup.sh` auf. Das legt Benutzer, venv und systemd-Units an und installiert mit `brokersync install-sc` Scalables CLI nach `/opt/wealthfolio-broker-sync/bin/sc` (nur mit gültiger Signatur von Scalables Release-Schlüssel; schlägt das fehl, läuft alles außer Scalable).
- **Update:** Der Befehl `update` im Container stoppt Dienst und Timer und sichert `data/` nach `/opt/wealthfolio-broker-sync/backup-<Zeitstempel>.tar.gz` (die letzten drei bleiben). Dann lädt er das neueste Release und ruft wieder `setup.sh` auf. Schlägt die Installation fehl, kommt das vorherige venv zurück und läuft weiter.
- **Schlüssel, Zertifikat, Migration (`setup.sh`):** legt den Host-Schlüssel einmalig in `/etc/wealthfolio-broker-sync` an (aus `data/secret.key` übernommen, sonst neu; mit `systemd-creds` verschlüsselt, geprüft durch Entschlüsseln, sonst root-only), schreibt die Drop-ins `10-key.conf`, erzeugt oder erneuert das Zertifikat (`brokersync make-cert`) und führt `brokersync-cli migrate` aus (als `brokersync`, mit dem Schlüssel per `systemd-run`).
- **Release:** Der Release-Workflow legt `v<version>` an, sobald eine neue Version in `pyproject.toml` auf `main` landet.

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
| `test_security.py` | nichts Lesbares in `data/` und im Log nach einem echten Lauf, Migration von 0.8, Schlüssel nur von systemd, Master-Passphrase (sperren, entsperren, Timer-Lauf gesperrt), Login-Bremse, argon2id statt scrypt, Header und Cookies, Schwärzung, ntfy ohne Details, Zertifikat, HTTP→HTTPS |
| `test_packaging.py` | community-scripts-Dateien, Installationszeile, Versionen |
| `contract/` | jeder Adapter gegen erfundene Aufzeichnungen; jede Transaktion lässt sich buchen |
