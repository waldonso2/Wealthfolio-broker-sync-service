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

Trades a CSV/PDF import booked on its own (not matched by the sync, e.g. DKB
statements imported before the sync read the giro account) are found too
(``learn_from_imports``): the import names the ISIN in the comment of a
trade's cash transfer ("Funds for <ISIN> (…) buy", "<ISIN> (…) sale -> Cash"),
booked a few seconds from the trade itself.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

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


ISIN = re.compile(r"\b([A-Z]{2}[A-Z0-9]{9}\d)\b")
# A trade and its transfer legs are booked this close together (the addon: 1-2 seconds apart).
LEG_SECONDS = 10


def valid_isin(code: str) -> bool:
    """ISO 6166 check digit (Luhn over the letters as numbers) - so a random 12-character word isn't one."""
    digits = "".join(str(int(c, 36)) for c in code[:-1])
    total = 0
    for i, d in enumerate(reversed(digits)):
        n = int(d) * (2 if i % 2 == 0 else 1)
        total += n // 10 + n % 10
    return (10 - total % 10) % 10 == int(code[-1])


def _when(a: dict) -> datetime:
    return datetime.fromisoformat(str(a.get("date")).replace("Z", "+00:00"))


def learn_from_imports(state: State, broker: str, wf: WealthfolioClient, accounts: Accounts,
                       isins: set[str]) -> int:
    """Learn the assets of ``isins`` (positions the broker reports, none known yet) from imported trades whose
    transfer names the ISIN; returns how many were learned. An ISIN on trades of two assets stays unknown."""
    known = state.assets(broker)
    wanted = {i for i in isins if not _preferred(i, known.get(i))}
    if not wanted:
        return 0
    activities = wf.search_activities([accounts.cash, accounts.portfolio], date(1970, 1, 1), date(2100, 1, 1))
    transfers = [(_when(a), {i for i in ISIN.findall(a.get("comment") or "") if valid_isin(i)})
                 for a in activities if a.get("activityType") in ("TRANSFER_IN", "TRANSFER_OUT")]
    found: dict[str, dict[str, dict]] = {}  # ISIN -> asset id -> entry
    for a in activities:
        asset_id = a.get("assetId") or ""
        if a.get("activityType") not in ("BUY", "SELL") or not asset_id or is_cash_symbol(asset_id):
            continue
        when = _when(a)
        named = set().union(*(names for t, names in transfers
                              if abs((t - when).total_seconds()) <= LEG_SECONDS)) & wanted
        if len(named) != 1:
            continue
        found.setdefault(named.pop(), {})[asset_id] = {
            "asset_id": asset_id, "symbol": a.get("assetSymbol") or asset_id,
            "exchange_mic": a.get("exchangeMic") or None, "name": a.get("assetName") or None}
    learned = 0
    for isin, assets in found.items():
        if len(assets) == 1:
            entry = next(iter(assets.values()))
            state.set_asset(broker, isin, **entry)
            learned += 1
    return learned


def mappings(state: State, broker: str) -> dict[str, SecurityMapping]:
    return {isin: SecurityMapping(a["symbol"], a["exchange_mic"], a["name"], a["asset_id"])
            for isin, a in state.assets(broker).items()}
