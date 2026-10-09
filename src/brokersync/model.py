"""What a broker reports, independent of the broker.

Adapters (``brokersync.adapters``) turn a broker's API answers into these
types; ``brokersync.mapping`` turns them into Wealthfolio activities. Amounts
are ``Decimal`` throughout - never floats - so the activity amounts are exact.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum


class Kind(StrEnum):
    BUY = "BUY"
    SELL = "SELL"
    DIVIDEND = "DIVIDEND"
    INTEREST = "INTEREST"
    DEPOSIT = "DEPOSIT"
    WITHDRAWAL = "WITHDRAWAL"
    FEE = "FEE"
    TAX = "TAX"
    TAX_REFUND = "TAX_REFUND"
    # An event type the adapter doesn't know. It is never dropped: the sync
    # stores and reports it (AC 10), and nothing is booked for it.
    UNKNOWN = "UNKNOWN"


SECURITY_KINDS = {Kind.BUY, Kind.SELL, Kind.DIVIDEND}


@dataclass(frozen=True)
class BrokerAccount:
    id: str
    name: str
    currency: str


@dataclass(frozen=True)
class Position:
    isin: str
    name: str
    shares: Decimal
    currency: str
    # Market value as reported by the broker, if it reports one.
    value: Decimal | None = None


@dataclass(frozen=True)
class CashBalance:
    currency: str
    amount: Decimal


@dataclass(frozen=True)
class Transaction:
    # The broker's own, stable reference for this transaction. Part of the
    # activity comments and of the sync state, so the same transaction is
    # never booked twice.
    id: str
    kind: Kind
    # Timezone-aware; trades at execution time, income at the payment date.
    datetime: datetime
    # Booking currency of the cash account.
    currency: str
    # The amount booked on the cash account, always positive.
    net: Decimal
    isin: str | None = None
    name: str = ""
    shares: Decimal | None = None
    # Market value of a trade or gross income of a dividend/interest payment.
    gross: Decimal | None = None
    fee: Decimal = Decimal(0)
    # Income taxes: positive = charged, negative = refunded.
    tax: Decimal = Decimal(0)
    # The broker's own type text, e.g. "Kauf", "Sparplan" - for comments.
    label: str = ""
    # The broker's raw event type, shown for UNKNOWN events.
    raw_type: str = ""
    # Original payload of an UNKNOWN event, stored for the report (never secrets).
    raw: dict = field(default_factory=dict, compare=False)


def to_dict(tx: Transaction) -> dict:
    """JSON-friendly form of a transaction (for contract tests and reports)."""
    out = {}
    for name in Transaction.__dataclass_fields__:
        v = getattr(tx, name)
        if name == "raw" or v is None or v == "":
            continue
        if isinstance(v, Decimal):
            if name in ("fee", "tax") and v == 0:
                continue
            v = format(v.normalize(), "f")
        elif isinstance(v, datetime):
            v = v.isoformat()
        elif isinstance(v, Kind):
            v = v.value
        out[name] = v
    return out
