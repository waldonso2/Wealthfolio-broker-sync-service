"""A fabricated broker for the tests - never shipped (``tests/`` isn't installed).

It asks for a confirmation code on the first login (like a real TAN), then
returns a deposit, a buy, a dividend and one event type it doesn't know. The
transactions lie on the day of the login (German time), a few hours before
it; the login time is kept in the session, so a second run books nothing new.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from brokersync.adapters.base import AdapterError, AuthRequired, BrokerAdapter, Challenge, CredentialField
from brokersync.model import BrokerAccount, CashBalance, Kind, Position, Transaction

CODE = "000000"
ISIN = "IE00B4L5Y983"
NAME = "TEST Fonds World"
BERLIN = ZoneInfo("Europe/Berlin")


def transactions(anchor: datetime) -> list[Transaction]:
    """The transactions for a login at ``anchor``: same day (German time), before it."""
    local = anchor.astimezone(BERLIN)
    day = local.strftime("%Y%m%d")
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)

    def at(hours_before: float) -> datetime:
        # Within 4 hours after midnight: spread them over the time since midnight.
        elapsed = local - start
        if elapsed >= timedelta(hours=4):
            t = local - timedelta(hours=hours_before)
        else:
            t = start + elapsed * (4 - hours_before) / 4
        return t.astimezone(UTC)

    return [
        Transaction(id=f"TEST-{day}-1", kind=Kind.DEPOSIT, datetime=at(3), currency="EUR", net=Decimal("1000"),
                    label="TEST Einzahlung"),
        Transaction(id=f"TEST-{day}-2", kind=Kind.BUY, datetime=at(2), currency="EUR", net=Decimal("501"),
                    isin=ISIN, name=NAME, shares=Decimal("5"), gross=Decimal("500"), fee=Decimal("1"),
                    label="TEST Kauf"),
        Transaction(id=f"TEST-{day}-3", kind=Kind.DIVIDEND, datetime=at(1), currency="EUR", net=Decimal("3.68"),
                    isin=ISIN, name=NAME, shares=Decimal("5"), gross=Decimal("5"), tax=Decimal("1.32"),
                    label="TEST Ausschüttung"),
        Transaction(id=f"TEST-{day}-4", kind=Kind.UNKNOWN, datetime=at(0.5), currency="EUR", net=Decimal("0"),
                    label="TEST Unbekanntes Ereignis", raw_type="FAKE_SPECIAL_EVENT",
                    raw={"eventType": "FAKE_SPECIAL_EVENT", "note": "fabricated"}),
    ]


class FakeBroker(BrokerAdapter):
    key = "fake"
    label = "Testbroker"
    credential_fields = [CredentialField("username", "Benutzername")]
    reports_positions = True

    def login(self) -> None:
        if self.credentials.get("fail") == "yes":
            raise AdapterError("Fake broker is down (simulated)")
        if not self.session.get("confirmed"):
            raise AuthRequired(Challenge("code", f"Gib den Bestätigungscode {CODE} ein."))
        self.session.setdefault("anchor", datetime.now(UTC).isoformat())

    def complete_login(self, code: str) -> None:
        if code.strip() != CODE:
            raise AuthRequired(Challenge("code", f"Falscher Code. Gib {CODE} ein."))
        self.session["confirmed"] = True
        self.session["anchor"] = datetime.now(UTC).isoformat()

    def _anchor(self) -> datetime:
        return datetime.fromisoformat(self.session["anchor"])

    def get_accounts(self) -> list[BrokerAccount]:
        return [BrokerAccount("fake-1", "Testdepot", "EUR")]

    def get_positions(self) -> list[Position]:
        return [Position(ISIN, NAME, Decimal("5"), "EUR", Decimal("510"))]

    def get_transactions(self, since: datetime | None) -> list[Transaction]:
        return [t for t in transactions(self._anchor()) if since is None or t.datetime >= since]

    def get_cash(self) -> list[CashBalance]:
        return [CashBalance("EUR", Decimal("502.68"))]
