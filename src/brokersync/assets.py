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
from decimal import Decimal, InvalidOperation

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
AMOUNT_TOLERANCE = Decimal("0.01")
# The reference a PDF import ends every activity of one statement with.
PDF_REF = re.compile(r"\[PDF [^\]]+\]")


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


def _entry(a: dict) -> dict:
    asset_id = a.get("assetId") or ""
    return {"asset_id": asset_id, "symbol": a.get("assetSymbol") or asset_id,
            "exchange_mic": a.get("exchangeMic") or None, "name": a.get("assetName") or None}


def _amount(a: dict) -> Decimal | None:
    try:
        return abs(Decimal(str(a.get("amount")))) if a.get("amount") not in (None, "") else None
    except InvalidOperation:
        return None


def _isins(text: str) -> set[str]:
    return {i for i in ISIN.findall(text) if valid_isin(i)}


def learn_from_imports(state: State, broker: str, wf: WealthfolioClient, accounts: Accounts,
                       isins: set[str], recheck: frozenset[str] | set[str] = frozenset()) -> int:
    """Learn the assets of ``isins`` (positions the broker reports whose asset is unknown or doesn't fit) from
    imported trades whose transfer names the ISIN; returns how many were learned or corrected.

    Exact first: a PDF import ends a trade's and its transfers' comments in the same ``[PDF <statement>]``;
    what that says replaces a stored asset. Then, for ISINs still unknown, by time and amount: the transfer
    within LEG_SECONDS with the trade's amount (a savings plan's executions share a second). An ISIN on
    trades of two assets stays unknown; what is learned narrows the next pass. ``recheck``: ISINs whose stored
    asset didn't fit last time - time and amount may replace it too (the sync's own trades name the ISIN in
    their transfers as well)."""
    known = state.assets(broker)
    activities = wf.search_activities([accounts.cash, accounts.portfolio], date(1970, 1, 1), date(2100, 1, 1))
    by_ref: dict[str, set[str]] = {}
    for a in activities:
        m = PDF_REF.search(a.get("comment") or "")
        if m and a.get("activityType") in ("TRANSFER_IN", "TRANSFER_OUT"):
            by_ref.setdefault(m.group(0), set()).update(_isins(a.get("comment") or ""))
    exact: dict[str, dict[str, dict]] = {}  # ISIN -> asset id -> entry
    for a in activities:
        m = PDF_REF.search(a.get("comment") or "")
        named = by_ref.get(m.group(0), set()) & isins if m else set()
        if len(named) == 1 and a.get("activityType") in ("BUY", "SELL") and a.get("assetId") \
                and not is_cash_symbol(a.get("assetId")):
            exact.setdefault(next(iter(named)), {})[a["assetId"]] = _entry(a)
    corrected = 0
    for isin, assets in exact.items():
        if len(assets) == 1:
            entry = next(iter(assets.values()))
            if (known.get(isin) or {}).get("asset_id") != entry["asset_id"]:
                state.set_asset(broker, isin, **entry)
                known[isin] = entry
                corrected += 1
    wanted = {i for i in isins if i not in exact and (i in recheck or not _preferred(i, known.get(i)))}
    if not wanted:
        return corrected
    transfers = [(_when(a), _amount(a), _isins(a.get("comment") or ""))
                 for a in activities if a.get("activityType") in ("TRANSFER_IN", "TRANSFER_OUT")]
    trades = []  # (asset entry, ISINs its transfers name)
    for a in activities:
        asset_id = a.get("assetId") or ""
        if a.get("activityType") not in ("BUY", "SELL") or not asset_id or is_cash_symbol(asset_id):
            continue
        when, amount = _when(a), _amount(a)
        near = [(t_amount, names) for t, t_amount, names in transfers
                if names and abs((t - when).total_seconds()) <= LEG_SECONDS]
        same = [names for t_amount, names in near
                if amount is not None and t_amount is not None and abs(t_amount - amount) <= AMOUNT_TOLERANCE]
        named = set().union(*(same or [names for _, names in near]))
        if named:
            trades.append((_entry(a), named))
    learned, taken = corrected, {e["asset_id"] for i, e in known.items() if i not in wanted}
    while True:
        found: dict[str, dict[str, dict]] = {}  # ISIN -> asset id -> entry
        for entry, named in trades:
            left = named & wanted
            if len(left) == 1 and entry["asset_id"] not in taken:
                found.setdefault(next(iter(left)), {})[entry["asset_id"]] = entry
        new = {isin: next(iter(assets.values())) for isin, assets in found.items() if len(assets) == 1}
        if not new:
            return learned
        for isin, entry in new.items():
            if (known.get(isin) or {}).get("asset_id") != entry["asset_id"]:
                state.set_asset(broker, isin, **entry)
                learned += 1
            wanted.discard(isin)
            taken.add(entry["asset_id"])


def mappings(state: State, broker: str) -> dict[str, SecurityMapping]:
    return {isin: SecurityMapping(a["symbol"], a["exchange_mic"], a["name"], a["asset_id"])
            for isin, a in state.assets(broker).items()}
