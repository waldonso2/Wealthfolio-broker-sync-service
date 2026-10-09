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
| `src/brokersync/adapters/` | `base.py` interface (read-only; `login` / `complete_login` for TAN; `on_user_action` for app confirmations during scheduled runs; `close`; `replay` for contract tests), `dkb.py` (python-fints: MT940/camt → `Transaction`, decoupled TAN, PIN lock-out guard), `tr.py` (pytr, pinned: v2 web login confirmed in the app, cookies in the session, timeline + details → pytr's `Event` → `Transaction`), `dummy.py`, registry in `__init__.py`; `reports_positions` marks adapters whose positions are complete |
| `src/brokersync/mapping.py` | Transaction → Wealthfolio `NewActivity` payloads — port of the addon's rules |
| `src/brokersync/dedup.py` | Check against existing Wealthfolio activities (port of the addon's `matchExisting`) |
| `src/brokersync/sync.py` | One run over all enabled brokers: login, fetch, dedupe, create, report |
| `src/brokersync/wealthfolio.py` | REST client: password login → `wf_session` JWT sent as Bearer; accounts, create, search |
| `src/brokersync/vault.py` | Fernet-encrypted secrets (`data/secrets.enc`, key `data/secret.key` 0600), UI password hash |
| `src/brokersync/config.py` / `state.py` | Non-secret config (`data/config.json`, written by the UI only) / SQLite sync state, runs, unknown events |
| `src/brokersync/reconcile.py` | Broker cash/positions vs. Wealthfolio holdings (`GET /holdings?accountId=`) after each run; reported when a deviation lasts two runs |
| `src/brokersync/notify.py` | ntfy |
| `src/brokersync/web/` | FastAPI + Jinja2 UI (German texts), CSRF via a dependency, own login |
| `ct/`, `install/`, `json/` | community-scripts files (`json/` is the catalog entry for a later submission to community-scripts) |
| `deploy/` | `setup.sh` (venv + units, used by install and update), systemd units, reset-password helper |
| `tests/` | `fakes.py` (in-memory Wealthfolio), unit/e2e tests, `contract/<adapter>/*.json` recorded cases, `fixtures/wealthfolio/` recorded API answers |

## Invariants (keep them)

- **Book exactly like the addon** (`src/pdf/activities.ts`, `src/common.ts` there): two-account model, every internal TRANSFER_OUT/TRANSFER_IN pair shares a `sourceGroupId`, BUY/SELL `amount = trade_final_cash(...)`, fee/tax in their own fields, tax refund = CREDIT/TAX_REFUND, cash activities use symbol `$CASH-<ccy>` with quantity/unitPrice 1. When the addon's rules change, change `mapping.py` and its tests too.
- **Comments are part of Wealthfolio's duplicate fingerprint.** Every activity ends in `[SYNC <broker>:<tx id>]`; never reword existing comment texts, or synced activities reappear as new.
- **Dedup layers:** state DB (`synced`), Wealthfolio's fingerprint ("Duplicate activity detected" → `Duplicate`, not an error), `dedup.ExistingIndex` for activities from CSV/PDF imports. Activities carrying our own `[SYNC …]` reference are *not* matched there: the whole transaction is re-sent so an interrupted run is completed.
- **A transaction is marked synced only when all its activities exist.** Failures stay unmarked and are retried; a run with failures is not a "success", so the next run fetches from before it.
- **Adapters are read-only** (AC 7 of #35): no endpoint that trades, transfers or changes settings.
- **Bank-side cash of securities is never booked twice.** A giro booking that settles a depot trade or payout is `Kind.SECURITIES_CASH`: the sync only looks for the transfer leg the securities side (the addon's PDF import, later a depot adapter) put on the cash account (`ExistingIndex.find_settlement`, ±0.02, ≤6 days). Unmatched ones are reported (`UNMATCHED_SECURITIES`), not marked synced, and keep the fetch window open (`State.oldest_open`) until they match.
- **Transfer patterns** (`Config.transfer_patterns`, UI *Überträge*) follow the addon: only outbound money (WITHDRAWAL) checks them; inbound is always DEPOSIT.
- **Never risk a bank lock-out:** after a rejected PIN an adapter sets `pin_rejected` in its session and refuses to contact the bank until the credentials are saved anew (`Vault.set_broker_credentials` clears the session).
- **Trade Republic events go through pytr's `Event.from_dict`** (the parser pytr's exports use), then `tr.to_transactions` maps them like the addon's `transform.ts`. Events pytr lists as informational (`events_known_ignored*`) and cancelled ones are skipped; everything else it can't book (corporate actions, securities transfers, private markets) is `UNKNOWN`. pytr is pinned (`pytr==…`): update it deliberately and re-run the contract case. Unknown payloads keep only eventType/title/subtitle/status.
- **Unknown event types** become `Kind.UNKNOWN`: stored, listed in the UI, notified once — never dropped, never booked.
- **One broker failing never stops the others**; it is reported via ntfy with a link (`public_url` + path).
- **Test data that can reach a real Wealthfolio** (the dummy adapter, anything a user tests with) is dated today — the dummy uses the day of its login, fixed in the session so the daily timer doesn't book it again — and marked **TEST** in every activity's comment (the dummy's transaction ids start with `TEST-`), so the user finds and deletes it easily. Fixtures for automated tests may use fixed dates.
- **Secrets only in the vault**, never in `config.json`, logs, exceptions shown to the user, or the repo. Test data is fabricated — no real statements, names, IBANs or account numbers.
- `ct/wealthfolio-broker-sync.sh` exports `COMMUNITY_SCRIPTS_URL` (this repo's raw `main`) **before** sourcing `community-scripts/core`'s `core/build.func`: the engine resolves `install/<slug>-install.sh` and writes the container's `update` command from it. Without it both point to community-scripts/ProxmoxVE. `test_packaging.py` checks this, and that the install line in the README matches.

## Adding a broker

1. `src/brokersync/adapters/<key>.py`: subclass `BrokerAdapter`, set `key`, `label`, `credential_fields`; implement `login` (raise `AuthRequired(Challenge(...))` when the user is needed), `complete_login`, `session_state`, the four getters, and `replay`.
2. Map every event type the broker has to a `Kind`; anything else → `Kind.UNKNOWN` with `raw_type` and a `raw` payload without personal data.
3. Register it in `adapters/__init__.py`.
4. Add fabricated contract cases in `tests/contract/<key>/` (see its README).

## Releasing

Version lives in `pyproject.toml` and `src/brokersync/__init__.py` (tested to match) with a `CHANGELOG.md` section `## <version>`. The release workflow creates `v<version>` when the tag doesn't exist yet; install and update deploy the latest release. Propose a bump for every change to `src/`, `deploy/`, `ct/` or `install/`; docs/CI-only changes need none.
