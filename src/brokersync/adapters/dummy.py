"""A broker that doesn't exist, for trying the service end to end.

It asks for a confirmation code on the first login (like a real TAN), then
returns a few fabricated transactions: a deposit, a buy, a dividend and one
event type it doesn't know. Point it at test accounts in Wealthfolio.

Test data in a real Wealthfolio must be easy to find and remove: every
transaction is dated on the day of the login (a few hours before it) and
carries "TEST" in its text. The date is fixed at the login and kept in the
session, so the daily timer doesn't book a new set every day; logging in to
the dummy again on another day books a new set.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from ..model import BrokerAccount, CashBalance, Kind, Position, Transaction
from .base import AdapterError, AuthRequired, BrokerAdapter, Challenge, CredentialField

CODE = "000000"
ISIN = "IE00B4L5Y983"
NAME = "TEST Fonds World (Dummy)"
BERLIN = ZoneInfo("Europe/Berlin")


def transactions(anchor: datetime) -> list[Transaction]:
    """The dummy's transactions for a login at ``anchor``: same day (German time), before it."""
    local = anchor.astimezone(BERLIN)
    day = local.strftime("%Y%m%d")
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)

    def at(hours_before: float) -> datetime:
        # Within 4 hours after midnight there's no room for that: spread them
        # over the time since midnight instead, in the same order.
        elapsed = local - start
        if elapsed >= timedelta(hours=4):
            t = local - timedelta(hours=hours_before)
        else:
            t = start + elapsed * (4 - hours_before) / 4
        return t.astimezone(UTC)

    return [
        Transaction(
            id=f"TEST-{day}-1",
            kind=Kind.DEPOSIT,
            datetime=at(3),
            currency="EUR",
            net=Decimal("1000"),
            label="TEST Einzahlung",
        ),
        Transaction(
            id=f"TEST-{day}-2",
            kind=Kind.BUY,
            datetime=at(2),
            currency="EUR",
            net=Decimal("501"),
            isin=ISIN,
            name=NAME,
            shares=Decimal("5"),
            gross=Decimal("500"),
            fee=Decimal("1"),
            label="TEST Kauf",
        ),
        Transaction(
            id=f"TEST-{day}-3",
            kind=Kind.DIVIDEND,
            datetime=at(1),
            currency="EUR",
            net=Decimal("3.68"),
            isin=ISIN,
            name=NAME,
            shares=Decimal("5"),
            gross=Decimal("5"),
            tax=Decimal("1.32"),
            label="TEST Ausschüttung",
        ),
        Transaction(
            id=f"TEST-{day}-4",
            kind=Kind.UNKNOWN,
            datetime=at(0.5),
            currency="EUR",
            net=Decimal("0"),
            label="TEST Unbekanntes Ereignis",
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
        # Sessions from before the dates were tied to the login.
        self.session.setdefault("anchor", datetime.now(UTC).isoformat())

    def complete_login(self, code: str) -> None:
        if code.strip() != CODE:
            raise AuthRequired(Challenge("code", f"Falscher Code. Gib {CODE} ein."))
        self.session["confirmed"] = True
        self.session["anchor"] = datetime.now(UTC).isoformat()

    @classmethod
    def replay(cls, recording: dict) -> DummyAdapter:
        return cls(recording.get("credentials", {}), {"confirmed": True, "anchor": recording["anchor"]})

    def _anchor(self) -> datetime:
        return datetime.fromisoformat(self.session["anchor"])

    def get_accounts(self) -> list[BrokerAccount]:
        return [BrokerAccount("dummy-1", "Dummy-Depot", "EUR")]

    def get_positions(self) -> list[Position]:
        return [Position(ISIN, NAME, Decimal("5"), "EUR", Decimal("510"))]

    def get_transactions(self, since: datetime | None) -> list[Transaction]:
        return [t for t in transactions(self._anchor()) if since is None or t.datetime >= since]

    def get_cash(self) -> list[CashBalance]:
        return [CashBalance("EUR", Decimal("502.68"))]
