# Changelog

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
