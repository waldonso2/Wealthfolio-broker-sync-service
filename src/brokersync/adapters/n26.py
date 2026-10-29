"""N26 over the interface of its own app (waldonso2/wealthfolio-importer-addon#135).

N26 offers no official interface to private customers; this is the one its app uses (documented by
the open-source python-n26). PSD2 through Enable Banking is the official way (``enablebanking.py``);
this one needs no app at a third party, but the user's yes (``consent``, #112).

Login (only from the UI): password grant → N26 answers ``mfa_required`` with an ``mfaToken`` → an
``oob`` challenge pushes a confirmation into the N26 app → ``grant_type=mfa_oob`` gives access and
refresh token, kept in the session with a device token of its own. Scheduled runs refresh the token
and never start a login (no push the user didn't ask for). A rejected password stops every further
attempt until the credentials are saved anew.

Read only: transactions (booked ones; ids ``n26-<id>``) and the balance. Money moved to a Space shows
up as a withdrawal; a transfer pattern (page *Überträge*) can book it as a transfer.
"""

from __future__ import annotations

import base64
import logging
import time
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import ClassVar

import httpx

from ..model import CashBalance, Kind, Position, Transaction
from .base import AdapterError, AuthRequired, BrokerAdapter, Challenge, CredentialField
from .fints import BERLIN

log = logging.getLogger(__name__)

API = "https://api.tech26.de"
# The app's public client (python-n26): not a secret, every installation of the app sends it.
CLIENT = "Basic " + base64.b64encode(b"nativeweb:").decode()
# The app confirmation: asked every POLL seconds, up to POLLS times per click on "bestätigt".
POLL, POLLS = 5, 12
PAGE = 200
# At most this many pages of transactions: 200 000 bookings, far beyond any account.
MAX_PAGES = 1000
FIRST_DAYS = 90  # without a start date


class N26Adapter(BrokerAdapter):
    key = "n26"
    label = "N26"
    credential_fields: ClassVar[list[CredentialField]] = [
        CredentialField("username", "E-Mail", help="Die E-Mail-Adresse deines N26-Kontos."),
        CredentialField("password", "Passwort", secret=True),
    ]
    consent = ("N26 bietet keine offizielle Schnittstelle für Privatkunden. Der Dienst nutzt die der N26-App, wie "
               "andere Finanzprogramme auch. N26 kann sie jederzeit ändern oder solche Zugriffe untersagen; dann "
               "klappt der Abruf nicht mehr. Der Dienst liest nur, er überweist nie. Der offizielle Weg ist "
               "„Bank über PSD2“.")

    # Replaced in tests with an httpx.MockTransport.
    transport: ClassVar[httpx.BaseTransport | None] = None

    def __init__(self, credentials, session=None):
        super().__init__(credentials, session)
        self._http: httpx.Client | None = None
        self.today: date | None = None

    # ── HTTP ────────────────────────────────────────────────────────────────
    @property
    def http(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(transport=type(self).transport, base_url=API, timeout=60)
        return self._http

    def close(self) -> None:
        if self._http is not None:
            self._http.close()
            self._http = None

    def _device(self) -> str:
        return self.session.setdefault("device_token", str(uuid.uuid4()))

    def _token_request(self, data: dict) -> httpx.Response:
        try:
            return self.http.post("/oauth2/token", data=data,
                                  headers={"Authorization": CLIENT, "device-token": self._device()})
        except httpx.HTTPError as e:
            raise AdapterError(f"N26 antwortet nicht ({type(e).__name__}).") from e

    def _keep(self, body: dict) -> None:
        self.session.update(access_token=body["access_token"], refresh_token=body.get("refresh_token", ""),
                            expires_at=time.time() + float(body.get("expires_in") or 0))
        self.session.pop("mfa_token", None)

    def _get(self, path: str, **params) -> dict | list:
        try:
            r = self.http.get(path, params=params, headers={"Authorization": f"Bearer {self.session['access_token']}",
                                                            "device-token": self._device()})
        except httpx.HTTPError as e:
            raise AdapterError(f"N26 antwortet nicht ({type(e).__name__}).") from e
        if r.status_code == 429:
            raise AdapterError("N26 begrenzt gerade die Abrufe - der nächste Abruf versucht es wieder.")
        if r.status_code in (401, 403):
            self.session.pop("access_token", None)
            raise AdapterError("N26 hat die Anmeldung beendet - bitte neu anmelden.")
        if r.status_code >= 400:
            raise AdapterError(f"N26: HTTP {r.status_code}")
        return r.json()

    # ── login ───────────────────────────────────────────────────────────────
    def login(self) -> None:
        if self.session.get("pin_rejected"):
            raise AdapterError("N26 hat E-Mail oder Passwort abgelehnt. Bitte in den Broker-Einstellungen neu "
                               "eintragen - bis dahin versucht der Dienst es nicht noch einmal.")
        if self.session.get("access_token") and time.time() < self.session.get("expires_at", 0) - 60:
            return
        if self.session.get("refresh_token"):
            r = self._token_request({"grant_type": "refresh_token", "refresh_token": self.session["refresh_token"]})
            if r.status_code == 200:
                self._keep(r.json())
                return
            for k in ("access_token", "refresh_token", "expires_at"):
                self.session.pop(k, None)
        if self.on_user_action is not None:  # a run of the sync: only the UI starts a login (a push in the app)
            raise AuthRequired(Challenge("confirm", "Die Anmeldung bei N26 ist abgelaufen - bitte in der Oberfläche "
                                                    "neu anmelden."))
        self._start()

    def _start(self) -> None:
        if not self.credentials.get("username") or not self.credentials.get("password"):
            raise AdapterError("E-Mail und Passwort fehlen - bitte in den Broker-Einstellungen eintragen.")
        r = self._token_request({"grant_type": "password", "username": self.credentials["username"],
                                 "password": self.credentials["password"]})
        body = _json(r)
        if r.status_code == 200:  # no second factor asked
            self._keep(body)
            return
        if r.status_code != 403 or body.get("error") != "mfa_required" or not body.get("mfaToken"):
            if r.status_code in (400, 401) or body.get("error") == "invalid_grant":
                self.session["pin_rejected"] = True
                raise AdapterError("N26 hat E-Mail oder Passwort abgelehnt. Bitte in den Broker-Einstellungen "
                                   "prüfen - bis dahin versucht der Dienst es nicht noch einmal.")
            raise AdapterError(f"N26: Anmeldung nicht möglich (HTTP {r.status_code}).")
        self.session["mfa_token"] = body["mfaToken"]
        try:
            c = self.http.post("/api/mfa/challenge", json={"challengeType": "oob", "mfaToken": body["mfaToken"]},
                               headers={"Authorization": CLIENT, "device-token": self._device()})
        except httpx.HTTPError as e:
            raise AdapterError(f"N26 antwortet nicht ({type(e).__name__}).") from e
        if c.status_code >= 400:
            raise AdapterError(f"N26: die Bestätigung in der App ließ sich nicht anfordern (HTTP {c.status_code}).")
        raise AuthRequired(Challenge("confirm", "Bestätige die Anmeldung in der N26-App."))

    def complete_login(self, code: str) -> None:
        token = self.session.get("mfa_token")
        if not token:
            raise AdapterError("Keine offene Anmeldung - bitte neu starten.")
        for n in range(POLLS):
            r = self._token_request({"grant_type": "mfa_oob", "mfaToken": token})
            if r.status_code == 200:
                self._keep(r.json())
                return
            if n + 1 < POLLS:
                time.sleep(POLL)
        raise AuthRequired(Challenge("confirm", "Noch nicht bestätigt - bestätige die Anmeldung in der N26-App "
                                                "und klicke dann erneut."))

    # ── data ────────────────────────────────────────────────────────────────
    def get_transactions(self, since: datetime | None) -> list[Transaction]:
        today = self.today or datetime.now(BERLIN).date()
        start = since.astimezone(BERLIN).date() if since else today - timedelta(days=FIRST_DAYS)
        self.history_start = start
        first = int(datetime(start.year, start.month, start.day, tzinfo=BERLIN).timestamp() * 1000)
        last = int(datetime.combine(today + timedelta(days=1), datetime.min.time(), BERLIN).timestamp() * 1000)
        raw: list[dict] = []
        params: dict = {"from": first, "to": last, "limit": PAGE}
        for _ in range(MAX_PAGES):  # an answer that ignores lastId would else loop for ever
            page = self._get("/api/smrt/transactions", **params)
            items = page if isinstance(page, list) else []
            raw += items
            if len(items) < PAGE:
                break
            params["lastId"] = items[-1]["id"]
        else:
            raise AdapterError("N26 liefert immer weitere Seiten - Abruf abgebrochen.")
        booked = [t for t in raw if not t.get("pending")]
        log.info("N26: %d Umsätze seit %s (%d vorgemerkte übersprungen)", len(booked), start.isoformat(),
                 len(raw) - len(booked))
        return [to_transaction(t) for t in booked]

    def get_cash(self) -> list[CashBalance]:
        account = self._get("/api/accounts")
        if not isinstance(account, dict):
            return []
        value = account.get("bankBalance", account.get("availableBalance"))
        return [] if value is None else [CashBalance(account.get("currency") or "EUR", Decimal(str(value)))]

    def get_positions(self) -> list[Position]:
        return []

    @classmethod
    def replay(cls, recording: dict) -> N26Adapter:
        adapter = cls({"username": "replay", "password": "replay"},
                      {"access_token": "replay", "expires_at": 4102444800, "device_token": "replay"})
        adapter._http = httpx.Client(transport=replay_transport(recording), base_url=API)
        adapter.today = date.fromisoformat(recording["today"])
        return adapter


def to_transaction(t: dict) -> Transaction:
    """One N26 transaction: money in is a deposit, money out a withdrawal (transfer patterns apply)."""
    amount = Decimal(str(t.get("amount") or 0))
    at = datetime.fromtimestamp(int(t.get("visibleTS") or t.get("createdTS") or 0) / 1000, UTC)
    name = t.get("partnerName") or t.get("merchantName") or ""
    return Transaction(
        id=f"n26-{t['id']}", kind=Kind.DEPOSIT if amount > 0 else Kind.WITHDRAWAL, datetime=at,
        currency=t.get("currencyCode") or "EUR", net=abs(amount), signed=amount, label=t.get("type") or "",
        counterparty=name, counterparty_iban=(t.get("partnerIban") or "").replace(" ", "").upper(),
        text=" ".join((t.get("referenceText") or "").split()), raw_type=t.get("type") or "")


def _json(r: httpx.Response) -> dict:
    try:
        body = r.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def replay_transport(recording: dict) -> httpx.MockTransport:
    """Answers from a recording: ``pages`` of transactions (one per request, in order) and ``account``."""
    pages = iter(recording["pages"])

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/smrt/transactions":
            return httpx.Response(200, json=next(pages, []))
        if request.url.path == "/api/accounts":
            return httpx.Response(200, json=recording.get("account", {}))
        return httpx.Response(404, json={})

    return httpx.MockTransport(handle)
