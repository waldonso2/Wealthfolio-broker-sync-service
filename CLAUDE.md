# Wealthfolio Broker Sync

Service that fetches broker transactions and writes them, deduplicated, into a self-hosted Wealthfolio via its REST API. Installed as an LXC with one line in the Proxmox shell (community-scripts layout and engine); PVE Scripts Local can't list it, because its catalog only shows scripts from the official community-scripts database. Companion of the Broker Importer addon (`waldonso2/wealthfolio-importer-addon`); the backlog lives there (#35, base #36, adapters #37 DKB, #38 Trade Republic, #39 Scalable Capital).

## Commands

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest -q                 # all tests
ruff check src tests      # lint
shellcheck ct/*.sh install/*.sh deploy/setup.sh deploy/brokersync-reset-password
BROKERSYNC_DATA=./data brokersync serve --port 8090   # UI locally
BROKERSYNC_DATA=./data brokersync run                 # one sync
```

## Layout

| Path | Purpose |
|---|---|
| `src/brokersync/model.py` | Broker-neutral `Transaction` (Decimal amounts), `Kind`, `Position`, `CashBalance` |
| `src/brokersync/adapters/` | `base.py` interface (read-only; `login` / `complete_login` for TAN; `on_user_action` for app confirmations during scheduled runs; `close`; `replay` for contract tests), `fints.py` (`FintsAdapter`, shared by every FinTS bank: python-fints, MT940/camt → `Transaction`, decoupled TAN or a TAN to type, PIN lock-out guard), `dkb.py` (DKB's profile on it), `tr.py` (pytr, pinned: v2 web login confirmed in the app, cookies in the session, timeline + details → pytr's `Event` → `Transaction`), `scalable.py` (Scalable's official CLI `sc` as a subprocess: device-code login `--local-read-only`, `--json` read commands; its config dir lives in a temp dir, the files are kept in the vault as `session["files"]`), registry in `__init__.py`; `reports_positions` marks adapters whose positions are complete, `full_history` those that read their whole history every run |
| `src/brokersync/mapping.py` | Transaction → Wealthfolio `NewActivity` payloads — port of the addon's rules |
| `src/brokersync/dedup.py` | Check against existing Wealthfolio activities (port of the addon's `matchExisting`) |
| `src/brokersync/sync.py` | One run over all enabled brokers: login, fetch, dedupe, create, report |
| `src/brokersync/wealthfolio.py` | REST client: password login → `wf_session` JWT sent as Bearer; accounts, create, update, delete, search, holdings |
| `src/brokersync/vault.py` | Fernet-encrypted secrets (`data/secrets.enc`, key `data/secret.key` 0600), UI password hash |
| `src/brokersync/config.py` / `state.py` | Non-secret config (`data/config.json`, written by the UI only) / SQLite sync state, runs, unknown events |
| `src/brokersync/assets.py` | ISIN → Wealthfolio asset, learned from the activities of trades/dividends (holdings carry no ISIN, the addon books under mapped tickers); used for the holdings check and to book new trades onto the same asset |
| `src/brokersync/reconcile.py` | Broker cash/positions vs. Wealthfolio holdings (`GET /holdings?accountId=`) after each run, plus cash on the securities account (must be 0) and `$CASH` positions; reported when a deviation lasts two runs |
| `src/brokersync/repair.py` | Once per broker (state flag): removes the `$CASH` asset from the sync's own cash activities (PUT `/activities`, `asset: {}`); the broker books nothing until it worked |
| `src/brokersync/duplicates.py` | Finds activities the sync created on top of CSV/PDF imports (same rules as `ExistingIndex`) and removes only the sync's copies; UI page *Duplikate*, confirmation required |
| `src/brokersync/coverage.py` | After each run: transactions synced before vs. Wealthfolio (by `[SYNC …]` reference and stored ids) → missing/partial/ignored, and for `full_history` adapters activities the broker no longer lists (orphan); stored in `state.gaps`, shown on *Prüfung* with *Wieder anlegen* / *Ignorieren* |
| `src/brokersync/audit.py` | Read-only check of a broker's two accounts (UI page *Prüfung*): depot moments (activities ≤10 s apart) whose cash effect isn't zero (two that cancel out within 36 h are a pair, in-kind payouts move no cash), and transfers between the two accounts without their other leg |
| `src/brokersync/notify.py` | ntfy |
| `src/brokersync/sc_install.py` | `brokersync install-sc`: downloads the latest `sc` release, verifies Scalable's minisign signature (pinned key) and the SHA-256 before writing `/opt/wealthfolio-broker-sync/bin/sc`; called by `deploy/setup.sh`, failure is non-fatal |
| `src/brokersync/web/` | FastAPI + Jinja2 UI (German texts), CSRF via a dependency, own login |
| `ct/`, `install/`, `json/` | community-scripts files (`json/` is the catalog entry for a later submission to community-scripts) |
| `deploy/` | `setup.sh` (venv + units, used by install and update), systemd units, reset-password helper |
| `ARCHITECTURE.md` | Overview for contributors: components, run sequence, dedup layers, data files, routes |
| `src/brokersync/retired.py` | On start (`Syncer.__init__`): removes config, vault entries and sync state of brokers that no longer exist (`dummy` up to 0.3.6); their Wealthfolio activities stay, the overview shows once how many and how to find them |
| `tests/` | `fakes.py` (in-memory Wealthfolio), `fake_broker.py` (test-only broker with a TAN step; no test broker ships), `fake_fints.py` (fake python-fints client), unit/e2e tests, `contract/<adapter>/*.json` recorded cases, `fixtures/wealthfolio/` recorded API answers |

## Invariants (keep them)

- **Book exactly like the addon** (`src/pdf/activities.ts`, `src/common.ts` there): two-account model, every internal TRANSFER_OUT/TRANSFER_IN pair shares a `sourceGroupId`, BUY/SELL `amount = trade_final_cash(...)`, fee/tax in their own fields, tax refund = CREDIT/TAX_REFUND, cash activities carry **no asset** (quantity/unitPrice 1) - with an asset Wealthfolio books a TRANSFER_IN/OUT as a securities transfer that moves no money; `repair.py` fixes the `$CASH-<ccy>` ones versions before 0.3.6 created. When the addon's rules change, change `mapping.py` and its tests too.
- **Comments are part of Wealthfolio's duplicate fingerprint.** Every activity ends in `[SYNC <broker>:<tx id>]`; never reword existing comment texts, or synced activities reappear as new.
- **Dedup layers:** state DB (`synced`), Wealthfolio's fingerprint ("Duplicate activity detected" → `Duplicate`, not an error), `dedup.ExistingIndex` for activities from CSV/PDF imports. Never require the same symbol there: the addon books securities under the user's mapped ticker, the sync under the ISIN - trades match on share count + amount + time, dividends with another symbol only when unambiguous. Activities carrying our own `[SYNC …]` reference are *not* matched there: the whole transaction is re-sent so an interrupted run is completed.
- **Wealthfolio says what is booked, the user decides what comes back.** `state.synced` only decides what is *new*; every run compares the rest with Wealthfolio (`coverage.py`). Missing ones are listed, never re-created without the user's *Wieder anlegen* (a deletion may be on purpose); orphans are listed, never deleted. `ignored` transactions are never booked.
- **A transaction is marked synced only when all its activities exist.** Failures stay unmarked and are retried; a run with failures is not a "success", so the next run fetches from before it.
- **Adapters are read-only** (AC 7 of #35): no endpoint that trades, transfers or changes settings. The Scalable adapter calls only `sc` read commands and logs in with `--local-read-only`.
- **`sc` only from Scalable's signed release** (`sc_install.py`); never a build from source, a fork or another package source. The CLI's session files never stay on disk: temp dir per adapter, contents in the vault.
- **Bank-side cash of securities is never booked twice.** A giro booking that settles a depot trade or payout is `Kind.SECURITIES_CASH`: the sync only looks for the transfer leg the securities side (the addon's PDF import, later a depot adapter) put on the cash account (`ExistingIndex.find_settlement`, ±0.02, ≤6 days). Unmatched ones are reported (`UNMATCHED_SECURITIES`), not marked synced, and keep the fetch window open (`State.oldest_open`) until they match.
- **Transfer patterns** (`Config.transfer_patterns`, UI *Überträge*) follow the addon: only outbound money (WITHDRAWAL) checks them; inbound is always DEPOSIT.
- **Never risk a bank lock-out:** after a rejected PIN an adapter sets `pin_rejected` in its session and refuses to contact the bank until the credentials are saved anew (`Vault.set_broker_credentials` clears the session).
- **Trade Republic events go through pytr's `Event.from_dict`** (the parser pytr's exports use), then `tr.to_transactions` maps them like the addon's `transform.ts`. Events pytr lists as informational (`events_known_ignored*`) and cancelled ones are skipped; everything else it can't book (corporate actions, securities transfers, private markets) is `UNKNOWN`. pytr is pinned (`pytr==…`): update it deliberately and re-run the contract case. Unknown payloads keep only eventType/title/subtitle/status and the amount (`betrag`), so the user can book them by hand.
- **Scalable** maps `sc broker transactions` like the addon's `src/scalable.ts` (whose CSV comes from the same API via the Transactions Exporter userscript): only FILLED/SETTLED; depot-migration pairs (security out/in, same ISIN and shares, ≤7 days; cash `SWITCH-` deposit + equal withdrawal) are skipped; a security `SWAP_OUT` with its cash `SWAP_OUT` (fund swap) and a security leg out with value 0 plus a distribution of the same ISIN and day (certificate redemption) become a SELL; a cancelled distribution and its original always cancel out (an original synced already shows up as orphan on *Prüfung*); every run reads the whole history (`full_history`), details only for new transactions; other cancellations, unpaired security transfers, ELTIFs, negative interest and fee refunds are `UNKNOWN`. A distribution has no share count: the dividend gets quantity 1, like the addon.
- **Unknown event types** become `Kind.UNKNOWN`: stored, listed in the UI, notified once — never dropped, never booked.
- **One broker failing never stops the others**; it is reported via ntfy with a link (`public_url` + path).
- **No test broker ships.** Test brokers live under `tests/` only (`fake_broker.py`); a broker the user can set up must be a real one - the old "Dummy" booked test data into real accounts. Anything that could still reach a real Wealthfolio as test data is marked **TEST** in every activity's comment.
- **Secrets only in the vault**, never in `config.json`, logs, exceptions shown to the user, or the repo. Test data is fabricated — no real statements, names, IBANs or account numbers.
- `ct/wealthfolio-broker-sync.sh` exports `COMMUNITY_SCRIPTS_URL` (this repo's raw `main`) **before** sourcing `community-scripts/core`'s `core/build.func`: the engine resolves `install/<slug>-install.sh` and writes the container's `update` command from it. Without it both point to community-scripts/ProxmoxVE. `test_packaging.py` checks this, and that the install line in the README matches.

## Adding a broker

1. `src/brokersync/adapters/<key>.py`: subclass `BrokerAdapter`, set `key`, `label`, `credential_fields`; implement `login` (raise `AuthRequired(Challenge(...))` when the user is needed), `complete_login`, `session_state`, the four getters, and `replay`.
2. Map every event type the broker has to a `Kind`; anything else → `Kind.UNKNOWN` with `raw_type` and a `raw` payload without personal data.
3. Register it in `adapters/__init__.py`.
4. Add fabricated contract cases in `tests/contract/<key>/` (see its README).

A bank that speaks FinTS needs no adapter of its own - only a profile, like `dkb.py`:

1. Subclass `FintsAdapter`; set `key`, `label`, `credential_fields = credential_fields(...)` (`blz=True` if the bank code differs per branch), `blz` (or leave it empty), `server`, and the names in messages (`bank`, `app`, `banking`).
2. Override `securities_pattern` / `interest_pattern` / `fee_pattern` only if the bank's posting texts differ; giro bookings of depot trades must become `SECURITIES_CASH`.
3. Never change `fints.to_transactions`' id scheme (content hash + counter): every synced booking would look new.
4. Register it, add a contract case with its own posting texts, and run `tests/test_fints.py` against it (add it to `PROFILES`).

## Releasing

Version lives in `pyproject.toml` and `src/brokersync/__init__.py` (tested to match) with a `CHANGELOG.md` section `## <version>`. The release workflow creates `v<version>` when the tag doesn't exist yet; install and update deploy the latest release. Propose a bump for every change to `src/`, `deploy/`, `ct/` or `install/`; docs/CI-only changes need none.
