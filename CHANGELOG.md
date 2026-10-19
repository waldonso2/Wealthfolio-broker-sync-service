# Changelog

## 0.29.0

- **Spending per bank (waldonso2/wealthfolio-importer-addon#134):** a new switch *Ausgaben auswerten* in a broker's settings puts its cash account into Wealthfolio's spending tracker after the next fetch (and turns the tracker on). Wealthfolio categorizes the bookings with its own rules (*Spending → Rules*, with presets), which match the activity's note - the comment already names counterparty and purpose, so no activity changes and nothing is booked anew. Own transfers and securities settlements are linked pairs, which the tracker counts as saving or neutral, not as spending. Switched off, the account leaves the tracker again, unless it was tracked in Wealthfolio before; accounts chosen there are never removed. A Wealthfolio without the tracker only logs a warning.

## 0.28.0

- **Complete unknown bookings (waldonso2/wealthfolio-importer-addon#132):** on *Unbekannte Buchungen*, each booking can be completed: buy or sale (shares and the money that moved, fee, tax), delivery in without payment (shares and cost basis), delivery out without payment, or a split. The next fetch books it by the booking model under the booking's own id, so it leaves the list and is never booked twice, even once the service learns the type itself. Until then it can be withdrawn. New table `completions` (state.db migration 3).
- **Splits and reverse splits (#133):** booked as one SPLIT activity on the securities account with Wealthfolio's ratio (new shares per old one; a 1:10 reverse split is 0.1). Trade Republic's reverse splits (waldonso2/Wealthfolio-broker-sync-service#81, #82) and dividend reinvestments (#77) can be completed this way.

## 0.27.0

- **Any bank with FinTS (waldonso2/wealthfolio-importer-addon#136):** *Weitere Bank 1–3 (FinTS)* connect a bank without a profile of its own - Sparkasse, Volks- und Raiffeisenbank and most other German banks - with its bank code and FinTS address (https only) as credentials. Bookings as for DKB with the standard posting patterns; a reported depot's holdings are read but count as incomplete. Up to three such banks at once; each keeps its key for good.
- **The bank code is enough:** for *Weitere Bank*, the FinTS address comes from the Deutsche Kreditwirtschaft's list of FinTS banks (1,716 bank codes), shipped with the service and kept out of plain sight like the product id. The field is only needed for a bank the list lacks.
- **The service's own FinTS product id is built in:** the field *FinTS-Produkt-ID* is gone for every FinTS bank. An id entered before is used until the bank's credentials are saved anew; then the bank may ask once more for a confirmation in its app. The id is kept out of plain sight in the code and out of logs and pages.
- The broker form now checks bank code (8 digits), FinTS address and IBANs on the server; a wrong one comes back at its field.
- **Hint at a new version:** after a fetch, at most once a day, the service asks GitHub which release is the newest (only the redirect of `releases/latest` is read; nothing is sent). A newer one is shown on the overview with a link to its notes, and ntfy tells it once. Installing stays `update` in the container's console. `BROKERSYNC_UPDATE_CHECK=0` turns the check off.

## 0.26.1

- **Faster duplicate check:** matching against the activities already in Wealthfolio parses each one once and looks only at the same account, type and time window (36 h, settlements 6 days), instead of re-reading every activity for every booking. The rules and their order are unchanged (`contract/dedup/cases.json`). In the performance report, a run of 300 bookings against 3000 existing activities took 2.0 s instead of 3.7 s.
- **Contrast in dark mode:** links had 4.4:1 on cards (WCAG AA asks 4.5:1); the dark accent is now `#66a0c8` (6.1:1).
- CI: zizmor for the workflows, ruff with `S`/`SIM`/`PERF`/`C4`/`RUF`, every page checked in Chromium (axe-core, CSP, phone width), a performance report on the run's summary page that never fails a build, OpenSSF Scorecard, Dependabot cooldown of 7 days. `KENNZAHLEN.md` lists the KPIs with their reference standards.

## 0.26.0

- **JSON logs and metrics (OPS-04 of #85):** `BROKERSYNC_LOG_FORMAT=json` writes one JSON object per log line, redacted like the plain lines. `GET /metrics` gives Prometheus text: fetches per broker and outcome, and the last fetch's time, duration, success and failed bookings - no amounts, no names. It needs the UI login, or for a scraper `Authorization: Bearer <BROKERSYNC_METRICS_TOKEN>` (at least 32 characters).
- `.github/CODEOWNERS` for `ct/`, `install/`, `deploy/` and `.github/` (waldonso2/wealthfolio-importer-addon#88; takes effect with branch protection).

## 0.25.0

Audit 2.0 (waldonso2/Wealthfolio-broker-sync-service#85), every finding checked against the code:

- **Encrypted database on ARM64 (SEC-05):** SQLCipher now also comes for ARM64 (`sqlcipher3-wheels`, a build of the same `sqlcipher3` module; locked with hashes). An existing plain `state.db` is encrypted at the next start. Only platforms without either wheel (e.g. 32-bit ARM) stay plain, as *Sicherheit* says.
- **Setup code never in a URL (V-01):** a password that is too short or too common no longer sends the browser to `/setup-password?code=…` (the history kept the code); the page comes back with the code in its form.
- **Masked texts in the comparison (SEC-04):** what *Prüfung* stores about missing bookings and deviations has its texts masked like unknown bookings (IBANs, long numbers); ids, amounts and ISINs stay.
- **No error page for a damaged setup code (V-02):** it counts as expired.
- **Run texts in English (V-04):** "aborted by a restart" follows the page's language; texts of banks and brokers stay as they are.
- **TAN pictures only embedded (SEC-08):** the login shows a TAN graphic only as a data: URL (the CSP already allowed nothing else).
- **Duplicates by meaning (SEC-09):** Wealthfolio's duplicate answer is recognised in any wording.
- **Bank not answering (UX-04):** FinTS says in German that the bank doesn't answer (network or timeout), the technical text after it.
- **Login: until when, and start over (UX-05):** the login page says until when it waits and offers *Anmeldung neu starten*.
- Checked and already in place: the FinTS session never holds the PIN (SEC-02 - python-fints doesn't serialise it; a test now pins the blob's contents), `BROKERSYNC_TRUST_PROXY` (SEC-03, since 0.20.0), progress of a run and screen-reader labels (UX-01, A11Y-01, since 0.21.0), the Argon2 parameters of the passphrase (V-03: stored with it since 0.9.0).
- Planned for 0.26.0: optional JSON logs and metrics (OPS-04).

## 0.24.1

- **Light/dark stays when you change the page (#123):** the header's symbol switched only the page it was tapped on; the next page came back in the device's colours. The session now remembers the choice: every page until you log out, a new session starts in the device's colours again. Still no JavaScript (a small form like *Abmelden*).
- The symbol sits at the height of the logout symbol.

## 0.24.0

- **Light or dark from the header (waldonso2/wealthfolio-importer-addon#123):** a symbol in the header (a half-filled circle, also on phones) switches between light and dark with one tap. Every visit starts in the device's colours; the switch lasts until the page is reloaded. It works without JavaScript (a checkbox evaluated by CSS), so the CSP stays as it was.
- The colour scheme is no longer chosen on *Darstellung* (only the language); a fixed choice saved before is ignored.

## 0.23.2

- **"Fehlt in Wealthfolio" for bookings that are there:** a transaction booked before whose activities the sync can't find any more is first looked for among the imports, with the rules of the duplicate detection. If an import holds the same booking, it is linked to that activity and not reported. This happens when Wealthfolio took every part of a booking for a duplicate of the import (nothing created, so no id to remember), or when an import was booked anew. Bookings that are really gone are reported as before.

## 0.23.1

- **Trade Republic: three more kinds of bookings** (waldonso2/Wealthfolio-broker-sync-service#76, #78, #79, as the CSV import books them):
  - a tax correction that takes money (pytr calls it a refund) is a tax (TAX);
  - a referral bonus is a bonus (CREDIT/BONUS) on the cash account;
  - a security written off as worthless is a sale at 0 on the securities account, without any transfer.
- A dividend reinvestment stays unknown (#77): pytr reads no share count for it. Book it by hand or import it.
- An unknown booking that a later run books (after *Ab Startdatum neu abrufen*) leaves the page *Unbekannt*.
- README: only the current version is described.

## 0.23.0

Data and cleanup, with the addon's 3.9.4 (waldonso2/wealthfolio-importer-addon#121).

- **Duplicate detection (#100):** of several activities that fit, the nearest in time is the one (then the closest amount) - two equal savings-plan deposits on two days each find their own, not the first in Wealthfolio's list. Two currencies never match. New shared cases in `contract/dedup/cases.json`, the addon applies the same rules.
- **`Position.value` removed (#118):** read nowhere, and 100 times too high for bonds priced in per cent; MT535 reads only shares and currency.
- **Simpler code (#80):** no C901 exception left; `mapping.to_activities`, `reconcile.compare`, `audit.check`, `assets.learn_from_imports`, `coverage.check`, the FinTS account lookup, `scalable._netted`, `mt535._holding` and `mt536.parse` split into named steps (activities, comments and ids unchanged); imports before the bank history and opening positions in `history.py`; the version only in `pyproject.toml`.
- **Unofficial interface and FinTS product id (#112):** Trade Republic asks once in the broker settings that its note was read - without it no login starts; brokers set up before count as agreed. The FinTS product id field says to use only your own registered id.

## 0.22.0

Transport, isolation, operation (waldonso2/wealthfolio-importer-addon#121).

- **The UI's CA signs only this container (#92):** its name constraints are now exactly the container's name, `localhost` and its current addresses (each a single address), no longer every private network and local domain - whoever has its key can't vouch for the router or the NAS. **Import the CA once more after this update** (page *Sicherheit*); a new address means a new CA. CA valid 2 years. A root timer (`wealthfolio-broker-sync-cert.timer`, `brokersync renew-cert`) renews the certificate daily and restarts the UI when it changed. More names or addresses (also Tailscale's 100.64.0.0/10) in `/etc/wealthfolio-broker-sync/tls/extra-names`.
- **`sc` in a sandbox (#93):** Scalable's CLI runs under Landlock (`brokersync.sandbox`): it can't read `data/`, the key or the unlock cache, and writes only its temp dir; its environment holds no `CREDENTIALS_DIRECTORY` or `BROKERSYNC_*`. The units allow `@sandbox`. A kernel without Landlock runs it without, and *Sicherheit* says so.
- **Honest protection (#94):** *Sicherheit* tells TPM, the container's host secret and a root-only file apart; without a TPM and a master passphrase it says a container backup holds key and data. Footer and summary claim database encryption only with SQLCipher. `setup.sh` moves a root-only key into `systemd-creds` once that can encrypt.
- **Wealthfolio with its own certificate (#95):** trusted by its SHA-256 fingerprint, checked in the TLS handshake before the password is sent. The page shows the fingerprint of a certificate nobody vouches for, to compare and save. *Nur testen* checks address, certificate and password without saving; a warning at the field for `http://`.
- **Event log (#102):** logins (also failed), logouts, password, sessions, credentials, Wealthfolio address, passphrase, fetch mode, *Neu aufsetzen*, lock and unlock, with the client's address, on *Sicherheit* (last 100, kept 180 days; new table, migration 2). A login from a new address is notified via ntfy.
- **Backup and restore (#109):** `brokersync-cli backup > file`, `brokersync-cli restore < file` - restore checks the archive opens with this container's key before it touches `data/`, keeps the previous data, refuses while the UI runs.
- The command line is one function per command.

## 0.21.0

UI sessions, forms and long actions (waldonso2/wealthfolio-importer-addon#121).

- **Sessions (#96):** a sign-in ends after 30 minutes without activity and after 12 hours at all. Logout ends every session (a copied cookie no longer works; `Clear-Site-Data`). New on *Sicherheit*: change the UI password (ends all other sessions) and *Alle anderen Sitzungen beenden*. Changing credentials, *Neu aufsetzen* and the fetch mode ask for the password again when the sign-in is older than 15 minutes. Common passwords (a list of 10,000 plus German ones, also with digits around them) are refused for the UI password and the master passphrase.
- **Forms (#103):** the broker form comes back with what was typed (never a secret) and the error at its field (`aria-invalid`, `aria-describedby`), status 422, nothing saved. Flash messages have `role="alert"`/`"status"`. The start date can't be set in the future (`max`).
- **Long actions in the background (#104):** *Neu aufsetzen*, *Logos jetzt holen* and the account check of *Prüfung* run as background jobs; *Prüfung* shows the last check with its time and *Neu prüfen*. The overview shows which broker a run is at, its step, and when it waits for an approval in the app; it refreshes every 10 s instead of 3 s and can be paused. The vault is decrypted only when its file changed; Wealthfolio's accounts and the timer's next run are kept for a minute.
- **Neu aufsetzen in two steps (#110):** a page shows how many bookings would be deleted; deleting needs the broker's name typed in. A setup checklist on the overview replaces the old "step by step" message.
- **Tables (#105):** caption, `thead`/`tbody`, `scope="col"`; checkbox labels name their booking.
- **Transfer patterns (#84):** *Überträge* is listed under *Einstellungen* again while patterns are saved, so every pattern that applies can be seen and deleted. *Duplikate* stays hidden.
- **Config under its lock (#117):** every change reads, changes and writes the config under one lock; two changes at once both stay.
- **One date parser (#119):** `model.when` for every moment from Wealthfolio or the state; a date without time zone or without time no longer breaks a broker's run.
- **Passphrase change and UI writes (#120):** writes to the sync database wait while it is re-encrypted; one that waited past the switch is refused, never lost silently.
- **Docs (#111):** README: checklist, *Überträge*, *Neu aufsetzen*, sessions, known limitations; no "self-signed" left (catalog entry, CLI help), screenshots taken anew.

## 0.20.0

Lock-out safety, concurrency and robustness, from a full audit of 15.10.2026 and the backlog (waldonso2/wealthfolio-importer-addon#98, #99, #101, #105, #108, #110, #113, #115 and the audit list):

- **Bank lock-out (comdirect, FinTS):**
  - A wrong TAN in the web UI counts at once. Until now the count was lost when the login was started again.
  - The web login and the daily run can no longer overwrite each other's TAN counters.
  - A scheduled comdirect run asks for a TAN only when the web login found a push TAN, never for an unknown type.
  - Opening the login page asks the broker nothing; *Anmeldung starten* does. An open login is shown again instead of replaced, and closes after 15 minutes.
  - A PIN the bank rejects while reading depot transactions now stops further attempts like everywhere else.
- **Bookings:**
  - comdirect: a trade whose giro booking never comes is listed as unknown while it is still fetched. Until now it could silently drop out after a week.
  - comdirect: a reversed dividend is listed as unknown.
  - Deutsche Bank: a CSV the runs can't read is refused at the upload. Until now it broke every later run of the broker. German dates (`15.03.2024`) are read too.
  - The start date begins at midnight German time, not UTC.
  - Trade Republic amounts given as text keep their digits.
- **Safety and concurrency:**
  - After `brokersync-reset-password` a cookie of the old password signs in nobody, also while the web UI keeps running.
  - The web server keeps no access log, because a link can carry the setup code.
  - Switching to *Sicher* during a run also ends that run's login.
  - Each request thread has its own database connection.
  - Files are written through a temp file of their own; settings are saved under the same lock as the re-encryption.
  - The re-encryption makes its staged files durable before the journal.
  - Two quick clicks on *Jetzt abrufen* start one run.
  - A broker form is saved only when all of it is valid.
  - The Wealthfolio password is length-checked.
- **Notifications (#98):** a new ntfy topic has 128 bits and is saved at once, so it stays the same while you subscribe. A saved token can be removed. Details on ntfy.sh need an explicit confirmation.
- **Look (#105, #115, #110):**
  - Colours meet WCAG AA in light and dark (warn, OK, muted, the grey light, error in dark).
  - In English, dates are `2026-03-15` and amounts `1,234.56`.
  - *Passphrase entfernen* needs a confirmation.
- **Clean-up:** unused code removed (`get_accounts` of every adapter, `BrokerAccount`, `crypto.write_keyring`); `state.rekey` can no longer re-encrypt `state.db` in place.

## 0.19.0

Supply chain and hardening (waldonso2/wealthfolio-importer-addon#88, #97, #107), from the audits of 13.10.2026:

- **Releases only after the whole CI, checked on install and update** (#88):
  - The release workflow runs every CI job on the same commit first; only its publishing step may write.
  - Each release carries the source archive, `SHA256SUMS` of every file and GitHub's build provenance (`gh attestation verify`). Published releases can't be changed any more.
  - Install and `update` check every downloaded file against the release's `SHA256SUMS`. A changed or an added file stops them, and the previous version keeps running.
  - `VERSION=0.19.0 update` installs that version, also an older one. Without it, `update` never goes back to an older version.
- **CI** (#97):
  - The test tools are locked with hashes like the runtime.
  - `pip-audit` checks the locked versions for known vulnerabilities on every change and weekly; pull requests get a dependency review.
  - Dependabot runs weekly, and also offers security updates of pytr.
  - Test coverage may not fall below 90 %.
- **Hardening** (#107):
  - The web UI answers only to its IP addresses and known names: the container's name, also with `.fritz.box`, `.lan` or `.local`, the certificate's names, the address of the notifications, and `BROKERSYNC_ALLOWED_HOSTS`. Port 8090 redirects only there. This blocks DNS rebinding and open redirects.
  - Pages allow no inline styles any more (CSP without `unsafe-inline`). `/healthz` shows the version only after login.
  - comdirect: the token goes only to comdirect's own API, never to a status link on another host.
  - logo.dev: answers are read only up to the size of a logo, redirects only within logo.dev.
  - The services may only use the system calls of an ordinary service (`systemd-analyze security` 3.0 → 1.5); the daily run stops after 45 minutes at the latest.

## 0.18.0

Access and data safety (waldonso2/wealthfolio-importer-addon#89, #90, #91, #106, #114), from the audits of 13.10.2026:

- **First password only with a setup code** (#89):
  - Until now anyone in the home network, and even any web page open in a browser there, could choose the UI password of a fresh installation before you.
  - The page now needs a one-time code shown only on the container's console: at the end of the installation (as a ready link), with `brokersync-cli setup-code`, and after `brokersync-reset-password`. It is valid 24 h and once; only its hash is stored, it is never logged.
  - The form carries a CSRF token like every other one.
  - *Passwort vergessen?* moved to the login page.
- **Login brake** (#90):
  - Every password check (UI password, master passphrase, setup code) is counted before the check and at most two run at once. Many parallel wrong passwords can no longer exhaust the container's memory or slip past the brake.
  - Also braked: 30 wrong attempts from all addresses together, IPv6 neighbours (same /64) count as one.
  - Behind a reverse proxy: `BROKERSYNC_TRUST_PROXY=<its address>` makes the brake count per user instead of locking everyone out.
- **Re-encryption that survives a crash** (#91):
  - Setting, changing or removing the master passphrase and the key change of an update write everything next to the old files first and then switch in one step; the database is never re-encrypted in place.
  - After a power cut in between, the next start brings the data to the old or the new state, never a mix (`brokersync-cli recover` does the same by hand).
- **Errors as pages** (#106): an expired form, an unknown page or an unexpected error show a page of the UI instead of JSON or "Internal Server Error"; an unexpected error shows only an id that is also in the log. Numbers like `Infinity` or `NaN` in a form, an unreadable CSV and odd amounts give a message instead of an error.
- **Log redaction** (#114): every new broker session replaces the old one's strings instead of piling up for as long as the service runs.

## 0.17.0

English, a light mode and a tidier overview (waldonso2/wealthfolio-importer-addon#86, waldonso2/wealthfolio-importer-addon#87):

- **Einstellungen → Darstellung** (new):
  - **Sprache**: Deutsch or English. Notifications, bank texts and the comments in Wealthfolio stay as they are.
  - **Farbschema**: like the device, always light or always dark.
- **Overview**: per broker only two buttons, *Jetzt abrufen* and a gear for its settings. The reasons below the traffic light are links: "Anmeldung nötig" leads to the login, unknown bookings to *Unbekannt*, deviations to *Prüfung*.
- **Unbekannte Buchungen**: each one can be reported as a GitHub issue with one button. The issue is prefilled with type, date, version and the masked data (no IBANs or account numbers); you see and can edit everything before sending it. The raw data is folded.
- **Prüfung**: an opening position is no longer shown once its buy has been booked. The note about German time is gone.
- **Wertpapiere**: saved mappings take one line each.

## 0.16.0

Abrufmodus "Sicher" and PINs that are never stored (waldonso2/wealthfolio-importer-addon#85):

- **Einstellungen → Sicherheit → Abrufmodus** - for all brokers:
  - **Automatisch**: as before; logins stay saved, the service fetches every morning.
  - **Sicher**: every fetch starts with your login (app confirmation, TAN or device code). Right after it the service logs out and deletes the session, whatever happened, so no saved login can fetch without you.
    - The daily run fetches nothing and only reminds you via ntfy.
    - Switching to *Sicher* ends all saved logins at once.
    - The lock-out guards (rejected PIN, unanswered or wrong TANs) are kept.
- **Jetzt abrufen** leads to the login first and fetches right after it.
- **Alle abrufen** leads through the logins one after the other ("Anmeldung 1 von 3", each broker can be skipped) and then fetches all together.
- **DKB and Deutsche Bank: PIN not stored.** Leave the PIN field empty and the login asks for it every time.
  - It stays in memory only until the fetch is done, never in the vault, the database or the log.
  - A saved PIN can be dropped with the new tick box below the field.

## 0.15.1

Tidier settings and overview (waldonso2/wealthfolio-importer-addon#84):

- **Überträge and Duplikate are hidden for now.** Both pages are gone from *Einstellungen* and their addresses no longer answer. Transfer patterns you saved before still apply to new bookings.
- **The overview shows no amounts**: per broker only whether the balance matches Wealthfolio ("Saldo stimmt" / "Saldo weicht ab"). The details of a deviation can still be opened.
- The broker list no longer says "Scalable Capital folgt".

## 0.15.0

A new look for the web UI, made for phones (waldonso2/wealthfolio-importer-addon#83):

- **Looks like Wealthfolio**: the same colours (light and dark, following the device), fonts and cards. On a phone the menu is a bar at the bottom, in reach of your thumb.
- **Overview with a traffic light per broker**: green - nothing to do; yellow - something to look at (deviations, unknown or missing bookings, no fetch for two days); red - the last fetch failed or a login is needed; grey - never fetched. Below it, only the reasons; the error message and the deviation table can be opened. The setup checklist is gone; while Wealthfolio isn't connected, a single card says so.
- **Last runs**: the last three, older ones folded away.
- **Prüfung**: everything about a broker in one card - missing bookings, positions without a buy and the account check - with a one-line summary on top. The explanations moved into "Was wird geprüft?".
- **Sicherheit**: master passphrase and certificate first; the checks as a summary ("7 von 9 in Ordnung") with only the open points shown, the rest folded. The import steps for the certificate are folded per device.

## 0.14.0

Internal restructuring for safer changes later (waldonso2/wealthfolio-importer-addon#75, #76, #77). Nothing changes in what the service books or shows.

- **The database knows its version.** `state.db` now records a schema version and brings itself up to date when the service starts, step by step and in one transaction. The first step stores a savings account's balance in its own column (it was hidden in the currency field). A database written by a newer version is not opened, so an older version can't damage it.
- **Clean-up per broker is exact.** Every stored flag and value is listed in one place, with whether it belongs to a broker. *Neu aufsetzen* and removing a broker delete exactly that broker's entries; before, a pattern could also catch e.g. the logo status of an asset whose id ended in the broker's name.
- **A broker's run in named steps.** Plan the window, fetch, prepare, repair, book, compare, add tax, learn assets, check holdings, finish - in the order the documentation describes, instead of one long function.
- **The web UI in modules.** Each area (setup, overview, brokers, settings, check, duplicates, security) has its own router; the pages and addresses are the same.

## 0.13.1

Readable dates and unknown bookings (waldonso2/wealthfolio-importer-addon#73):

- **One date format everywhere, in German time.** Days read `03.03.2026`, moments `03.03.2026 11:00` on every page. *Prüfung* showed Wealthfolio's activity times in UTC (one or two hours off); it now shows German time like the rest, to the second, with UTC in the tooltip. *Unbekannte Buchungen* and *Duplikate* showed ISO dates.
- **Unknown bookings without JSON.** *Unbekannte Buchungen* lists amount, security (with share count) and text in their own columns, so you can enter a booking by hand. The raw data is still there, folded, for a bug report.

## 0.13.0

Clearer web UI and stricter checks (waldonso2/wealthfolio-importer-addon#69, #70, #71, #72):

- **Shorter menu.** The header now holds four entries: Übersicht, Prüfung, Unbekannt (with the number of open entries) and Einstellungen. *Einstellungen* lists Wealthfolio, Broker, Benachrichtigungen, Wertpapiere, Überträge, Logos, Sicherheit and Duplikate with a short note each; on those pages a bar below the header switches between them. The current page is marked.
- **Tables readable on phones.** On narrow screens every table row (runs, deviations, unknown bookings, the checks on *Prüfung*, duplicates, securities, transfers, security checks) is shown as a block with the column name above each value, instead of a table you have to scroll sideways.
- **Shared rules for recognising duplicates.** The cases that decide whether a booking already exists in Wealthfolio are now kept in one file the service and the import addon both test against, so both recognise the same bookings in both directions. Nothing changes in how the service matches.
- Stricter automatic checks of the code (type checks, a complexity limit, test coverage). Bookings with a missing date or share count are now reported as errors instead of failing later.

## 0.12.2

Security hardening of the web UI (#33):

- **Every form field is checked on the server**, not only in the browser: addresses (Wealthfolio, ntfy, this UI) must be http:// or https:// without credentials in them, the start date a real date not in the future, ISINs and IBANs need a valid check digit, exchanges a 4-character MIC, symbols only the usual characters, and free text is limited in length. A wrong entry is reported on the page and nothing is saved - before, e.g. a start date like "01.01.2024" was stored and made every later run fail.
- **Oversized requests are refused** (over 6 MB) before anything is read - also on the login page, which answers without a session.
- Deleting a transfer pattern with a broken form no longer ends in an error page; a new session is started when the UI password is first set.
- `SECURITY.md` lists the security libraries and measures the service uses, and why some common ones aren't needed.

## 0.12.1

Security hardening from the audit (waldonso2/wealthfolio-importer-addon#55):

- **Dependencies only at locked, hash-checked versions.** Install and update used to fetch the newest version of every dependency from PyPI; a tampered or broken release would have landed in the container unseen, next to the bank credentials. Now `requirements.txt` holds each one at an exact version with its hashes (for every supported Python and platform), `setup.sh` installs only those, only as ready-built packages, and the CI tests exactly them. Dependabot proposes updates.
- **The install engine is pinned.** The install and `update` load community-scripts' engine, which runs as root on the Proxmox host, from a reviewed commit instead of its moving main branch; a weekly check reports newer ones.
- **Error texts are masked on the pages too.** A bank's or broker's error message shown on the overview (or Wealthfolio's on the other pages) no longer shows IBANs, account numbers or stored secrets - as in the log and the notifications. The log no longer lists request addresses.
- Backups are created readable for root only from the first byte.
- A secret just saved is masked in the log at once, not only after the next read.
- Repository: secret scan of the whole history in the CI, `.gitignore` for keys and data, GitHub Actions pinned to commits, `SECURITY.md` for private reports.

## 0.12.0

- **Logos for securities (optional, page *Logos*).** Wealthfolio's own logos are named by ticker and exchange, so securities booked under their ISIN show none. Turned on and with a (free) logo.dev key, the service asks logo.dev for each security by ISIN after every run and sets the logo as the security's custom logo in Wealthfolio (version 3.8.0 or later) - stored in Wealthfolio's database, so it survives updates and is part of its backups. Only ISINs go to logo.dev. A logo you set yourself is never replaced; one you remove isn't set again; a security logo.dev has no logo for is asked again after 30 days. At most 50 questions per run.

## 0.11.5

- **Fix: an opening position entered from the old parser's ISIN still stayed unmatched.** Its Wealthfolio asset counted as taken by that spelling (`<ISIN>|/DE/<WKN>`) - which is the same ISIN. It isn't taken from the real ISIN any more.
- **The log masked ISINs** of US securities ("US***"): long digit runs after letters (an ISIN) are no longer taken for account numbers; account, depot and reference numbers still are.
- ISINs assigned on the page *Wertpapiere* are no longer looked up in imported trades every run (the holdings check finds them by symbol).

## 0.11.4

- **Fix: an opening position entered from holdings read before 0.9.9 wasn't found by the holdings check.** python-fints' parser, used until then, read a security split over two lines as ISIN `<ISIN>|/DE/<WKN>` and kept only the name's last line (Deutsche Bank: "IEN O.N."). An opening entered from it was booked under that ISIN. Its transaction now carries the real ISIN (its id stays, so nothing is booked again), and the check finds the position.
- **The log says why a position's Wealthfolio asset couldn't be found**: no purchase or sale whose transfer names the ISIN, its asset already belonging to another ISIN, transfers naming several ISINs, or statements naming several assets - asset ids and ISINs only.
- An ISIN whose statements name several assets is now also tried by time and amount.

## 0.11.3

- **Fix: two ETFs of a savings plan were mixed up.** A PDF import ends a trade and its transfers with the same statement reference (`[PDF …]`); that now ties them together exactly, before time and amount are tried. It also corrects an asset an earlier version learned wrongly: positions that deviated in the last check are looked at again, and what a statement says replaces the stored asset.
- **Fix: a position booked by the service itself (an opening position) stayed unmatched** when another asset was stored for its ISIN. For a deviating position, the trade whose transfer names the ISIN with the same amount and time - the service's own as well as an import's - now replaces the stored asset.
- The log names the stored Wealthfolio asset of every deviating position, to see at once why it doesn't fit.

## 0.11.2

- **Fix: positions of a savings plan stayed unmatched.** The executions of a savings plan are booked in the same second, so the transfers next to a trade named several ISINs and 0.11.1 learned nothing for them. A trade's transfer is now also told apart by its amount, and what is learned narrows the rest in the same run.
- The log names imported trades before the bank history only when new ones get their money (not the same count every run).

## 0.11.1

- **Fix: positions bought only by an import were reported as missing.** The holdings check finds a position's Wealthfolio asset through the activities the service booked or matched. Trades a PDF/CSV import booked on its own (e.g. all DKB statements from before the giro history) were unknown, so every position showed up twice: at the bank, and under the import's ticker in Wealthfolio. Now, for a position whose asset is still unknown, the service looks for imported purchases and sales whose cash transfer names the ISIN ("Funds for <ISIN> …", "<ISIN> (…) sale"), and learns their asset - only when the ISIN is on one asset only, and only for a valid ISIN (check digit).

## 0.11.0

- **Imported trades older than the bank history get their money (FinTS banks, e.g. DKB).** A purchase a CSV/PDF import booked before the first day of the giro history the service read was paid from money the service never saw: the cash account went negative. Now every run adds, for each such import:
  - a purchase: a deposit of its cost just before its transfer to the depot,
  - a sale: a withdrawal of its proceeds just after,
  - a dividend: nothing - its money stays on the cash account.
  
  Trades in the read history aren't touched (their giro booking was read and matched), nor the service's own activities. The first day read is now recorded for every FinTS bank (`history-start`); where it wasn't yet, the first successful run tells (its start date, else 89 days before it).
- **FinTS depot transactions (diagnosis):** asked for the last 89 days (as far back as banks answer without a TAN), not just the run's window.

## 0.10.2

- **FinTS depot transactions, first step (diagnosis, nothing booked).** Banks that offer depot transactions over FinTS (DKB) are asked for them (HKWDU, answered in MT536 - python-fints knows neither, the service defines both). The log names, per depot, how many came and what they would be booked as (purchase, sale, delivery, corporate action, reversal), how complete they are, and the answer's layout - counts, field names and standard codes only, no ISINs, amounts or numbers. Booking follows once a real answer has been checked.
  - Not asked when the bank wants a TAN for them; a failure never stops the run.

## 0.10.1

- **Fix (FinTS depot holdings): "'NoneType' object is not subscriptable".** python-fints takes the bank's country from the account's BIC when it asks for the holdings. A depot listed only in the user parameters (DKB) has no BIC, so the request failed before it was sent. It now gets the BIC of the bank's other accounts (else one naming Germany).

## 0.10.0

- **Tagesgeld as a Wealthfolio account of its own (FinTS banks, e.g. DKB).** In the broker settings, an optional third Wealthfolio account can be assigned to the bank's savings account. The service then also reads its bookings and balance:
  - Interest, deposits and withdrawals of the Tagesgeld are booked on that account; its balance is compared with it after each run.
  - A transfer between the giro account and the Tagesgeld is booked once, as a transfer pair (TRANSFER_OUT/TRANSFER_IN with one `sourceGroupId`), not as a withdrawal plus a deposit. Transfers the giro side booked before the Tagesgeld was assigned get their Tagesgeld side on their own.
  - The first run with the Tagesgeld reads it from the start date (without one: the last 89 days); so does a run after another account was chosen.
  - The Tagesgeld is found by its name or account type; where that fails, its IBAN can be entered with the credentials.
- The giro account is no longer taken from an account named like a savings account, even when the bank reports both with the same account type (DKB).

## 0.9.9

- **FinTS depot holdings: own MT535 parser and a readable log.** python-fints' parser fails on a holding without a total value and only knows share counts in one notation, and the service logged a failed holdings request at debug level only. So a depot could come back empty without a word (DKB in 0.9.8). Now:
  - Holdings are read by the service's own, more tolerant parser (shares or nominal, value from price × shares when the bank sends no total).
  - Only accounts the bank offers holdings for are asked (not the giro or savings account).
  - The log says per depot how many positions came, why none could be read, and for an empty answer its layout (field names only, no values or numbers).

## 0.9.8

- **DKB: depot holdings.** The DKB delivers the depot's holdings over FinTS (the depot has no IBAN and is only listed in the user parameters). After each run they are compared with the DKB depot account in Wealthfolio, and a position missing there can be entered on *Prüfung* as an opening position. Depot transactions still come from a PDF import of the statements.
- **Unknown events keep no account or depot numbers.** IBANs and long numbers (account, depot, reference) in their texts are masked before they are stored, for every broker. Events stored before are masked at their next update; an unmatched securities booking is updated by every run. ISINs, dates and amounts stay.

## 0.9.7

- **Fix: dividends stay on the cash account.** 0.9.4 followed a dividend whose giro booking wasn't read (a Deutsche Bank CSV row older than the giro history) with a withdrawal of the same amount, so the money left the cash account right away. A dividend now always stays there, `external_cash` or not; purchases and sales are unchanged.
- Withdrawals booked that way before stay in Wealthfolio. Remove them by hand (comment "… paid out [SYNC deutschebank:…]"), or use *Neu aufsetzen* for the broker: it books everything again, uploaded CSV rows and opening positions are kept.

## 0.9.6

- **Fix (FinTS camt): "Abrufzeitraum außerhalb des unterstützten Bereichs".** A bank states in its parameters how many days of camt bookings it keeps (Deutsche Bank). The service now asks only that far back, and the log says so ("die Bank hält camt-Umsätze N Tage vor"). Older giro bookings aren't available over FinTS at all.

## 0.9.5

- **Fix (Deutsche Bank): the camt request was refused.**
  - The bank answered "9210 Eindeutige Identifikation des Kontos nicht möglich - Angaben zur nationalen Kontoverbindung erforderlich".
  - The reason: python-fints names the account by IBAN and BIC only in camt requests.
  - The service now adds account number, sub-account and bank code, which FinTS provides for in the same field.
- **Readable bank errors (FinTS).** Instead of the whole technical answer, the overview and the notification say "Die Deutsche Bank meldet: 9210 …" with the bank's codes and texts.

## 0.9.4

- **Fix (FinTS, Deutsche Bank): without an IBAN the wrong account was read.** The service took the first account with an IBAN, at the Deutsche Bank the savings account, so the giro account's bookings never came. It now picks the giro account by the account type the bank reports, and the log says which one (`Umsätze und Saldo von „…“`). An IBAN entered in the settings still decides.
- **Deutsche Bank: giro bookings as camt.** The bank answers the MT940 request with nothing. When a bank does that and offers camt, the service reads camt instead; the log says how many bookings came which way.
- **Deutsche Bank: depot transactions from the CSV export.** FinTS doesn't deliver them, so the export of the depot's transactions can be uploaded in the broker settings, again and again, overlapping without duplicates; depot and account numbers aren't stored. What gets booked:
  - dividends with their taxes;
  - buys and sales, paid via the cash account; the giro bookings that settle them are matched in the same run;
  - deliveries against payment (*Eingang*, e.g. employee shares) as a buy whose money came from outside;
  - rows older than the giro history the service read, without money on the cash account;
  - corporate actions and depot transfers are listed under *Unbekannte Buchungen*.
- A dividend whose money went outside the broker's two accounts gets a withdrawal after its transfer (as a sale already did).
- Page *Sicherheit*: the import instructions warn against Windows' automatic certificate store, and say to restart the browser after the import.

## 0.9.3

- **Trusted HTTPS instead of a browser warning.** The container now has a small certificate authority of its own. It signs the web UI's certificate, and the next update replaces the self-signed one from 0.9.0. Import the CA certificate once (page *Sicherheit* → *CA-Zertifikat herunterladen*; instructions for Windows, macOS, Firefox, iPhone and Android are on the page). After that the browser shows the UI as secure, also after every renewal. The CA may only vouch for local names (private IP ranges, the container's name, `.local`, `.lan`, `.home.arpa`, `.fritz.box`): even with its key, nobody could fake a public site with it. Its key stays root-only and is never handed to the service. The certificate leaves out public IPv6 addresses.

## 0.9.2

- **Fix:** `brokersync-cli security-status` reported the web UI as plain HTTP although it runs on HTTPS; the command now knows the service's setting.

## 0.9.1

- **A fresh key, once.** 0.9.0 moved the existing key from `data/secret.key` to `/etc/wealthfolio-broker-sync` instead of replacing it. Backups from before the update, or Proxmox backups, that held that key could still open the current data. The update now creates a new key once and re-encrypts everything for it; copies of the old key open nothing any more. With a master passphrase the service has to be unlocked before the update, otherwise the next update does it.
- **Fix: older backups holding the key were not replaced.** The check never matched (`tar | grep -q` under `pipefail`). They are now deleted and replaced by one backup of the encrypted data.
- **The update no longer hangs.** Steps that go through systemd (`systemd-creds`, the migration) have time limits. Running `deploy/setup.sh` by hand restarts a running service with the new files.

## 0.9.0

- **Everything the service stores is encrypted, and the key is no longer next to the data** (waldonso2/wealthfolio-importer-addon#53).
  - Credentials, sessions and tokens: AES-256-GCM instead of Fernet (`secrets.enc`).
  - Settings, which include your own IBANs in the transfer patterns: `config.enc` instead of a plain `config.json`.
  - ntfy settings, including the topic, which is a secret on ntfy.sh: `notify.enc`.
  - Sync database: SQLCipher (AES-256).
  - The key moves from `data/secret.key` to `/etc/wealthfolio-broker-sync/key.cred`, encrypted with `systemd-creds`; the services get it from systemd at start. A copy of `data/`, or one of the update backups, is useless without it.
  - The update migrates everything once and replaces older backups that still held the key.
- **Optional master passphrase** (new page *Sicherheit*): the data key is then derived from the service key and your passphrase (argon2id). Not even a full copy of the container opens the data without the passphrase. After a restart the service has to be unlocked in the web UI; until then it fetches nothing and says so via ntfy. Without the passphrase the data is lost.
- **HTTPS for the web UI:**
  - The UI now runs on `https://<ip>:8443` with a self-signed certificate that is created and renewed automatically. A certificate of your own in `/etc/wealthfolio-broker-sync/tls/` is kept.
  - `http://<ip>:8090` redirects there, and links in notifications are switched to the new address.
  - Cookies are `Secure`, with `SameSite=strict` and a 24 h lifetime; strict security headers (CSP without scripts, no framing, HSTS).
- **Web UI password:**
  - It is now hashed with argon2id instead of scrypt; existing passwords are upgraded at the next login.
  - New passwords need at least 12 characters.
  - After five wrong attempts, logins from that client pause for 15 minutes.
  - `brokersync-reset-password` also signs everyone out.
- **Nothing sensitive in the log or in notifications:**
  - Credentials, tokens, IBANs and long account numbers are masked in the log and in messages on screen.
  - ntfy messages carry only the title and a link unless *Details* is switched on under *Benachrichtigungen*.
- **Hardened services:** no capabilities, private `/dev` and `/tmp`, kernel and namespace restrictions, and only `data/` writable (mode 0700, files 0600).
- New commands:
  - `brokersync-cli <command>` runs a command as the service user with the key, e.g. `brokersync-cli security-status`.
  - `brokersync make-cert` and `brokersync migrate` are used by the installation and the update.

## 0.8.0

- **Deutsche Bank** (FinTS): the giro account like DKB's (deposits, withdrawals, transfers, interest, fees; securities bookings are only checked against an import of the statements) and the **holdings of the maxblue depot**, compared with the depot account after each run - positions without a buy in Wealthfolio can be entered as opening positions on *Prüfung*. Login with the Deutsche Bank ID (or branch and account number), PIN and the bank code of the branch; confirmation with BestSign in the app. New and not yet tried against the bank: at login the log lists what it offers over FinTS (transactions, camt, holdings, depot transactions; per account its product name and type, no numbers) - that decides how depot trades are booked next.
- FinTS (all banks): a depot listed among the accounts no longer gets picked as the giro account when no IBAN is set; holdings are also asked for depots that have no IBAN.

## 0.7.4

- **Docs and UI texts describe the service on its own.** README, ARCHITECTURE and the pages of the web UI no longer point to another tool; CSV and PDF imports are mentioned only as activities the sync recognises and doesn't book twice. The message about an unmatched securities booking now says to import the securities statement (PDF) in Wealthfolio. Bookings are unchanged.

## 0.7.3

- **Tax for dividends from the CSV import.** Scalable's CSV export has no tax on dividends, so dividends the addon imported had none. When the sync finds such a dividend for a broker's payout that carries a tax, it puts the tax into the imported activity - only the tax field; amount and comment stay, and tax is not part of Wealthfolio's duplicate fingerprint, so a later re-import still recognises it. Dividends matched before this version get it once: the first run after the update fetches the details of those transactions again (for Scalable a few minutes, paced for its rate limit). The log says how many dividends got their tax.

## 0.7.2

- **Fix: comdirect returned only half a year without a start date.** Unasked, comdirect's API delivers about six months of giro bookings (and possibly depot transactions); older ones only when a date is given. Without a start date the service now asks for ten years back, giro and depot alike; if comdirect refuses the date, it asks without.

## 0.7.1

- **Fix: "Neu aufsetzen" wasn't visible** in the broker settings - it had landed in the page title.
- **comdirect without a start date fetches everything** comdirect's API delivers (before: one year). The log shows how far back that goes (oldest giro booking and depot transaction), without amounts.

## 0.7.0

- **comdirect: only the depot is booked, no longer the giro account.** The comdirect cash account in Wealthfolio now receives only the dividends. A buy is booked with a deposit of its cost on the cash account right before it, a sale with a withdrawal of its proceeds right after it - so the account doesn't run into the minus because the giro history before the start date is missing. Deposits, card payments, fees and interest of the giro account are no longer booked, and its balance is no longer compared. After the update click **Neu aufsetzen** in the comdirect settings once: it deletes what 0.6.0 booked and the next run books everything again with the new rules.
- **Opening positions:** positions the broker holds that Wealthfolio has no buy for (bought before the start date, or transferred in) are listed on the page *Prüfung*. Enter the purchase date, price per unit (pre-filled with the broker's purchase price, if it reports one) and fees: the next run books a buy on that day, preceded by a deposit of its cost.
- **Neu aufsetzen** (broker settings): deletes every activity the service booked for a broker (`[SYNC …]` in its two accounts; imports and manual entries stay), forgets its sync state and fetches everything from the start date again. Confirmation required.

## 0.6.0

- **New broker: comdirect**, through comdirect's official REST API for private customers (giro account and depot). Login with Zugangsnummer, PIN and the API's client ID and secret, confirmed with photoTAN-Push in the app (or a photoTAN graphic / mobileTAN typed in the web UI); a scheduled run asks via ntfy and waits. Balance, giro bookings, depot positions and depot transactions are read; buys and sales are booked with the giro booking that settled them (its amount is the trade's cash, the rest of it the costs - the API doesn't split costs of a sale into fee and tax), dividends when the posting text names the security. Depot transfers and trades whose settlement doesn't turn up are reported.
- **Lock-out guard for comdirect:** comdirect locks the online banking after five TAN requests without a correct TAN and after three wrong TANs. The service stops after three unanswered requests or two wrong TANs and asks the user to log in once on comdirect.de and save the credentials again.
- The login page can show a picture with the challenge (comdirect's photoTAN graphic).

## 0.5.1

- **Prüfung: fewer false alarms.** Two moments on the securities account that cancel each other within 36 hours now count as one and are no longer listed - e.g. a dividend from the CSV import and its transfer to cash from a PDF import or entered by hand at another time of day. A stock dividend or staking reward (subtype `DIVIDEND_IN_KIND`, `STAKING_REWARD`) brings shares, not money, and no longer counts as cash on the securities account.

## 0.5.0

- **Wealthfolio is checked after every run, not trusted from the sync's own records.** Until now a transaction counted as done once the sync had booked it; deleting it in Wealthfolio meant it never came back. Now every run compares the transactions synced before with Wealthfolio (by their `[SYNC …]` reference and the stored activity ids) and lists on the page *Prüfung*, per broker: transactions that are missing in Wealthfolio, completely or partly, with **Wieder anlegen** (the next run books them, unless an import has them by then) and **Ignorieren** (they stay away; *Doch anlegen* undoes it). A deletion may have been on purpose, so nothing is re-created without that click. New deviations are notified once.
- **Scalable reads its whole history on every run** (from the start date): a few paged queries; the per-transaction details are still fetched only once. So the comparison also finds activities of the sync that Scalable no longer lists - e.g. a distribution Scalable cancelled after it was booked. They are listed for deletion in Wealthfolio, never deleted by the sync. *Ab Startdatum neu abrufen* is no longer needed for Scalable and hidden there.
- Scalable: a cancelled distribution and its original now always cancel out; the cancellation is no longer an unknown event, and its old entry disappears from *Unbekannte Buchungen*.

## 0.4.5

- **New page "Prüfung"** (read only): for each broker it shows where the two-account model doesn't add up - the moments on the securities account where cash is left behind (e.g. a buy without its funding transfer, or a transfer whose sale or payout was deleted), and transfers between the two accounts without their other leg. Each line has date, type, amount, comment and whether the sync or an import booked it. Nothing is changed; fixes are made in Wealthfolio.

## 0.4.4

- **Fix: a certificate redemption was booked as a dividend next to the CSV import's sale.** Scalable books a redemption or knock-out as the securities leaving the depot with value 0 plus a distribution of the same ISIN on the same day; the addon books both as one sale. The sync now does the same, so it recognises the CSV sale instead of adding a dividend.
- **Fund swaps** (securities `SWAP_OUT` plus the cash `SWAP_OUT` of the same ISIN and day) are booked as a sale like the addon, instead of being reported as unknown.
- **A cancelled distribution** and its original cancel out, like in the addon. If the sync had booked the original already, the cancellation is still reported, so the original can be removed by hand.
- Activities booked before this version are not changed: a dividend the sync added for a redemption has to be deleted by hand.

## 0.4.3

- **Fix: a full Scalable fetch stopped with "RATE_LIMITED".** Scalable limits how many transaction details may be queried in a short time. The adapter now pauses a second between detail queries, waits and retries when Scalable reports the limit (30 s, 60 s, 120 s, or as long as Scalable says), and only queries details of transactions the sync doesn't have yet. If the limit lasts, the run books everything it has the details for and reports the remaining trades ("Details folgen beim nächsten Abruf"); the next run continues with those.
- The journal now carries the reason of a failed run, not only its status.

## 0.4.2

- **Fix: Scalable brought no transactions and no cash balance.** The CLI's broker commands wrap their answer in `result` (next to the account and portfolio they used); the adapter read one level too high and found nothing. It now reads the answer like `sc` itself does. After the update, click *Ab Startdatum neu abrufen* in the Scalable settings, so the next run fetches the history the earlier runs missed.

## 0.4.1

- **"Ab Startdatum neu abrufen"** in the broker settings: the next run fetches everything from the start date again, once. Changing the start date alone only affects a broker's first run; afterwards each run starts at the last successful one (minus 7 days). Transactions already synced are not created again.
- Each run now logs how many transactions the broker returned since when, and how many were new.
- Scalable: the log shows what `sc` returned - transactions by type, status and subtype - and, when the cash balance is missing, the structure of the answer instead (field names and types, no amounts, ids or names). This helps to find out why a run brings nothing.

## 0.4.0

- **New broker: Scalable Capital**, through Scalable's official command line [Scalable CLI](https://github.com/ScalableCapital/scalable-cli) (`sc`) instead of the web app's internal API. Enable it once in Scalable's web platform under *Profil → Sicherheit → Agentic Investing*; log in from the service's page with a link and a code confirmed in the browser. The CLI runs in read-only mode and only read commands are used.
- Trades, savings plans, distributions, interest, taxes, fees, deposits and withdrawals are booked like the addon's Scalable CSV import, so CSV-imported activities are recognised. A depot migration that moves positions out and back in is skipped; cancellations, single security transfers, fund swaps and ELTIFs are reported as unknown. Positions and cash are checked against Wealthfolio after each run.
- The CLI's session is kept encrypted in the vault and renews itself; when Scalable asks for a new login, the daily run sends the link by ntfy and waits ten minutes for the confirmation.
- Install and update fetch `sc` from Scalable's GitHub release (`brokersync install-sc`) and install it only when Scalable's minisign signature and the checksum are valid. If that fails, everything except Scalable keeps working.
- A dividend without a share count is booked with quantity 1, like the addon does for Scalable.

## 0.3.8

- Internal: the FinTS part of the DKB adapter is now a shared `FintsAdapter`, so further FinTS banks (Deutsche Bank, comdirect as a fallback) only need a short profile ([importer-addon#44](https://github.com/waldonso2/wealthfolio-importer-addon/issues/44)). DKB behaves exactly as before: same transaction ids, same messages, same settings.
- A bank whose bank code differs per branch gets a *Bankleitzahl* field; a TAN to type in (instead of a confirmation in the app) is asked for in the web UI.

## 0.3.7

- **The test broker "Dummy" is gone.** With Trade Republic and DKB there are real brokers to try the service with, and the dummy did harm: set up on the same Wealthfolio accounts as a real broker, it booked its test transactions there (a fake position, cash too high) and left permanent "unknown" events.
- On the first start after the update, its settings, saved login, sync state, runs, unknown events and holdings check are removed; the other brokers are untouched. What it booked in Wealthfolio stays - the overview says once how many activities that are and how to find them (comment `[SYNC dummy:`), so you can delete them yourself.
- The tests use a test-only broker under `tests/` instead, which isn't installed.
- README screenshots show Trade Republic instead of the dummy.

## 0.3.6

- **Fix: the transfers between securities and cash account moved no money.** Every cash activity was created with the asset `$CASH-<ccy>`, and Wealthfolio books a TRANSFER_IN/TRANSFER_OUT with an asset as a *securities* transfer of that asset. So a buy was paid from the securities account's own cash, sale proceeds and dividends stayed there, the cash account was short by the same amount and showed a position "$CASH". Cash activities now carry no asset, like the addon's imports.
- **Existing installations are repaired automatically:** the first run of each broker after the update removes the `$CASH` asset from the sync's own activities in Wealthfolio (updated in place, not deleted and created again); Wealthfolio then recalculates the portfolio. Until that has worked the broker books nothing, the run is reported as failed and the next one tries again.
- The holdings check now also reports cash on the securities account (it should be 0) and any "$CASH" position, on either account.

## 0.3.5

- **Fix: the holdings check listed every position twice** - once with the broker's shares and Wealthfolio 0, once with 0 and Wealthfolio's shares. Wealthfolio's holdings carry no ISIN and the addon books under the ticker the user mapped, so the check compared the ISIN with the ticker. The sync now learns which Wealthfolio asset an ISIN is booked under from the activities of its trades and dividends (its own or the CSV/PDF ones it recognised) and compares positions by that asset.
- New trades and dividends of a known ISIN are booked onto that asset, instead of opening a second position under the ISIN.
- Unknown Trade Republic events now show their amount (`betrag`) on the page *Unbekannte Buchungen*, so they can be booked by hand; events already listed get it with the next run.
- After this update, the next run of a broker with positions (Trade Republic) fetches the whole history once to learn the assets; nothing already in Wealthfolio is booked again.

## 0.3.4

- **Fix: "Jetzt abrufen" did nothing for a broker with "Automatisch abrufen" off.** Each broker now has its own "Jetzt abrufen" button on the status page, which runs it even when automatic fetching is off; the general button is now "Alle abrufen" and, like the daily run, skips those brokers (the page says so). The status of such a broker shows its last result plus "automatischer Abruf aus" instead of only "aus".
- After removing duplicates, the holdings/cash check on the status page is discarded (it was computed with the duplicates) and recomputed by the broker's next run; the message says to click "Jetzt abrufen".

## 0.3.3

- **Fix: removing duplicates stopped after the first transfer leg.** Wealthfolio deletes both legs of a linked transfer pair when one is deleted; deleting the second leg then failed ("not found") and aborted the whole removal. The partner leg is now skipped, "not found" counts as removed, and one transaction failing no longer stops the others - the page lists what couldn't be removed.
- Logging in to a broker whose saved session is still valid now says so ("die gespeicherte Sitzung ist noch gültig, eine Bestätigung war nicht nötig") instead of a bare "angemeldet", which looked as if nothing had happened.

## 0.3.2

- **Fix: trades and dividends imported by CSV were booked a second time.** The check against existing activities required the same symbol, but the addon books a security under the ticker the user mapped, the sync under the ISIN. A trade now also matches with another symbol when share count, amount (±0.02) and time (≤36 h) agree; a dividend when it is the only candidate.
- **New page "Duplikate":** lists the activities the sync created although the CSV/PDF import already had them, side by side, and - after confirmation - deletes only the sync's copies (with their transfer legs). The transaction is then marked as existing and not created again.
- **Fix: Trade Republic positions were read as empty.** Trade Republic groups them in categories now (`categories[].positions[]`, field `isin`); the holdings check showed every position as 0 at Trade Republic.

## 0.3.1

- Updates can no longer leave the service unable to start: the update keeps the previous Python environment until the new one is completely installed, puts it back if the install fails (network, disk, a broken package), restarts the previous version and says so. Before, a failed install left no environment at all and the service restarted in a loop ("Unable to locate executable .../venv/bin/brokersync").

## 0.3.0

- **Trade Republic** (waldonso2/wealthfolio-importer-addon#38), read-only via pytr 0.4.10: web login confirmed in the Trade Republic app (or with an authenticator code) without logging out the phone; the session is kept and resumed, and a scheduled run asks for a new confirmation via ntfy when it has expired. Timeline events are booked like the addon's CSV import: trades and savings plans with fee and tax, dividends net with withholding tax, Saveback as a bonus-funded buy, interest, Vorabpauschale, tax corrections, deposits, card payments and refunds, transfers (with transfer patterns). Corporate actions, securities transfers and private markets are reported for the CSV import; informational and cancelled events are skipped.
- **Holdings check** after every run (all brokers): the broker's cash and - for Trade Republic and the dummy - positions against Wealthfolio's holdings, shown on the status page and reported via ntfy when a deviation lasts two runs.
- The dummy reports its positions, so the check can be tried with it.

## 0.2.1

- The web UI shows the installed version in the header, next to the name (previously only in the footer).

## 0.2.0

- **DKB** (waldonso2/wealthfolio-importer-addon#37): giro account via FinTS (python-fints), read-only. Balance and transactions; confirmation in the DKB app in the web UI, or with a ntfy message and a few minutes' wait during the daily run. Deposits, card payments/withdrawals, interest and fees are booked on the DKB cash account; outbound transfers to own accounts become transfers via the new *Überträge* page (transfer patterns as in the addon).
- Giro bookings for depot trades and payouts aren't booked again: the sync checks that the addon's PDF import booked them and reports the ones still missing until it has.
- The status page shows the broker's balance after each run, for comparison with Wealthfolio.
- After a rejected PIN the service doesn't contact the bank again until the credentials are saved anew, so the online banking isn't locked.

## 0.1.3

- The status page shows the real state of the sync: "läuft" only while a sync actually holds the lock (from the web UI or the timer), and it refreshes itself every 3 seconds until the sync is done.
- Runs left at "läuft" by a process that ended in the middle of a sync (restart, update, crash) are closed as "abgebrochen" instead of staying "läuft" forever.
- A sync started while the status page checks the lock waits a moment instead of being skipped.

## 0.1.2

- Dummy test data is easy to find and remove in Wealthfolio: its transactions are dated on the day of the login to the dummy (a few hours before it, German time) and marked TEST in every comment (`TEST Kauf …`, ids `TEST-<date>-<n>`). The date is fixed at the login, so the daily timer doesn't book a new set every day; logging in to the dummy again on another day books a new one.

## 0.1.1

- Install with one line in the Proxmox shell instead of PVE Scripts Local: its catalog only lists scripts from the official community-scripts database, so a custom repository never showed up there. The container script now loads the community-scripts engine (`community-scripts/core`) with this repository as script source, so the install script and the container's `update` command come from here.

## 0.1.0

First version: the base of the service (waldonso2/wealthfolio-importer-addon#36), with a dummy broker to try it end to end. DKB, Trade Republic and Scalable Capital follow as their own adapters (#37–#39).

- Adapter interface `get_accounts` / `get_positions` / `get_transactions(since)` / `get_cash`, read-only, with a two-step login for TAN/app confirmation.
- Booking rules of the Broker Importer addon: two-account model, transfer pairs with `sourceGroupId`, `amount = tradeFinalCash`, fee and tax in their own fields, tax refunds as CREDIT/TAX_REFUND.
- Deduplication: sync state, Wealthfolio's duplicate fingerprint, and a check against activities the addon already imported from CSV or PDF (same as its `matchExisting`). An interrupted run is completed by the next one.
- Web UI with setup assistant, status per broker, "Jetzt abrufen", broker login with TAN, ntfy settings, security mappings, list of unknown event types.
- Credentials encrypted (Fernet, key file 0600), web UI behind its own password.
- Daily systemd timer; ntfy notifications with a link when a TAN is due, a login expired or a broker failed.
- One-click install and update through PVE Scripts Local (community-scripts layout `ct/`, `install/`, `json/`); updates keep configuration and credentials and back them up first.
