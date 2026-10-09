"""Finds transactions that are already in Wealthfolio.

Checked against the activities of the broker's accounts:

- **Synced before** (e.g. the sync state was lost or a run was interrupted):
  activities carrying this transaction's ``[SYNC <broker>:<id>]`` reference
  are left to Wealthfolio's own duplicate check - see ``find``.
- **Imported by the Broker Importer addon** from the CSV export or a PDF
  statement: those have other comments and times, so they are matched by
  substance, like the addon's ``matchExisting``: same account and type, at
  most 36 hours apart, same share count (not for dividends) and the same
  amount within 0.02. The security should match too, but the addon books it
  under the ticker the user mapped while the sync uses the ISIN: so a trade
  with a different symbol still matches, a dividend only if it is the only
  candidate. Cash-only activities match on account, type and amount.

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

        def same_substance(e: dict) -> bool:
            if e["id"] in self.used or e.get("accountId") != a["accountId"]:
                return False
            if e.get("activityType") != a["activityType"] or (e.get("subtype") or None) != a.get("subtype"):
                return False
            if abs((_when(e["date"]) - when).total_seconds()) > MATCH_WINDOW_SECONDS:
                return False
            existing_amount = _dec(e.get("amount"))
            if existing_amount is None:
                q, p = _dec(e.get("quantity")), _dec(e.get("unitPrice"))
                existing_amount = q * p if q is not None and p is not None else None
            if existing_amount is None or abs(existing_amount - amount) > AMOUNT_TOLERANCE:
                return False
            return a["activityType"] not in ("BUY", "SELL") or _dec(e.get("quantity")) == quantity

        def same_security(e: dict) -> bool:
            return is_cash or symbol in (e.get("assetSymbol"), e.get("assetId"))

        candidates = [e for e in self.existing if same_substance(e)]
        # Same security first. The addon books a security under the ticker the
        # user mapped it to, the sync under the ISIN, so a different symbol is
        # still a match when nothing else is - for trades the share count and
        # amount within a day and a half pin it down.
        match = next((e for e in candidates if same_security(e)), None)
        if match is None and not is_cash and (a["activityType"] in ("BUY", "SELL") or len(candidates) == 1):
            match = candidates[0] if candidates else None
        if match is None:
            return None
        self.used.add(match["id"])
        return match["id"]

    def find_settlement(self, cash_account: str, signed: Decimal, when: datetime, days: int) -> str | None:
        """The transfer leg the securities side booked on the cash account for a bank settlement.

        A debit (``signed`` < 0) pays a buy: TRANSFER_OUT to the depot. A credit
        is the proceeds of a sale or a payout: TRANSFER_IN from the depot.
        """
        want = "TRANSFER_OUT" if signed < 0 else "TRANSFER_IN"
        amount = abs(signed)
        best = None
        for e in self.existing:
            if e["id"] in self.used or e.get("accountId") != cash_account or e.get("activityType") != want:
                continue
            existing_amount = _dec(e.get("amount"))
            if existing_amount is None or abs(existing_amount - amount) > AMOUNT_TOLERANCE:
                continue
            gap = abs((_when(e["date"]) - when).total_seconds())
            if gap > days * 86400:
                continue
            if best is None or gap < best[0]:
                best = (gap, e["id"])
        if best is None:
            return None
        self.used.add(best[1])
        return best[1]
