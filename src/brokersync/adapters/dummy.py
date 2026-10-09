"""A broker that doesn't exist, for trying the service end to end.

It asks for a confirmation code on the first login (like a real TAN), then
returns a few fabricated transactions: a deposit, a buy, a dividend and one
event type it doesn't know. Point it at test accounts in Wealthfolio.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from ..model import BrokerAccount, CashBalance, Kind, Position, Transaction
from .base import AdapterError, AuthRequired, BrokerAdapter, Challenge, CredentialField

CODE = "000000"
ISIN = "IE00B4L5Y983"
NAME = "Testfonds World (Dummy)"


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=UTC)


TRANSACTIONS = [
    Transaction(
        id="dummy-0001",
        kind=Kind.DEPOSIT,
        datetime=_dt("2026-01-02T09:00:00"),
        currency="EUR",
        net=Decimal("1000"),
        label="Einzahlung",
    ),
    Transaction(
        id="dummy-0002",
        kind=Kind.BUY,
        datetime=_dt("2026-01-05T10:15:00"),
        currency="EUR",
        net=Decimal("501"),
        isin=ISIN,
        name=NAME,
        shares=Decimal("5"),
        gross=Decimal("500"),
        fee=Decimal("1"),
        label="Kauf",
    ),
    Transaction(
        id="dummy-0003",
        kind=Kind.DIVIDEND,
        datetime=_dt("2026-03-16T11:00:00"),
        currency="EUR",
        net=Decimal("3.68"),
        isin=ISIN,
        name=NAME,
        shares=Decimal("5"),
        gross=Decimal("5"),
        tax=Decimal("1.32"),
        label="Ausschüttung",
    ),
    Transaction(
        id="dummy-0004",
        kind=Kind.UNKNOWN,
        datetime=_dt("2026-03-20T08:00:00"),
        currency="EUR",
        net=Decimal("0"),
        label="Unbekanntes Ereignis",
        raw_type="DUMMY_SPECIAL_EVENT",
        raw={"eventType": "DUMMY_SPECIAL_EVENT", "note": "fabricated"},
    ),
]


class DummyAdapter(BrokerAdapter):
    key = "dummy"
    label = "Dummy (Test)"
    credential_fields = [
        CredentialField("username", "Benutzername", help="Beliebig - der Dummy prüft nichts."),
    ]

    def login(self) -> None:
        if self.credentials.get("fail") == "yes":
            raise AdapterError("Dummy broker is down (simulated)")
        if not self.session.get("confirmed"):
            raise AuthRequired(Challenge("code", f"Gib den Bestätigungscode {CODE} ein."))

    def complete_login(self, code: str) -> None:
        if code.strip() != CODE:
            raise AuthRequired(Challenge("code", f"Falscher Code. Gib {CODE} ein."))
        self.session["confirmed"] = True

    @classmethod
    def replay(cls, recording: dict) -> DummyAdapter:
        return cls(recording.get("credentials", {}), {"confirmed": True})

    def get_accounts(self) -> list[BrokerAccount]:
        return [BrokerAccount("dummy-1", "Dummy-Depot", "EUR")]

    def get_positions(self) -> list[Position]:
        return [Position(ISIN, NAME, Decimal("5"), "EUR", Decimal("510"))]

    def get_transactions(self, since: datetime | None) -> list[Transaction]:
        return [t for t in TRANSACTIONS if since is None or t.datetime >= since]

    def get_cash(self) -> list[CashBalance]:
        return [CashBalance("EUR", Decimal("502.68"))]
