"""The interface every broker adapter implements.

Adapters only read: there is no method that places orders, moves money or
changes anything at the broker, and an adapter must not call such endpoints
(AC 7 of #35).

Login is a two-step dance because brokers ask for a second factor:

1. ``login()`` uses the stored session if it is still valid. If the broker
   needs the user (TAN, SMS code, app confirmation), it raises
   ``AuthRequired`` with a ``Challenge`` that the web UI shows.
2. ``complete_login(code)`` finishes the login with what the user entered
   (an empty string for "I confirmed it in the app").

After a successful login ``session_state()`` returns what is needed to log in
again without the user (cookies, tokens); the sync stores it encrypted and
passes it to the next adapter instance.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar

from ..model import BrokerAccount, CashBalance, Position, Transaction


@dataclass(frozen=True)
class CredentialField:
    name: str
    label: str
    secret: bool = False
    help: str = ""


@dataclass(frozen=True)
class Challenge:
    # "code": the user types a code; "confirm": the user confirms in an app.
    kind: str
    message: str


class AuthRequired(Exception):
    """The broker needs the user: show ``challenge`` and call ``complete_login``."""

    def __init__(self, challenge: Challenge):
        super().__init__(challenge.message)
        self.challenge = challenge


class AdapterError(Exception):
    """The broker could not be read (down, changed API, wrong credentials)."""


class BrokerAdapter(ABC):
    key: ClassVar[str]
    label: ClassVar[str]
    credential_fields: ClassVar[list[CredentialField]] = []
    # True if get_positions() is the complete list of holdings: then the sync
    # compares it with Wealthfolio (a missing position counts as 0 shares).
    reports_positions: ClassVar[bool] = False

    def __init__(self, credentials: dict[str, str], session: dict | None = None):
        self.credentials = credentials
        self.session = dict(session or {})
        # Set by the sync: tells the user to act now (e.g. confirm in the bank's
        # app) while the adapter waits. Unset in the web UI, which shows the
        # challenge itself.
        self.on_user_action: Callable[[str], None] | None = None

    def close(self) -> None:  # noqa: B027 - optional hook, most adapters hold no connection
        """End the connection; afterwards ``session_state()`` is final."""

    @abstractmethod
    def login(self) -> None:
        """Log in with the stored session or credentials; raise ``AuthRequired`` if the user is needed."""

    def complete_login(self, code: str) -> None:
        """Finish a login that raised ``AuthRequired``."""
        raise AdapterError(f"{self.label} has no second login step")

    def session_state(self) -> dict:
        """What the next run needs to log in without the user. Stored encrypted."""
        return self.session

    @classmethod
    def replay(cls, recording: dict) -> BrokerAdapter:
        """An instance that answers from a recording instead of the broker (contract tests).

        Adapters talking HTTP build it on a transport that replays the recorded
        responses; see tests/contract/README.md.
        """
        raise NotImplementedError(f"{cls.__name__} has no replay support")

    @abstractmethod
    def get_accounts(self) -> list[BrokerAccount]: ...

    @abstractmethod
    def get_positions(self) -> list[Position]: ...

    @abstractmethod
    def get_transactions(self, since: datetime | None) -> list[Transaction]:
        """All transactions booked at or after ``since`` (everything if ``None``)."""

    @abstractmethod
    def get_cash(self) -> list[CashBalance]: ...
