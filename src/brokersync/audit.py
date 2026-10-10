"""Checks a broker's two Wealthfolio accounts for bookings that don't add up - read only.

The two-account model (``brokersync.mapping``) keeps the securities account
free of cash: every buy is funded by a transfer from the cash account, every
sale and payout is swept back. When one side of that is missing - deleted by
hand, imported without its transfer, booked twice - the securities account
shows cash and the cash account the opposite. This finds where:

- **Depot moments that leave cash:** the securities account's activities,
  grouped by time (the transfer legs lie a few seconds from their trade), whose
  cash effect doesn't net to zero - e.g. a buy without its funding transfer,
  or a sweep whose sale was deleted.
- **Transfers without a partner:** a transfer on the securities account with
  no matching opposite transfer on the cash account (same ``sourceGroupId``, or
  the same amount within a minute), and an internal transfer on the cash
  account ("Portfolio" in its comment) with none on the securities account.

Nothing is changed; the user fixes it in Wealthfolio.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from .mapping import Accounts, is_cash_symbol
from .wealthfolio import WealthfolioClient

TOLERANCE = Decimal("0.02")
MOMENT_SECONDS = 10
PARTNER_SECONDS = 60
# Cash effect of each type on its own account, as Wealthfolio books ``amount``.
SIGN = {"DEPOSIT": 1, "TRANSFER_IN": 1, "SELL": 1, "DIVIDEND": 1, "INTEREST": 1, "CREDIT": 1,
        "WITHDRAWAL": -1, "TRANSFER_OUT": -1, "BUY": -1, "FEE": -1, "TAX": -1}


def _dec(v) -> Decimal:
    try:
        return Decimal(str(v)) if v not in (None, "") else Decimal(0)
    except InvalidOperation:
        return Decimal(0)


def _when(a: dict) -> datetime:
    return datetime.fromisoformat(str(a.get("date")).replace("Z", "+00:00"))


def is_cash_transfer(a: dict) -> bool:
    """A transfer of money - one with a (non-cash) asset moves securities, not cash."""
    if a.get("activityType") not in ("TRANSFER_IN", "TRANSFER_OUT"):
        return False
    asset = a.get("assetId") or a.get("assetSymbol") or ""
    return not asset or is_cash_symbol(asset)


def cash_effect(a: dict) -> Decimal:
    t = a.get("activityType") or ""
    if t in ("TRANSFER_IN", "TRANSFER_OUT") and not is_cash_transfer(a):
        return Decimal(0)
    return SIGN.get(t, 0) * abs(_dec(a.get("amount")))


def source(a: dict) -> str:
    return "Dienst" if "[SYNC " in (a.get("comment") or "") else "Import / von Hand"


@dataclass
class Moment:
    """Activities on the securities account at one point in time that leave cash behind."""
    activities: list[dict]
    net: Decimal


@dataclass
class Report:
    depot_cash: Decimal = Decimal(0)
    cash: Decimal = Decimal(0)
    moments: list[Moment] = field(default_factory=list)
    lonely: list[dict] = field(default_factory=list)  # transfers without a partner

    @property
    def moments_total(self) -> Decimal:
        return sum((m.net for m in self.moments), Decimal(0))


def check(activities: list[dict], accounts: Accounts) -> Report:
    depot = sorted((a for a in activities if a.get("accountId") == accounts.portfolio), key=_when)
    cash = [a for a in activities if a.get("accountId") == accounts.cash]
    report = Report(depot_cash=sum((cash_effect(a) for a in depot), Decimal(0)),
                    cash=sum((cash_effect(a) for a in cash), Decimal(0)))

    # Depot moments: activities less than MOMENT_SECONDS apart belong together.
    group: list[dict] = []
    for a in [*depot, None]:
        if group and (a is None or (_when(a) - _when(group[-1])).total_seconds() > MOMENT_SECONDS):
            net = sum((cash_effect(x) for x in group), Decimal(0))
            if abs(net) > TOLERANCE:
                report.moments.append(Moment(group, net))
            group = []
        if a is not None:
            group.append(a)

    # Transfers between the two accounts without their other leg.
    used: set[str] = set()

    def partner(a: dict, others: list[dict]) -> dict | None:
        want = "TRANSFER_OUT" if a["activityType"] == "TRANSFER_IN" else "TRANSFER_IN"
        amount = abs(_dec(a.get("amount")))
        group_id = a.get("sourceGroupId")
        best = None
        for o in others:
            if o["id"] in used or o.get("activityType") != want or not is_cash_transfer(o):
                continue
            if abs(abs(_dec(o.get("amount"))) - amount) > TOLERANCE:
                continue
            if group_id and o.get("sourceGroupId") == group_id:
                return o
            gap = abs((_when(o) - _when(a)).total_seconds())
            if gap <= PARTNER_SECONDS and (best is None or gap < best[0]):
                best = (gap, o)
        return best[1] if best else None

    for a in depot:
        if not is_cash_transfer(a):
            continue
        p = partner(a, cash)
        if p:
            used.add(p["id"])
        else:
            report.lonely.append(a)
    for a in cash:
        if is_cash_transfer(a) and a["id"] not in used and "portfolio" in (a.get("comment") or "").lower():
            report.lonely.append(a)
    report.lonely.sort(key=_when)
    return report


def run(wf: WealthfolioClient, accounts: Accounts) -> Report:
    activities = wf.search_activities([accounts.cash, accounts.portfolio], date(1970, 1, 1), date(2100, 1, 1))
    return check(activities, accounts)
