# Shared duplicate-detection cases

The Wealthfolio Companion service (`src/brokersync/dedup.py`, `ExistingIndex.find`) and the Broker Importer
addon (`src/importer.ts`, `matchExisting`) must recognise the same activities as "already in Wealthfolio": an
import after the sync, or the sync after an import, must never book a transaction twice.

`cases.json` holds fabricated cases both test suites run:

- `existing`: activities as Wealthfolio lists them (`id`, `accountId`, `activityType`, `subtype`, `date`,
  `quantity`, `unitPrice`, `amount`, `assetSymbol`, `assetId`, `currency`). Of several that fit, the nearest in time is the one (then the closest amount).
- `incoming`: the activity that stands for a new transaction (`line`, `accountId`, `activityType`, `subtype`,
  `date`, `quantity`, `amount`, `symbol`, `currency`), processed in this order. `currency` on either side is optional; when both have one, they must be the same.
- `expect`: per incoming line, the id of the existing activity it is, or `null`.

**The service's copy is the leading one.** Change it there (with `dedup.py` and both implementations), then copy
the file unchanged to the addon (`contract/dedup/cases.json`). CI in each repository compares its copy with the
other repository's `main` and fails when they differ.
