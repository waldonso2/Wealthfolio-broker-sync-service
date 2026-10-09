"""DKB via FinTS (HBCI), read-only: balance, transactions and - if DKB offers
them over FinTS - depot holdings of the giro account.

- **Login:** Anmeldename and PIN of DKB online banking. FinTS needs strong
  customer authentication (SCA) when a dialog starts and, at many banks, for
  transactions older than 90 days. DKB confirms in the DKB app (decoupled):
  in the web UI the user clicks "confirmed"; during a scheduled run the
  adapter notifies the user (``on_user_action``) and waits a few minutes.
- **PIN safety:** after a rejected PIN the adapter never contacts the bank
  again until the credentials are saved anew - three wrong attempts lock the
  online banking.
- **Session:** python-fints' state (system id, bank and user parameters, chosen
  TAN method) is kept in the session, so a confirmed device stays known.
- **Product ID:** FinTS servers want a product ID registered with the Deutsche
  Kreditwirtschaft. Until this service has one, it is entered in the broker
  settings.
- **Securities:** debits/credits on the giro account for depot trades and
  payouts are reported as ``SECURITIES_CASH``: the PDF import (or later a depot
  adapter) books them as transfer pairs, the sync only checks they are there.

Bookings come as MT940 (or camt, which python-fints converts) records without a
stable id; the id is a hash of their content plus a counter for identical
bookings on the same day.
"""

from __future__ import annotations

import base64
import hashlib
import re
import time
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from ..model import BrokerAccount, CashBalance, Kind, Position, Transaction
from .base import AdapterError, AuthRequired, BrokerAdapter, Challenge, CredentialField

BLZ = "12030000"
SERVER = "https://fints.dkb.de/fints"
# The FinTS product ID registered for this service with the Deutsche
# Kreditwirtschaft; empty until it is assigned (then users need not enter one).
PRODUCT_ID = ""
BERLIN = ZoneInfo("Europe/Berlin")
# PSD2: banks may ask for a TAN for bookings older than this.
NO_TAN_DAYS = 89
# How long a scheduled run waits for a confirmation in the DKB app.
DECOUPLED_WAIT = 180
DECOUPLED_POLL = 5

# Posting text or purpose of a giro booking that settles a depot trade or payout.
# Keywords only - an ISIN pattern would also hit SEPA mandate references.
SECURITIES = re.compile(r"wertpapier|wp-abr|depot|ertragsgutschrift|dividende|aussch[uü]ttung|kupon", re.IGNORECASE)
INTEREST = re.compile(r"\bzins|habenzins|guthabenzins", re.IGNORECASE)
FEE = re.compile(r"entgelt|geb[uü]hr|kontof[uü]hrung|abschluss", re.IGNORECASE)


class DkbAdapter(BrokerAdapter):
    key = "dkb"
    label = "DKB"
    credential_fields = [
        CredentialField("username", "Anmeldename", help="Wie beim DKB-Banking im Browser."),
        CredentialField("pin", "PIN / Passwort", secret=True),
        CredentialField("iban", "IBAN des Girokontos",
                        help="Leer lassen, wenn du bei der DKB nur ein Konto hast."),
        CredentialField("product_id", "FinTS-Produkt-ID",
                        help="Wird bald fest eingebaut. Bis dahin hier die registrierte Produkt-ID eintragen."),
    ]

    # Replaced in tests with a fake; real runs use python-fints.
    client_factory = None

    def __init__(self, credentials, session=None):
        super().__init__(credentials, session)
        self._client = None
        self._pending = None
        self._open = False

    # ── login ───────────────────────────────────────────────────────────────
    def _make_client(self):
        product_id = (self.credentials.get("product_id") or PRODUCT_ID).strip()
        if not product_id:
            raise AdapterError(
                "Für die DKB fehlt die FinTS-Produkt-ID. Trage sie in den Broker-Einstellungen ein."
            )
        if not self.credentials.get("username") or not self.credentials.get("pin"):
            raise AdapterError("Anmeldename und PIN fehlen - bitte in den Broker-Einstellungen eintragen.")
        data = base64.b64decode(self.session["fints"]) if self.session.get("fints") else None
        factory = type(self).client_factory or _fints_client
        return factory(BLZ, self.credentials["username"], self.credentials["pin"], SERVER, product_id, data)

    def login(self) -> None:
        if self.session.get("pin_rejected"):
            raise AdapterError(
                "Die DKB hat Anmeldename oder PIN abgelehnt. Bitte in den Broker-Einstellungen neu eintragen - "
                "bis dahin versucht der Dienst es nicht noch einmal, damit das Online-Banking nicht gesperrt wird."
            )
        self._client = self._make_client()
        try:
            if not self._client.get_current_tan_mechanism():
                self._client.fetch_tan_mechanisms()
            if self._client.is_tan_media_required() and not self._client.selected_tan_medium:
                media = self._client.get_tan_media()[1]
                if media:
                    self._client.set_tan_medium(media[0])
            self._client.__enter__()
            self._open = True
        except Exception as e:
            self._fail(e)
        if self._client.init_tan_response:
            self._pending = self._client.init_tan_response
            self._settle()

    def complete_login(self, code: str) -> None:
        if self._pending is None:
            return
        try:
            result = self._client.send_tan(self._pending, code)
        except Exception as e:
            self._fail(e)
        if _needs_tan(result):
            self._pending = result
            raise AuthRequired(_challenge(result, again=True))
        self._pending = None

    def _settle(self, result=None):
        """Resolve a pending TAN request; returns the operation's result."""
        if self._pending is None:
            return result
        if self._pending.decoupled and self.on_user_action:
            self.on_user_action("Bitte bestätige den Zugriff des Wealthfolio Broker Sync in der DKB-App.")
            deadline = time.monotonic() + DECOUPLED_WAIT
            while time.monotonic() < deadline:
                time.sleep(DECOUPLED_POLL)
                try:
                    result = self._client.send_tan(self._pending, "")
                except Exception as e:
                    self._fail(e)
                if not _needs_tan(result):
                    self._pending = None
                    return result
                self._pending = result
        raise AuthRequired(_challenge(self._pending))

    def _fail(self, e: Exception):
        name = type(e).__name__
        if name == "FinTSClientPINError":
            self.session["pin_rejected"] = True
            raise AdapterError("Die DKB hat Anmeldename oder PIN abgelehnt.") from e
        if name == "FinTSClientTemporaryAuthError":
            self.session["pin_rejected"] = True
            raise AdapterError("Der DKB-Zugang ist vorübergehend gesperrt. Bitte im DKB-Banking entsperren.") from e
        if isinstance(e, AuthRequired | AdapterError):
            raise e
        raise AdapterError(f"DKB (FinTS): {e}") from e

    def _call(self, fn, *args):
        try:
            result = fn(*args)
        except Exception as e:
            self._fail(e)
        if _needs_tan(result):
            self._pending = result
            result = self._settle()
        return result

    def close(self) -> None:
        if self._client is None:
            return
        try:
            if self._open:
                self._client.__exit__(None, None, None)
        except Exception:
            pass
        self._open = False
        try:
            self.session["fints"] = base64.b64encode(self._client.deconstruct(including_private=True)).decode()
        except Exception:
            pass

    @classmethod
    def replay(cls, recording: dict) -> DkbAdapter:
        adapter = cls({"username": "replay", "pin": "replay", "product_id": "replay",
                       "iban": recording.get("iban", "")})
        adapter._client = ReplayClient(recording)
        adapter._open = True
        return adapter

    # ── data ────────────────────────────────────────────────────────────────
    def _account(self):
        accounts = self._call(self._client.get_sepa_accounts)
        if not accounts:
            raise AdapterError("Die DKB meldet kein Konto für diesen Zugang.")
        wanted = (self.credentials.get("iban") or "").replace(" ", "").upper()
        if wanted:
            for a in accounts:
                if (a.iban or "").upper() == wanted:
                    return a
            raise AdapterError(f"Die IBAN {wanted} gehört nicht zu diesem DKB-Zugang.")
        return accounts[0]

    def get_accounts(self) -> list[BrokerAccount]:
        return [BrokerAccount(a.iban or a.accountnumber, a.iban or a.accountnumber, "EUR")
                for a in self._call(self._client.get_sepa_accounts)]

    def get_cash(self) -> list[CashBalance]:
        balance = self._call(self._client.get_balance, self._account())
        if balance is None:
            return []
        return [CashBalance(balance.amount.currency, Decimal(str(balance.amount.amount)))]

    def get_positions(self) -> list[Position]:
        out: list[Position] = []
        for account in self._call(self._client.get_sepa_accounts):
            try:
                holdings = self._call(self._client.get_holdings, account) or []
            except AdapterError:
                continue  # not a depot, or DKB doesn't offer holdings over FinTS
            for h in holdings:
                out.append(Position(h.ISIN, h.name, Decimal(str(h.pieces)), h.value_symbol or "EUR",
                                    Decimal(str(h.total_value)) if h.total_value is not None else None))
        return out

    def get_transactions(self, since: datetime | None) -> list[Transaction]:
        today = datetime.now(BERLIN).date()
        start = since.astimezone(BERLIN).date() if since else today - timedelta(days=NO_TAN_DAYS)
        records = self._call(self._client.get_transactions, self._account(), start, today) or []
        return to_transactions([r.data for r in records])


def to_transactions(records: list[dict]) -> list[Transaction]:
    """MT940/camt records (python-fints ``Transaction.data``) → transactions."""
    seen: Counter[str] = Counter()
    out: list[Transaction] = []
    for d in records:
        amount = d["amount"]
        value = Decimal(str(amount.amount))
        currency = amount.currency or "EUR"
        day: date = d.get("entry_date") or d.get("date")
        posting = (d.get("posting_text") or "").strip()
        purpose = " ".join((d.get("purpose") or "").split())
        name = (d.get("applicant_name") or "").strip()
        iban = (d.get("applicant_iban") or d.get("gvc_applicant_iban") or "").replace(" ", "").upper()
        key = "|".join(str(x) for x in (day, d.get("date"), value, currency, posting, purpose, name, iban,
                                         d.get("end_to_end_reference") or "", d.get("customer_reference") or ""))
        digest = hashlib.sha256(key.encode()).hexdigest()[:16]
        seen[digest] += 1
        tx_id = f"{day:%Y%m%d}-{digest}-{seen[digest]}"
        when = datetime(day.year, day.month, day.day, 12, tzinfo=BERLIN).astimezone(UTC)
        if SECURITIES.search(f"{posting} {purpose}"):
            kind = Kind.SECURITIES_CASH
        elif value > 0:
            kind = Kind.INTEREST if INTEREST.search(f"{posting} {purpose}") and not name else Kind.DEPOSIT
        else:
            kind = Kind.FEE if FEE.search(posting) and not name else Kind.WITHDRAWAL
        out.append(Transaction(
            id=tx_id,
            kind=kind,
            datetime=when,
            currency=currency,
            net=abs(value),
            signed=value,
            label=posting,
            counterparty=name,
            counterparty_iban=iban,
            text=purpose,
            raw_type=posting,
        ))
    return out


def _needs_tan(result) -> bool:
    return type(result).__name__ == "NeedTANResponse"


def _challenge(resp, again: bool = False) -> Challenge:
    text = getattr(resp, "challenge", None) or ""
    if getattr(resp, "decoupled", False):
        msg = "Noch nicht bestätigt. " if again else ""
        return Challenge("confirm", msg + (text or "Bitte bestätige den Zugriff in der DKB-App."))
    return Challenge("code", text or "Bitte gib die TAN ein.")


class _Amount:
    def __init__(self, amount: str, currency: str):
        self.amount = Decimal(amount)
        self.currency = currency


class _Record:
    def __init__(self, data: dict):
        self.data = data


class _Account:
    def __init__(self, iban: str):
        self.iban = iban
        self.accountnumber = iban[-10:]


class _Balance:
    def __init__(self, amount: str, currency: str):
        self.amount = _Amount(amount, currency)


class ReplayClient:
    """Answers like python-fints' client from a recording (contract tests).

    Recording: ``{"accounts": [{"iban"}], "transactions": [{"date", "entry_date",
    "amount", "currency", "posting_text", "purpose", "applicant_name",
    "applicant_iban"}], "balance": {"amount", "currency"}}`` - dates ISO, amounts
    as strings, always fabricated.
    """

    init_tan_response = None
    selected_tan_medium = None

    def __init__(self, recording: dict):
        self.recording = recording

    def get_current_tan_mechanism(self):
        return "940"

    def fetch_tan_mechanisms(self):
        return "940"

    def is_tan_media_required(self):
        return False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def deconstruct(self, including_private=False) -> bytes:
        return b"replay"

    def get_sepa_accounts(self):
        return [_Account(a["iban"]) for a in self.recording.get("accounts", [])]

    def get_balance(self, account):
        b = self.recording.get("balance")
        return _Balance(b["amount"], b.get("currency", "EUR")) if b else None

    def get_holdings(self, account):
        return []

    def get_transactions(self, account, start_date=None, end_date=None):
        out = []
        for t in self.recording.get("transactions", []):
            d = dict(t)
            d["amount"] = _Amount(t["amount"], t.get("currency", "EUR"))
            d["date"] = date.fromisoformat(t["date"])
            d["entry_date"] = date.fromisoformat(t["entry_date"]) if t.get("entry_date") else None
            if (start_date and (d["entry_date"] or d["date"]) < start_date) or \
                    (end_date and (d["entry_date"] or d["date"]) > end_date):
                continue
            out.append(_Record(d))
        return out


def _fints_client(blz, user, pin, server, product_id, data):
    from fints.client import FinTS3PinTanClient

    return FinTS3PinTanClient(blz, user, pin, server, product_id=product_id, from_data=data)
