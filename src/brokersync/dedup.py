"""Finds transactions that are already in Wealthfolio.

Checked against the activities of the broker's accounts:

- **Synced before** (e.g. the sync state was lost or a run was interrupted):
  activities carrying this transaction's ``[SYNC <broker>:<id>]`` reference
  are left to Wealthfolio's own duplicate check - see ``find``.
- **Imported by the Broker Importer addon** from the CSV export or a PDF
  statement: those have other comments and times, so they are matched by
  substance, like the addon's ``matchExisting``: same account, type and
  security, at most 36 hours apart, same share count (not for dividends) and
  the same amount within 0.02. Cash-only activities (deposits, interest, …)
  match on account, type and amount in the same window.

Each existing activity matches at most one transaction.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation

from .mapping import CASH_SYMBOL_PREFIX

MATCH_WINDOW_SECONDS = 36 * 60 * 60
AMOUNT_TOLERANCE = Decimal("0.02")
MAIN_TYPES = {"BUY", "SELL", "DIVIDEND", "DEPOSIT", "WITHDRAWAL", "INTEREST", "FEE", "TAX", "CREDIT"}


def _dec(v) -> Decimal | None:
    if v is None or v == "":
        return None
    try:
        return abs(Decimal(str(v)))
    except InvalidOperation:
        return None


def _when(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def main_activity(payloads: list[dict]) -> dict:
    """The activity that stands for the transaction (not a transfer leg or refund credit)."""
    for p in payloads:
        if p["activityType"] in ("BUY", "SELL", "DIVIDEND"):
            return p
    return payloads[0]


class ExistingIndex:
    def __init__(self, existing: list[dict]):
        self.existing = existing
        self.used: set[str] = set()

    def find(self, ref: str, payloads: list[dict]) -> str | None:
        """Id of the existing activity (from another source) this transaction already is, or None.

        Activities this service created itself (they carry ``ref``) are not a
        match: the sync re-sends the whole transaction and Wealthfolio rejects
        the parts that exist as duplicates, so an interrupted run is completed.
        """
        tag = ref.strip()
        for e in self.existing:
            if tag in (e.get("comment") or ""):
                self.used.add(e["id"])
        a = main_activity(payloads)
        if a["activityType"] not in MAIN_TYPES:
            return None
        when = _when(a["activityDate"])
        amount = _dec(a.get("amount")) or Decimal(0)
        quantity = _dec(a.get("quantity"))
        symbol = a.get("asset", {}).get("symbol", "")
        is_cash = symbol.startswith(CASH_SYMBOL_PREFIX)
        for e in self.existing:
            if e["id"] in self.used or e.get("accountId") != a["accountId"]:
                continue
            if e.get("activityType") != a["activityType"] or (e.get("subtype") or None) != a.get("subtype"):
                continue
            if not is_cash and e.get("assetSymbol") != symbol and e.get("assetId") != symbol:
                continue
            if abs((_when(e["date"]) - when).total_seconds()) > MATCH_WINDOW_SECONDS:
                continue
            existing_amount = _dec(e.get("amount"))
            if existing_amount is None:
                q, p = _dec(e.get("quantity")), _dec(e.get("unitPrice"))
                existing_amount = q * p if q is not None and p is not None else None
            if existing_amount is None or abs(existing_amount - amount) > AMOUNT_TOLERANCE:
                continue
            if a["activityType"] in ("BUY", "SELL") and _dec(e.get("quantity")) != quantity:
                continue
            self.used.add(e["id"])
            return e["id"]
        return None
