# Wealthfolio Broker Sync

Service that fetches broker transactions and writes them, deduplicated, into a self-hosted Wealthfolio via its REST API. Installed as an LXC through PVE Scripts Local (community-scripts layout). Companion of the Broker Importer addon (`waldonso2/wealthfolio-importer-addon`); the backlog lives there (#35, base #36, adapters #37 DKB, #38 Trade Republic, #39 Scalable Capital).

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
| `src/brokersync/adapters/` | `base.py` interface (read-only; `login` / `complete_login` for TAN; `replay` for contract tests), `dummy.py`, registry in `__init__.py` |
| `src/brokersync/mapping.py` | Transaction → Wealthfolio `NewActivity` payloads — port of the addon's rules |
| `src/brokersync/dedup.py` | Check against existing Wealthfolio activities (port of the addon's `matchExisting`) |
| `src/brokersync/sync.py` | One run over all enabled brokers: login, fetch, dedupe, create, report |
| `src/brokersync/wealthfolio.py` | REST client: password login → `wf_session` JWT sent as Bearer; accounts, create, search |
| `src/brokersync/vault.py` | Fernet-encrypted secrets (`data/secrets.enc`, key `data/secret.key` 0600), UI password hash |
| `src/brokersync/config.py` / `state.py` | Non-secret config (`data/config.json`, written by the UI only) / SQLite sync state, runs, unknown events |
| `src/brokersync/notify.py` | ntfy |
| `src/brokersync/web/` | FastAPI + Jinja2 UI (German texts), CSRF via a dependency, own login |
| `ct/`, `install/`, `json/` | community-scripts files for PVE Scripts Local |
| `deploy/` | `setup.sh` (venv + units, used by install and update), systemd units, reset-password helper |
| `tests/` | `fakes.py` (in-memory Wealthfolio), unit/e2e tests, `contract/<adapter>/*.json` recorded cases, `fixtures/wealthfolio/` recorded API answers |

## Invariants (keep them)

- **Book exactly like the addon** (`src/pdf/activities.ts`, `src/common.ts` there): two-account model, every internal TRANSFER_OUT/TRANSFER_IN pair shares a `sourceGroupId`, BUY/SELL `amount = trade_final_cash(...)`, fee/tax in their own fields, tax refund = CREDIT/TAX_REFUND, cash activities use symbol `$CASH-<ccy>` with quantity/unitPrice 1. When the addon's rules change, change `mapping.py` and its tests too.
- **Comments are part of Wealthfolio's duplicate fingerprint.** Every activity ends in `[SYNC <broker>:<tx id>]`; never reword existing comment texts, or synced activities reappear as new.
- **Dedup layers:** state DB (`synced`), Wealthfolio's fingerprint ("Duplicate activity detected" → `Duplicate`, not an error), `dedup.ExistingIndex` for activities from CSV/PDF imports. Activities carrying our own `[SYNC …]` reference are *not* matched there: the whole transaction is re-sent so an interrupted run is completed.
- **A transaction is marked synced only when all its activities exist.** Failures stay unmarked and are retried; a run with failures is not a "success", so the next run fetches from before it.
- **Adapters are read-only** (AC 7 of #35): no endpoint that trades, transfers or changes settings.
- **Unknown event types** become `Kind.UNKNOWN`: stored, listed in the UI, notified once — never dropped, never booked.
- **One broker failing never stops the others**; it is reported via ntfy with a link (`public_url` + path).
- **Secrets only in the vault**, never in `config.json`, logs, exceptions shown to the user, or the repo. Test data is fabricated — no real statements, names, IBANs or account numbers.
- `ct/wealthfolio-broker-sync.sh` line 2 must stay the exact `misc/build.func` source line: PVE Scripts Local rewrites that line to its bundled build.func, which then runs our `install/` script. `test_packaging.py` checks this.

## Adding a broker

1. `src/brokersync/adapters/<key>.py`: subclass `BrokerAdapter`, set `key`, `label`, `credential_fields`; implement `login` (raise `AuthRequired(Challenge(...))` when the user is needed), `complete_login`, `session_state`, the four getters, and `replay`.
2. Map every event type the broker has to a `Kind`; anything else → `Kind.UNKNOWN` with `raw_type` and a `raw` payload without personal data.
3. Register it in `adapters/__init__.py`.
4. Add fabricated contract cases in `tests/contract/<key>/` (see its README).

## Releasing

Version lives in `pyproject.toml` and `src/brokersync/__init__.py` (tested to match) with a `CHANGELOG.md` section `## <version>`. The release workflow creates `v<version>` when the tag doesn't exist yet; install and update deploy the latest release. Propose a bump for every change to `src/`, `deploy/`, `ct/` or `install/`; docs/CI-only changes need none.
