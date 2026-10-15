"""Money from before what the sync reads: imports older than the bank account's history, opening positions.

Both become transactions the run books like any other (``sync.Syncer._before_history``, ``Syncer._openings``).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from . import audit as audit_mod
from . import coverage
from .mapping import Accounts
from .model import Kind, Transaction

# Days and start dates are German days (#108).
BERLIN = ZoneInfo("Europe/Berlin")


def opening_id(isin: str, day: str) -> str:
    return f"start-{isin}-{day}"


def money_before(activities: list[dict], accounts: Accounts, history: date,
                 synced: dict[str, tuple[str, list[str]]]) -> list[Transaction]:
    """Trades a CSV/PDF import booked before ``history`` (the earliest bank day read): their money never showed
    up on the cash account. A purchase gets a deposit of its cost just before its transfer to the depot, a sale a
    withdrawal of its proceeds just after (id ``pre-<activity id>`` - never change it); dividends keep their
    money. Not for transfers the sync matched to a bank booking (those were read), nor the sync's own."""
    matched = {i for _, ids in synced.values() for i in ids}
    trades = [a for a in activities if a.get("accountId") == accounts.portfolio
              and a.get("activityType") in ("BUY", "SELL")]
    first = datetime(history.year, history.month, history.day, tzinfo=BERLIN)
    out = []
    for a in activities:
        if a.get("accountId") != accounts.cash or not audit_mod.is_cash_transfer(a) or a.get("id") in matched \
                or coverage.SYNC_REF.search(a.get("comment") or ""):
            continue
        at = audit_mod._when(a)
        buy = a.get("activityType") == "TRANSFER_OUT"
        if at < first and _beside_trade(at, "BUY" if buy else "SELL", trades):
            out.append(_money_leg(a, at, buy))
    return out


def _beside_trade(at: datetime, kind: str, trades: list[dict]) -> bool:
    return any(t.get("activityType") == kind
               and abs((audit_mod._when(t) - at).total_seconds()) <= audit_mod.MOMENT_SECONDS for t in trades)


def _money_leg(a: dict, at: datetime, buy: bool) -> Transaction:
    return Transaction(
        id=f"pre-{a['id']}", kind=Kind.DEPOSIT if buy else Kind.WITHDRAWAL,
        datetime=at + timedelta(seconds=-3 if buy else 3), currency=a.get("currency") or "EUR",
        net=abs(audit_mod._dec(a.get("amount"))),
        label=("Deposit for a buy before the bank history read" if buy
               else "Withdrawal after a sale before the bank history read"))


def openings(rows: list[dict]) -> list[Transaction]:
    """Opening positions the user entered (page Prüfung): a buy at noon of that day, its cost deposited first
    (``external_cash``), id ``start-<isin>-<day>``."""
    out = []
    for o in rows:
        shares, price, fee = Decimal(o["shares"]), Decimal(o["price"]), Decimal(o["fee"])
        day = date.fromisoformat(o["day"])
        # Holdings read before 0.9.9 (python-fints' parser) could carry "<ISIN>|/DE/<WKN>" as ISIN: the id
        # keeps it (it is booked under it), the ISIN is the real one, so the holdings check finds it.
        isin = o["isin"].split("|")[0].strip()
        out.append(Transaction(
            id=opening_id(o["isin"], o["day"]), kind=Kind.BUY,
            datetime=datetime(day.year, day.month, day.day, 12, tzinfo=BERLIN).astimezone(UTC),
            currency=o["currency"], net=shares * price + fee, isin=isin, name=o["name"], shares=shares,
            gross=shares * price, fee=fee, label="Anfangsbestand", external_cash=True))
    return out
