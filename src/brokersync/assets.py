"""Which Wealthfolio asset a broker's ISIN is booked under.

The addon books a security under the ticker the user mapped (``NVDA``), the
sync under the ISIN, and Wealthfolio's holdings carry no ISIN. So the asset is
learned from the activities of the broker's trades and dividends: the sync
state knows which Wealthfolio activities belong to a transaction - the sync's
own or the CSV/PDF activity it matched - and that activity names the asset.

Used for the holdings check (positions compared by asset, not by symbol) and
for booking: a new trade of a known ISIN goes onto the same asset, instead of
opening a second position under the ISIN.

An asset the addon created (another symbol than the ISIN) wins over one the
sync created under the ISIN.
"""

from __future__ import annotations

from datetime import timedelta

from .mapping import Accounts, SecurityMapping, is_cash_symbol
from .model import Kind, Transaction
from .state import State
from .wealthfolio import WealthfolioClient

SECURITY_KINDS = (Kind.BUY, Kind.SELL, Kind.DIVIDEND)
SECURITY_TYPES = ("BUY", "SELL", "DIVIDEND")


def _preferred(isin: str, known: dict | None) -> bool:
    """True when ``known`` is already the asset to keep (one the addon created)."""
    return bool(known) and known["symbol"] != isin


def learn(state: State, broker: str, transactions: list[Transaction], wf: WealthfolioClient,
          accounts: Accounts) -> int:
    """Learn the assets of the transactions' ISINs; returns how many were (re)learned."""
    known = state.assets(broker)
    wanted: dict[str, list[str]] = {}  # activity id -> ISINs
    dates = []
    for tx in transactions:
        if tx.kind not in SECURITY_KINDS or not tx.isin or _preferred(tx.isin, known.get(tx.isin)):
            continue
        ids = state.activity_ids(broker, tx.id)
        if ids:
            for i in ids:
                wanted.setdefault(i, []).append(tx.isin)
            dates.append(tx.datetime)
    if not wanted:
        return 0
    # Activities are stored at the transaction's time; a day of margin for time zones.
    activities = wf.search_activities([accounts.cash, accounts.portfolio], (min(dates) - timedelta(days=1)).date(),
                                      (max(dates) + timedelta(days=1)).date())
    learned = 0
    for a in activities:
        isins = wanted.get(a.get("id", ""))
        asset_id = a.get("assetId") or ""
        if not isins or a.get("activityType") not in SECURITY_TYPES or not asset_id or is_cash_symbol(asset_id):
            continue
        symbol = a.get("assetSymbol") or asset_id
        for isin in isins:
            current = known.get(isin)
            if _preferred(isin, current) or (current and current["asset_id"] == asset_id):
                continue
            entry = {"asset_id": asset_id, "symbol": symbol, "exchange_mic": a.get("exchangeMic") or None,
                     "name": a.get("assetName") or None}
            state.set_asset(broker, isin, **entry)
            known[isin] = entry
            learned += 1
    return learned


def mappings(state: State, broker: str) -> dict[str, SecurityMapping]:
    return {isin: SecurityMapping(a["symbol"], a["exchange_mic"], a["name"], a["asset_id"])
            for isin, a in state.assets(broker).items()}
