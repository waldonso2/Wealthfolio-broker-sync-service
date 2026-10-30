"""Banks via FinTS (HBCI), read-only: balance, transactions and - if the bank
offers them over FinTS - depot holdings. ``FintsAdapter`` holds everything
FinTS banks share; a bank is a subclass that only sets its profile (see
``dkb.py``).

- **Login:** Anmeldename and PIN of the bank's online banking. FinTS needs
  strong customer authentication (SCA) when a dialog starts and, at many banks,
  for transactions older than 90 days. Banks with a decoupled method confirm in
  their app: in the web UI the user clicks "confirmed"; during a scheduled run
  the adapter notifies the user (``on_user_action``) and waits a few minutes.
  A TAN to type in is asked for in the web UI.
- **PIN safety:** after a rejected PIN the adapter never contacts the bank
  again until the credentials are saved anew - three wrong attempts lock the
  online banking.
- **Session:** python-fints' state (system id, bank and user parameters - the
  latter with account numbers and names -, chosen TAN method and medium) is kept
  in the session, so a confirmed device stays known. Never the PIN: python-fints
  doesn't serialise it (``test_security`` pins the blob's keys).
- **Product ID:** FinTS servers want a product ID registered with the Deutsche
  Kreditwirtschaft. Until this service has one, it is entered in the broker
  settings.
- **Securities:** debits/credits on the giro account for depot trades and
  payouts are reported as ``SECURITIES_CASH``: the PDF import (or later a depot
  adapter) books them as transfer pairs, the sync only checks they are there.
  Which bookings these are, and which are interest or fees, is recognised from
  posting text and purpose; a bank whose texts differ sets its own patterns.

Bookings come as MT940 (or camt, which python-fints converts) records without a
stable id; the id is a hash of their content plus a counter for identical
bookings on the same day. Keep that scheme: changing it makes every
synced booking look new.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import re
import threading
import time
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import ClassVar
from zoneinfo import ZoneInfo

from .. import redact
from ..model import SAVINGS, CashBalance, Kind, Position, Transaction
from . import institutes
from .base import AdapterError, AuthRequired, BrokerAdapter, Challenge, CredentialField

log = logging.getLogger(__name__)

# The service's FinTS product id, registered with the Deutsche Kreditwirtschaft. Not a secret - every
# FinTS message carries it to the bank - but it must not turn up in a search of the code: stored XORed
# with a pad derived by SHA-256, so neither it nor any part or encoding of it appears here.
# shortcut: obfuscation, not encryption (the decoding ships with it); a new id from the registry means a
# new value here (scripts/obfuscate_product_id.py).
_REGISTERED = bytes.fromhex("530fc37fcb51d2d113f4a5d844b700dd96336ab95cbe14e2af")


def product_id() -> str:
    pad = hashlib.sha256(b"wealthfolio-broker-sync/fints").digest()  # a label, not the product name: never rename
    value = bytes(a ^ b for a, b in zip(_REGISTERED, pad, strict=False)).decode()
    redact.register(value)  # never in a log line or on screen
    return value
BERLIN = ZoneInfo("Europe/Berlin")
# PSD2: banks may ask for a TAN for bookings older than this.
NO_TAN_DAYS = 89
# How long a scheduled run waits for a confirmation in the bank's app.
DECOUPLED_WAIT = 180
DECOUPLED_POLL = 5

# Posting text or purpose of a giro booking that settles a depot trade or payout.
# Keywords only - an ISIN pattern would also hit SEPA mandate references.
SECURITIES = re.compile(r"wertpapier|wp-abr|depot|ertragsgutschrift|dividende|aussch[uü]ttung|kupon", re.IGNORECASE)
INTEREST = re.compile(r"\bzins|habenzins|guthabenzins", re.IGNORECASE)
FEE = re.compile(r"entgelt|geb[uü]hr|kontof[uü]hrung|abschluss", re.IGNORECASE)
# Product names of savings accounts - not the giro account, even when the bank reports them as account type 1-9
# (the DKB's Tagesgeld).
SAVINGS_NAME = re.compile(r"tagesgeld|spar|festgeld|geldmarkt|zinskonto", re.IGNORECASE)
# Days between the two sides of a transfer between the cash and the savings account.
OWN_TRANSFER_DAYS = 3


def credential_fields(bank: str, banking: str, *, blz: bool = False, server: bool = False,
                      username_help: str = "") -> list[CredentialField]:
    """The usual FinTS credentials; ``blz`` for banks whose bank code differs per branch, ``server`` for a
    bank without a profile (its FinTS address, #136)."""
    fields = [
        CredentialField("username", "Anmeldename", help=username_help or f"Wie beim {banking} im Browser."),
        CredentialField("pin", "PIN / Passwort", secret=True, ask=True,
                        help="Leer lassen: Du gibst die PIN bei jeder Anmeldung ein, gespeichert wird sie nicht."),
    ]
    if blz:
        fields.append(CredentialField("blz", "Bankleitzahl", help="Die Bankleitzahl deines Kontos (8 Ziffern)."))
    if server:
        fields.append(CredentialField("server", "FinTS-Adresse",
                                      help="Leer lassen: Der Dienst kennt die Adresse deiner Bank aus der Liste der "
                                           "Deutschen Kreditwirtschaft. Nur nötig, wenn sie dort fehlt - dann steht "
                                           "sie auf der Website deiner Bank (Stichwort FinTS oder HBCI), beginnt mit https://."))
    fields += [
        CredentialField("iban", "IBAN des Girokontos",
                        help=f"Leer lassen, wenn du bei {bank} nur ein Konto hast."),
        CredentialField("savings_iban", "IBAN des Tagesgeldkontos",
                        help="Nur nötig, wenn du unten ein Wealthfolio-Konto für das Tagesgeld wählst und der Dienst "
                             "es nicht selbst findet."),
    ]
    return fields


class FintsAdapter(BrokerAdapter):
    """A bank over FinTS. Subclasses set ``key``, ``label``, ``credential_fields``
    (see ``credential_fields()``) and the profile below."""

    # ── bank profile ────────────────────────────────────────────────────────
    # Bank code; empty if it differs per branch (then it is a credential, "blz").
    blz: ClassVar[str] = ""
    server: ClassVar[str] = ""  # empty: a credential, "server" (#136)
    # Names in messages: "Die DKB hat ... abgelehnt", "in der DKB-App", "im DKB-Banking".
    bank: ClassVar[str] = "die Bank"
    app: ClassVar[str] = "Banking-App"
    banking: ClassVar[str] = "Online-Banking"
    # Posting text/purpose of giro bookings that settle securities, are interest or fees.
    securities_pattern: ClassVar[re.Pattern] = SECURITIES
    interest_pattern: ClassVar[re.Pattern] = INTEREST
    fee_pattern: ClassVar[re.Pattern] = FEE
    savings_label = "Tagesgeld"
    # Book the depot transactions (HKWDU, MT536) as trades (#130); else they are only described in the log.
    # Off where depot trades come from elsewhere (Deutsche Bank: its CSV export) - they would be booked twice.
    book_depot_movements: ClassVar[bool] = False

    # Replaced in tests with a fake; real runs use python-fints.
    client_factory = None

    def __init__(self, credentials, session=None):
        super().__init__(credentials, session)
        self._client = None
        self._pending = None
        self._open = False
        self._logged_account = False
        self._logged_savings = False
        # No stored PIN (#85): the login asks for it; it lives only in this object's credentials.
        self._awaiting_pin = False

    # ── login ───────────────────────────────────────────────────────────────
    def _make_client(self):
        # A user's own product id saved before the service had one keeps working until the credentials are
        # saved anew (the form no longer has the field).
        pid = (self.credentials.get("product_id") or "").strip() or product_id()
        if not self.credentials.get("username"):
            raise AdapterError("Der Anmeldename fehlt - bitte in den Broker-Einstellungen eintragen.")
        blz = self.blz or (self.credentials.get("blz") or "").replace(" ", "")
        if not blz:
            raise AdapterError("Die Bankleitzahl fehlt - bitte in den Broker-Einstellungen eintragen.")
        listed = institutes.lookup(blz)
        server = self.server or (self.credentials.get("server") or "").strip() or (listed[1] if listed else "")
        if not server:
            raise AdapterError(f"Für die Bankleitzahl {blz} kennt der Dienst keine FinTS-Adresse - bitte in den "
                               "Broker-Einstellungen eintragen.")
        data = base64.b64decode(self.session["fints"]) if self.session.get("fints") else None
        factory = type(self).client_factory or _fints_client
        return factory(blz, self.credentials["username"], self.credentials["pin"], server, pid, data)

    def login(self) -> None:
        if self.session.get("pin_rejected"):
            raise AdapterError(
                f"{_cap(self.bank)} hat Anmeldename oder PIN abgelehnt. Bitte in den Broker-Einstellungen neu "
                "eintragen - bis dahin versucht der Dienst es nicht noch einmal, damit das Online-Banking nicht "
                "gesperrt wird."
            )
        if not self.credentials.get("pin"):
            self._awaiting_pin = True
            raise AuthRequired(Challenge("pin", f"Bitte gib deine PIN für das {self.banking} ein. Sie wird nicht "
                                                "gespeichert und nur für diesen Abruf verwendet."))
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
        self._log_capabilities()

    def complete_login(self, code: str) -> None:
        if self._awaiting_pin:
            if not code:
                raise AuthRequired(Challenge("pin", "Bitte gib deine PIN ein."))
            redact.register(code)  # never in a log line or a message on screen
            self.credentials = {**self.credentials, "pin": code}
            self._awaiting_pin = False
            self.login()
            return
        if self._pending is None:
            return
        try:
            result = self._client.send_tan(self._pending, code)
        except Exception as e:
            self._fail(e)
        if _needs_tan(result):
            self._pending = result
            raise AuthRequired(self._challenge(result, again=True))
        self._pending = None

    def _settle(self, result=None):
        """Resolve a pending TAN request; returns the operation's result."""
        if self._pending is None:
            return result
        if self._pending.decoupled and self.on_user_action:
            self.on_user_action(f"Bitte bestätige den Zugriff von Wealthfolio Companion in der {self.app}.")
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
        raise AuthRequired(self._challenge(self._pending))

    def _challenge(self, resp, again: bool = False) -> Challenge:
        text = getattr(resp, "challenge", None) or ""
        if getattr(resp, "decoupled", False):
            msg = "Noch nicht bestätigt. " if again else ""
            return Challenge("confirm", msg + (text or f"Bitte bestätige den Zugriff in der {self.app}."))
        return Challenge("code", text or "Bitte gib die TAN ein.")

    def _fail(self, e: Exception):
        name = type(e).__name__
        if name == "FinTSClientPINError":
            self.session["pin_rejected"] = True
            raise AdapterError(f"{_cap(self.bank)} hat Anmeldename oder PIN abgelehnt.") from e
        if name == "FinTSClientTemporaryAuthError":
            self.session["pin_rejected"] = True
            raise AdapterError(f"Der {self.label}-Zugang ist vorübergehend gesperrt. Bitte im {self.banking} "
                               "entsperren.") from e
        if isinstance(e, AuthRequired | AdapterError):
            raise e
        if isinstance(e, OSError):  # also requests' ConnectionError and Timeout
            # No answer (network, timeout): said in German, the technical text after it (UX-04).
            raise AdapterError(f"{_cap(self.bank)} antwortet nicht (Netzwerk oder Zeitüberschreitung) - bitte später "
                               f"erneut versuchen. ({name}: {e})") from e
        bank_says = _bank_messages(str(e))
        if bank_says:
            raise AdapterError(f"{self.label} (FinTS): {_cap(self.bank)} meldet: {bank_says}") from e
        raise AdapterError(f"{self.label} (FinTS): {e}") from e

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
        with suppress(Exception):
            if self._open:
                self._client.__exit__(None, None, None)
        self._open = False
        with suppress(Exception):
            self.session["fints"] = base64.b64encode(self._client.deconstruct(including_private=True)).decode()

    @classmethod
    def replay(cls, recording: dict) -> FintsAdapter:
        adapter = cls({"username": "replay", "pin": "replay", "product_id": "replay",
                       "blz": "replay", "iban": recording.get("iban", "")})
        adapter._client = ReplayClient(recording)
        adapter._open = True
        return adapter

    def _information(self) -> dict:
        """python-fints' summary of the bank parameters and accounts ({} when not available)."""
        try:
            return self._client.get_information() or {}
        except Exception:
            return {}

    def _log_capabilities(self) -> None:
        """What the bank offers over FinTS, without numbers or names - to set up a new bank profile."""
        info = self._information()
        if not info:
            return
        ops = _ops(info.get("bank", {}))
        depot_tx = False
        with suppress(Exception):
            depot_tx = bool(self._client.bpd.find_segment_first("HIWDUS"))
        log.info("%s (FinTS): Umsätze %s, camt %s, Depotbestand %s, Depotumsätze %s", self.label,
                 _yes(ops.get("GET_TRANSACTIONS")), _yes(ops.get("GET_TRANSACTIONS_XML")),
                 _yes(ops.get("GET_HOLDINGS")), _yes(depot_tx))
        for a in info.get("accounts") or []:
            acc_ops = _ops(a)
            log.info("%s (FinTS): Konto „%s“ (Art %s, IBAN %s): Umsätze %s, Depotbestand %s", self.label,
                     a.get("product_name") or "?", a.get("type") or "?", _yes(a.get("iban")),
                     _yes(acc_ops.get("GET_TRANSACTIONS") or acc_ops.get("GET_TRANSACTIONS_XML")),
                     _yes(acc_ops.get("GET_HOLDINGS")))

    # ── data ────────────────────────────────────────────────────────────────
    def _upd(self) -> dict[str, dict]:
        """The accounts of the user parameters (UPD) by IBAN."""
        return {(a.get("iban") or "").upper(): a for a in self._information().get("accounts") or [] if a.get("iban")}

    def _typed_iban(self, field: str) -> str:
        return (self.credentials.get(field) or "").replace(" ", "").upper()

    def _log_chosen(self, flag: str, what: str, chosen) -> None:
        """Once per adapter: which account the bank's name says was chosen."""
        if not getattr(self, flag):
            setattr(self, flag, True)
            name = self._upd().get((chosen.iban or "").upper(), {}).get("product_name")
            log.info("%s (FinTS): %s von „%s“", self.label, what, name or "?")

    def _account(self):
        accounts = self._call(self._client.get_sepa_accounts)
        if not accounts:
            raise AdapterError(f"{_cap(self.bank)} meldet kein Konto für diesen Zugang.")
        wanted = self._typed_iban("iban")
        if wanted:
            chosen = next((a for a in accounts if (a.iban or "").upper() == wanted), None)
            if chosen is None:
                raise AdapterError(f"Die IBAN {wanted} gehört nicht zu diesem {self.label}-Zugang.")
            return chosen
        # Without an IBAN: the giro account by the account type the bank reports (FinTS: 1-9 current
        # accounts; 10-19 savings, 30-39 depots), else the first account with an IBAN.
        upd = self._upd()
        chosen = (next((a for a in accounts if _kind(upd.get((a.iban or "").upper())) == "giro"), None)
                  or next((a for a in accounts if a.iban), accounts[0]))
        self._log_chosen("_logged_account", "Umsätze und Saldo", chosen)
        return chosen

    def _depot_accounts(self) -> list[tuple[object, str]]:
        """Accounts to ask for holdings, each with how it was found (for the log): the SEPA accounts the user
        parameters allow holdings for (all of them if the bank says nothing), plus accounts listed there with
        holdings but without IBAN (a depot is not a SEPA account at most banks)."""
        info = self._information().get("accounts") or []
        holdings_ibans = {(a.get("iban") or "").upper() for a in info if _ops(a).get("GET_HOLDINGS")}
        sepa = self._call(self._client.get_sepa_accounts) or []
        accounts = [(a, "Konto mit IBAN" if a.iban else "Konto ohne IBAN") for a in sepa
                    if not (info and a.iban and a.iban.upper() not in holdings_ibans)]  # a giro: no holdings
        # python-fints takes the bank's country from the BIC (HKWPD's account, Account2/3): a depot without
        # IBAN gets the BIC of the bank's other accounts, else one naming Germany.
        bic = next((a.bic for a in sepa if getattr(a, "bic", None)), None) or "XXXXDEXXXXX"
        known = {(a.accountnumber, getattr(a, "subaccount", None)) for a, _ in accounts}
        return accounts + [(self._upd_account(a, bic), f"„{a.get('product_name') or '?'}“ (Art {a.get('type') or '?'})")
                           for a in info if _depot_without_iban(a, known)]

    def _upd_account(self, a: dict, bic: str):
        """A depot the user parameters list without IBAN, as the account python-fints asks for holdings."""
        bank_id = a.get("bank_identifier")
        return _sepa_account(a["account_number"], a.get("subaccount_number"),
                             getattr(bank_id, "bank_code", None) or self.blz
                             or (self.credentials.get("blz") or "").replace(" ", ""), bic)

    def _savings_account(self):
        """The savings account (Tagesgeld): by the IBAN the user entered, else the account the bank calls a
        savings account (type 10-19, or its product name) other than the giro account."""
        accounts = [a for a in self._call(self._client.get_sepa_accounts) or [] if a.iban]
        giro = (self._account().iban or "").upper()
        wanted = self._typed_iban("savings_iban")
        if wanted:
            chosen = next((a for a in accounts if a.iban.upper() == wanted), None)
            if chosen is None:
                raise AdapterError(f"Die IBAN des Tagesgeldkontos gehört nicht zu diesem {self.label}-Zugang.")
        else:
            upd = self._upd()
            chosen = next((a for a in accounts if a.iban.upper() != giro
                           and _kind(upd.get(a.iban.upper())) == "savings"), None)
            if chosen is None:
                raise AdapterError(f"{_cap(self.bank)} meldet kein Tagesgeldkonto für diesen Zugang - bitte die "
                                   "IBAN des Tagesgeldkontos in den Broker-Einstellungen eintragen.")
        if chosen.iban.upper() == giro:
            raise AdapterError("Girokonto und Tagesgeldkonto sind dasselbe Konto - bitte die IBANs prüfen.")
        self._log_chosen("_logged_savings", "Tagesgeld: Umsätze und Saldo", chosen)
        return chosen

    def get_cash(self) -> list[CashBalance]:
        out = []
        balance = self._call(self._client.get_balance, self._account())
        if balance is not None:
            out.append(CashBalance(balance.amount.currency, Decimal(str(balance.amount.amount))))
        if self.with_savings:
            balance = self._call(self._client.get_balance, self._savings_account())
            if balance is not None:
                out.append(CashBalance(balance.amount.currency, Decimal(str(balance.amount.amount)), SAVINGS))
        return out

    def get_positions(self) -> list[Position]:
        out: list[Position] = []
        accounts = self._depot_accounts()
        if not accounts:
            log.info("%s (FinTS): kein Konto mit Depotbestand gefunden", self.label)
        for account, what in accounts:
            seen: list[str] = []
            try:
                with _mt535_parser(seen):
                    holdings = self._call(self._client.get_holdings, account) or []
            except AdapterError as e:
                log.info("%s (FinTS): Depotbestand von %s nicht lesbar: %s", self.label, what, e)
                continue  # not a depot, or the bank doesn't offer holdings over FinTS
            log.info("%s (FinTS): Depotbestand von %s mit %d Positionen%s", self.label, what, len(holdings),
                     f" (Antwort: {'; '.join(seen)})" if seen and not holdings else "")
            for h in holdings:
                if not h.ISIN or h.pieces is None:
                    log.info("%s (FinTS): eine Position ohne ISIN oder Stückzahl übersprungen", self.label)
                    continue
                out.append(Position(h.ISIN, h.name, Decimal(str(h.pieces)), h.value_symbol or "EUR"))
        return out

    def _records(self, account, start: date, end: date) -> tuple[list[dict], str]:
        records = [r.data for r in self._call(self._client.get_transactions, account, start, end) or []]
        if not records and _ops(self._information().get("bank", {})).get("GET_TRANSACTIONS_XML"):
            # Some banks (Deutsche Bank) answer the MT940 request with nothing and deliver camt only.
            return self._camt(account, start, end), "camt"
        return records, "MT940"

    def get_transactions(self, since: datetime | None) -> list[Transaction]:
        today = datetime.now(BERLIN).date()
        start = since.astimezone(BERLIN).date() if since else today - timedelta(days=NO_TAN_DAYS)
        self.history_start = start
        account = self._account()
        records, source = self._records(account, start, today)
        log.info("%s (FinTS): %d Umsätze per %s seit %s", self.label, len(records), source,
                 (self.history_start or start).isoformat())
        out = to_transactions(records, self.securities_pattern, self.interest_pattern, self.fee_pattern)
        if self.with_savings:
            out = self._with_savings(out, account, today)
        # As far back as the bank answers without a TAN - not just this run's window: there is more to check.
        movements = self._depot_movements(min(start, today - timedelta(days=NO_TAN_DAYS)), today)
        if movements and self.book_depot_movements:
            out += self._book_movements(movements, out, today)
        return out

    def _book_movements(self, movements: list, giro: list[Transaction], today: date) -> list[Transaction]:
        """The depot transactions as trades (``mt536.to_transactions``); a failure books none of them, never
        stops the giro account's."""
        from . import mt536

        read = [d for d in (self.history_start, self.history_read) if d]
        try:
            txs = mt536.to_transactions(movements, giro, min(read) if read else None, today)
        except Exception as e:
            log.info("%s (FinTS): Depotumsätze nicht gebucht: %s: %s", self.label, type(e).__name__, e)
            return []
        log.info("%s (FinTS): Depotumsätze: %d gebucht oder geprüft, %d warten auf ihre Girobuchung", self.label,
                 len(txs), len(movements) - len(txs))
        return txs

    def _depot_movements(self, start: date, end: date) -> list:
        """The depot transactions (HKWDU, MT536) as ``mt536.Movement``s if the bank offers them without a
        TAN; the log tells what they are and the answer's layout, counts and field names only."""
        try:
            if not self._client.bpd.find_segment_first("HIWDUS"):
                return []
        except Exception:
            return []
        from . import mt536

        if mt536.needs_tan(self._client):
            log.info("%s (FinTS): Depotumsätze verlangen eine Freigabe in der App - nicht abgerufen", self.label)
            return []
        fetch = getattr(self._client, "get_depot_transactions", None) or (
            lambda account, a, b: mt536.fetch(self._client, account, a, b))
        try:
            depots = self._depot_accounts()
        except AdapterError as e:
            log.info("%s (FinTS): Depotumsätze: kein Depot gefunden: %s", self.label, e)
            return []
        out = []
        for account, what in depots:
            try:
                texts = fetch(account, start, end)
            except Exception as e:
                if type(e).__name__ in ("FinTSClientPINError", "FinTSClientTemporaryAuthError"):
                    self._fail(e)  # a rejected PIN must stop everything, not only this diagnostic step
                log.info("%s (FinTS): Depotumsätze von %s nicht lesbar: %s", self.label, what,
                         _bank_messages(str(e)) or f"{type(e).__name__}: {e}")
                continue
            if _needs_tan(texts):
                log.info("%s (FinTS): Depotumsätze von %s verlangen eine Freigabe - nicht abgerufen", self.label,
                         what)
                continue
            try:
                movements = [m for t in texts for m in mt536.parse(t)]
                log.info("%s (FinTS): Depotumsätze von %s seit %s%s: %d - %s. Aufbau: %s",
                         self.label, what, start.isoformat(), "" if self.book_depot_movements else " (nicht gebucht)",
                         len(movements), mt536.summary(movements), mt536.structure(texts))
            except Exception as e:
                log.info("%s (FinTS): Depotumsätze von %s nicht auswertbar: %s: %s", self.label, what,
                         type(e).__name__, e)
                continue
            out += movements
        return out

    def _with_savings(self, giro_txs: list[Transaction], giro_account, today: date) -> list[Transaction]:
        """The savings account's bookings (on its own Wealthfolio account); transfers between the two
        accounts are booked once, as a transfer pair, from the giro side."""
        account = self._savings_account()
        start = (self.savings_since.astimezone(BERLIN).date() if self.savings_since
                 else today - timedelta(days=NO_TAN_DAYS))
        giro_start = self.history_start
        records, source = self._records(account, start, today)
        self.history_start = giro_start  # the giro account's history decides what counts as read (statements)
        log.info("%s (FinTS): %d Tagesgeld-Umsätze per %s seit %s", self.label, len(records), source,
                 start.isoformat())
        savings_iban = (account.iban or "").upper()
        giro_iban = (giro_account.iban or "").upper()
        moves = (Kind.DEPOSIT, Kind.WITHDRAWAL)
        giro_txs = [replace(t, own_transfer=True) if t.kind in moves and t.counterparty_iban == savings_iban
                    else t for t in giro_txs]
        used: set[str] = set()
        savings_txs = []
        for t in to_transactions(records, self.securities_pattern, self.interest_pattern, self.fee_pattern,
                                 prefix="sav-"):
            t = replace(t, account=SAVINGS)
            if t.kind in moves and t.counterparty_iban == giro_iban:
                other = Kind.WITHDRAWAL if t.kind == Kind.DEPOSIT else Kind.DEPOSIT
                partner = next((g for g in giro_txs if g.own_transfer and g.kind == other and g.net == t.net
                                and g.id not in used
                                and abs((g.datetime - t.datetime).days) <= OWN_TRANSFER_DAYS), None)
                if partner:
                    used.add(partner.id)
                    t = replace(t, pair=partner.id)
            savings_txs.append(t)
        return giro_txs + savings_txs

    def _camt_storage_days(self) -> int | None:
        """How many days back the bank keeps camt bookings (its parameters, HICAZS), if it says."""
        try:
            days = self._client.bpd.find_segment_first("HICAZS").parameter.storage_duration
            return int(days) if days else None
        except Exception:
            return None

    def _camt(self, account, start: date, end: date) -> list[dict]:
        from fints.camt_parser import camt053_to_dict

        days = self._camt_storage_days()
        if days and start < end - timedelta(days=days - 1):
            start = end - timedelta(days=days - 1)
            log.info("%s (FinTS): die Bank hält camt-Umsätze %d Tage vor - Abruf ab %s", self.label, days,
                     start.isoformat())
            self.history_start = start
        with _national_account_details():
            answer = self._call(self._client.get_transactions_xml, account, start, end)
        booked = answer[0] if answer else []
        out = []
        for document in booked:
            for d in camt053_to_dict(document):
                d.setdefault("posting_text", d.get("AdditionalEntryInformation") or "")
                out.append(d)
        return out


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]


def _depot_without_iban(a: dict, known: set) -> bool:
    """An account of the user parameters with holdings and no IBAN, not already among the SEPA accounts."""
    number = a.get("account_number")
    return bool(_ops(a).get("GET_HOLDINGS") and not a.get("iban") and number
                and (number, a.get("subaccount_number")) not in known)


def _kind(entry: dict | None) -> str:
    """What the bank says an account of the user parameters is: "savings" (type 10-19 or its product name),
    "giro" (type 1-9), else ""."""
    if not entry:
        return ""
    kind, name = entry.get("type"), entry.get("product_name") or ""
    if (isinstance(kind, int) and 10 <= kind <= 19) or SAVINGS_NAME.search(name):
        return "savings"
    return "giro" if isinstance(kind, int) and 1 <= kind <= 9 else ""


def _ops(entry: dict) -> dict[str, bool]:
    """``supported_operations`` of python-fints' information, by operation name."""
    return {getattr(k, "name", str(k)): v for k, v in (entry.get("supported_operations") or {}).items()}


def _bank_messages(text: str) -> str:
    """The bank's error codes and texts from python-fints' error response (it puts the whole message in)."""
    found = re.findall(r"Response\(code='(9\d{3})', reference_element=[^,]*, text='([^']*)'", text)
    unique = list(dict.fromkeys(f"{code} {msg}" for code, msg in found))
    return "; ".join(unique)


# Both patches below change python-fints for the whole process: two logins at once (two brokers in the web
# UI) must not see or undo each other's (#113). Re-entrant: one adapter may hold both.
_PATCHING = threading.RLock()


@contextmanager
def _national_account_details():
    """Send account number, sub-account and bank code with the IBAN in camt requests (HKCAZ).

    python-fints identifies the account there by IBAN and BIC only; the Deutsche Bank answers
    9210 "Angaben zur nationalen Kontoverbindung für Identifikation erforderlich". The FinTS
    account identification (KTI) allows both, so the national details are added."""
    from fints.formals import KTI1, BankIdentifier

    original = KTI1.__dict__["from_sepa_account"]

    def with_national(cls, acc):
        bank = BankIdentifier(country_identifier="280", bank_code=acc.blz) if getattr(acc, "blz", None) else None
        return cls(iban=acc.iban, bic=acc.bic, account_number=getattr(acc, "accountnumber", None) or None,
                   subaccount_number=getattr(acc, "subaccount", None) or None, bank_identifier=bank)

    with _PATCHING:
        KTI1.from_sepa_account = classmethod(with_national)
        try:
            yield
        finally:
            KTI1.from_sepa_account = original


@contextmanager
def _mt535_parser(seen: list[str]) -> Iterator[None]:
    """python-fints parses holdings with its ``MT535_Miniparser``; ours (``mt535``) is used instead, and the
    layout of each answer (tags only, no values) goes to ``seen`` for the log."""
    import fints.client

    from . import mt535

    class Parser:
        def parse(self, lines):
            seen.append(mt535.structure(lines))
            return mt535.parse(lines)

    with _PATCHING:
        original = fints.client.MT535_Miniparser
        fints.client.MT535_Miniparser = Parser
        try:
            yield
        finally:
            fints.client.MT535_Miniparser = original


def _yes(value) -> str:
    return "ja" if value else "nein"


def _sepa_account(number: str, subaccount, blz: str, bic: str | None = None):
    from fints.models import SEPAAccount

    return SEPAAccount(iban=None, bic=bic, accountnumber=number, subaccount=subaccount, blz=blz)


def to_transactions(records: list[dict], securities: re.Pattern = SECURITIES, interest: re.Pattern = INTEREST,
                    fee: re.Pattern = FEE, prefix: str = "") -> list[Transaction]:
    """MT940/camt records (python-fints ``Transaction.data``) → transactions; ``prefix`` for the ids of a
    second account's bookings (the savings account: ``sav-``), so they never collide with the giro's."""
    seen: Counter[str] = Counter()
    out: list[Transaction] = []
    for d in records:
        amount = d["amount"]
        value = Decimal(str(amount.amount))
        currency = amount.currency or "EUR"
        day = d.get("entry_date") or d.get("date")
        if day is None:
            raise AdapterError("FinTS: a booking without a date")
        posting = (d.get("posting_text") or "").strip()
        purpose = " ".join((d.get("purpose") or "").split())
        name = (d.get("applicant_name") or "").strip()
        iban = (d.get("applicant_iban") or d.get("gvc_applicant_iban") or "").replace(" ", "").upper()
        key = "|".join(str(x) for x in (day, d.get("date"), value, currency, posting, purpose, name, iban,
                                         d.get("end_to_end_reference") or "", d.get("customer_reference") or ""))
        digest = hashlib.sha256(key.encode()).hexdigest()[:16]
        seen[digest] += 1
        tx_id = f"{prefix}{day:%Y%m%d}-{digest}-{seen[digest]}"
        when = datetime(day.year, day.month, day.day, 12, tzinfo=BERLIN).astimezone(UTC)
        if securities.search(f"{posting} {purpose}"):
            kind = Kind.SECURITIES_CASH
        elif value > 0:
            kind = Kind.INTEREST if interest.search(f"{posting} {purpose}") and not name else Kind.DEPOSIT
        else:
            kind = Kind.FEE if fee.search(posting) and not name else Kind.WITHDRAWAL
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


class _ReplayBpd:
    def __init__(self, segments: set[str]):
        self.segments = segments

    def find_segment_first(self, name: str):
        return name if name in self.segments else None


class ReplayClient:
    """Answers like python-fints' client from a recording (contract tests).

    Recording: ``{"accounts": [{"iban"}], "transactions": [{"date", "entry_date",
    "amount", "currency", "posting_text", "purpose", "applicant_name",
    "applicant_iban"}], "balance": {"amount", "currency"}, "depot": {"account",
    "mt536": [answer]}}`` - dates ISO, amounts as strings, always fabricated.
    ``depot`` (optional) adds a depot without IBAN that answers HKWDU.
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

    @property
    def bpd(self):
        return _ReplayBpd({"HIWDUS"} if self.recording.get("depot") else set())

    def _need_twostep_tan_for_segment(self, segment) -> bool:
        return False

    def get_sepa_accounts(self):
        accounts = [_Account(a["iban"]) for a in self.recording.get("accounts", [])]
        if self.recording.get("depot"):
            depot = _Account("")
            depot.iban, depot.accountnumber = None, self.recording["depot"]["account"]
            accounts.append(depot)
        return accounts

    def get_depot_transactions(self, account, start, end):
        depot = self.recording.get("depot") or {}
        return [t.replace("\n", "\r\n") for t in depot.get("mt536", [])] \
            if account.accountnumber == depot.get("account") else []

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
