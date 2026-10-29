#!/usr/bin/env bash
# Not an installer: Wealthfolio Broker Sync is now Wealthfolio Companion. Containers installed before keep calling
# this file from their `update` command; it installs nothing and says how to move.
cat >&2 <<'MSG'
Wealthfolio Broker Sync is now Wealthfolio Companion - a new product that starts over at 0.1.0.
It is not installed by `update`: move by installing Wealthfolio Companion fresh and restoring a backup of
this installation into it, step by step:
  https://github.com/waldonso2/wealthfolio-companion/blob/main/CHANGELOG.md#moving-from-wealthfolio-broker-sync

Wealthfolio Broker Sync heißt jetzt Wealthfolio Companion - ein neues Produkt, das bei 0.1.0 neu beginnt.
`update` installiert es nicht: Wealthfolio Companion neu installieren und eine Sicherung dieser Installation
dort wiederherstellen, Schritt für Schritt (englisch):
  https://github.com/waldonso2/wealthfolio-companion/blob/main/docs/OPERATIONS.md#moving-from-wealthfolio-broker-sync

Nothing was changed here. / Hier wurde nichts geändert.
MSG
exit 1
