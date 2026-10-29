# Data contract: Wealthfolio Companion → X-Ray addon (schema 1)

Wealthfolio Companion writes the funds' holdings lists into Wealthfolio's addon storage; the X-Ray addon
(`waldonso2/wealthfolio-companion-addon-xray`) reads them, takes the positions' values live from Wealthfolio and
computes X-Ray in the browser. This copy (in the service) is leading; the addon keeps the same files, and CI compares
them. The full specification is waldonso2/wealthfolio-importer-addon#146.

## Storage (addon id `broker-sync-xray`)

| Key | Written by | Content |
|---|---|---|
| `xray:index` | service | `{schema, generation, written_at, service_version, base_currency, last_pass {at, error}, isins {asset id: ISIN}, funds {asset id: {h, name, isin, issuer, fetched_at, source, note, ter, ter_source, lines, coverage, parts, sha256}}, missing [{asset_id, name, isin, reason}]}` |
| `xray:f:<h>:<generation>:<part>` | service | `{schema, asset_id, part, parts, lines: [[key, name, weight, country, sector, currency], …]}` |
| `addon:settings` | addon | `{schema, limits {…}, accepted [finding key]}` |

- `h` = first 16 hex characters of SHA-256 of the asset id; numbers are strings (exact decimals), never JSON numbers.
- `sha256` = SHA-256 of the compact JSON (no spaces, UTF-8 unescaped) of all lines of a fund in order.
- Each value stays under 230 000 bytes measured as Wealthfolio does: the compact JSON of `{addon_id, key, value}`.
- Writing: parts of the new generation, then `xray:index` (the switch), then the old generation's parts are deleted.
  Reading: the index, then every part of its generation; a missing part or a wrong `sha256` means the service is
  writing - read the index once more.
- Never in storage: values, shares, accounts, costs. `ter` is a share (`0.002` = 0.20 %), `null` when unknown.

## Computation (`cases.json`)

`cases.json` holds fabricated depots and what `src/brokersync/xray.py` computes for them (`unify`, `exposures`,
`overlap`, `own_share`, `effective_count`, `findings`). Built by `scripts/xray_cases.py`; the addon must give the same
numbers (tolerance 1e-9).
