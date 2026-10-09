# Changelog

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
