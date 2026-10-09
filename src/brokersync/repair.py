"""Repairs the cash activities versions before 0.3.6 created with a ``$CASH`` asset.

The sync sent every cash activity with ``asset: {symbol: "$CASH-<ccy>"}``.
Wealthfolio books a TRANSFER_IN/TRANSFER_OUT that carries an asset as a
*securities* transfer of that asset: no money moves. So the transfers between
the securities and the cash account did nothing - a buy was paid from the
securities account's own cash, sale proceeds and dividends stayed there, and
the cash account showed a position "$CASH".

The repair updates the sync's own activities (``[SYNC <broker>:…]``) that
carry such an asset to "no asset" (``PUT /activities`` with ``asset: {}``) -
not deleted and created again, so their ids, the sync state and the transfer
pairs stay. Wealthfolio recalculates the portfolio after each update. Runs
once per broker; one that had errors runs again on the next sync.
"""

from __future__ import annotations

import logging
from datetime import date

from .duplicates import REF
from .mapping import Accounts, is_cash_symbol
from .wealthfolio import WealthfolioClient, WealthfolioError

log = logging.getLogger(__name__)

SECURITY_TYPES = ("BUY", "SELL", "DIVIDEND")
FIELDS = {"accountId": "accountId", "activityType": "activityType", "subtype": "subtype", "date": "activityDate",
          "currency": "currency", "quantity": "quantity", "unitPrice": "unitPrice", "amount": "amount",
          "fee": "fee", "tax": "tax", "comment": "comment"}


def with_cash_asset(a: dict) -> bool:
    return is_cash_symbol(a.get("assetSymbol")) or is_cash_symbol(a.get("assetId"))


def find(wf: WealthfolioClient, broker: str, accounts: Accounts) -> list[dict]:
    """The broker's sync activities that still carry a ``$CASH`` asset."""
    activities = wf.search_activities([accounts.cash, accounts.portfolio], date(1970, 1, 1), date(2100, 1, 1))
    out = []
    for a in activities:
        m = REF.search(a.get("comment") or "")
        if m and m.group(1) == broker and a.get("activityType") not in SECURITY_TYPES and with_cash_asset(a):
            out.append(a)
    return out


def payload(a: dict) -> dict:
    """The update for one activity: unchanged, without the asset."""
    p = {"id": a["id"], "asset": {}}
    p.update({new: a[old] for old, new in FIELDS.items() if a.get(old) is not None})
    return p


def repair(wf: WealthfolioClient, broker: str, accounts: Accounts) -> tuple[int, list[str]]:
    """Remove the ``$CASH`` asset from the broker's sync activities; returns (updated, errors)."""
    updated = 0
    errors: list[str] = []
    for a in find(wf, broker, accounts):
        try:
            wf.update_activity(payload(a))
        except WealthfolioError as e:
            errors.append(f"{(a.get('date') or '')[:10]} {a.get('activityType')} {a.get('amount')}: {e}")
            continue
        updated += 1
    if updated:
        log.info("%s: removed the $CASH asset from %d activities", broker, updated)
    return updated, errors
