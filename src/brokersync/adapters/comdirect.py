"""comdirect via its official REST API for private customers (read-only).

The user enables the API in the comdirect online banking (persönlicher Bereich
→ Zugänge → REST API) and gets a client ID and secret there. Specification:
"comdirect REST API – Schnittstellenspezifikation" (April 2020).

- **Login** (five calls, chapter 2): OAuth2 password grant with Zugangsnummer
  and PIN, the session object, a TAN challenge for it, its activation, and the
  ``cd_secondary`` grant that issues the token for banking and brokerage. With
  photoTAN-Push the user approves in the photoTAN app: in the web UI they click
  "confirmed", a scheduled run sends a notification and polls the challenge's
  status link. A photoTAN graphic or mobileTAN is typed in the web UI.
- **Session:** access token (10 minutes) and refresh token are kept in the
  session and refreshed while comdirect allows; after that a new login with TAN
  is needed.
- **Lock-out guard:** comdirect locks the online banking after five TAN
  challenges without a correct TAN in between, and after three wrong TANs. The
  adapter counts unanswered challenges and wrong TANs in the session and stops
  asking long before that (``MAX_OPEN_CHALLENGES``, ``MAX_WRONG_TANS``); a
  rejected PIN stops it too, until the credentials are saved anew.
- **Only the depot is booked, not the giro account.** The cash account in
  Wealthfolio receives the dividends (swept from the depot) and nothing else:
  a buy is preceded by a deposit of its cost, a sale followed by a withdrawal
  of its proceeds (``Transaction.external_cash``). No balance is compared.
- **Trades:** the depot transactions carry share count, price and market
  value, but neither fees nor taxes. Each buy/sell is paired with the giro
  booking that settled it (``Wertpapier``, ±``SETTLEMENT_DAYS``): its amount
  is the trade's cash, the difference to the market value its costs. The API
  doesn't split costs of a sale into fee and tax, so both go into the fee.
  Depot transfers are ``UNKNOWN``; positions bought before the start date
  are added as opening positions on the page *Prüfung*.
- **Dividends** come as giro bookings (``Zinsen / Dividenden``) and are booked
  on the depot when the posting text names the security (ISIN, or a WKN of a
  position); otherwise they are reported.

Only GET endpoints of ``banking`` and ``brokerage`` are called besides the login;
the order, quote and transfer endpoints are never used (AC 7 of #35).
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import time
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import ClassVar
from zoneinfo import ZoneInfo

import httpx

from ..model import BrokerAccount, CashBalance, Kind, Position, Transaction
from .base import AdapterError, AuthRequired, BrokerAdapter, Challenge, CredentialField

log = logging.getLogger(__name__)
BERLIN = ZoneInfo("Europe/Berlin")

API = "https://api.comdirect.de"
TOKEN_URL = f"{API}/oauth/token"
# comdirect locks after 5 challenges without a correct TAN and after 3 wrong TANs.
MAX_OPEN_CHALLENGES = 3
MAX_WRONG_TANS = 1
# A scheduled run waits this long for the approval in the photoTAN app.
PUSH_WAIT = 180
PUSH_POLL = 3
# "Ich habe bestätigt" in the web UI waits at most this long for the status.
CONFIRM_WAIT = 30
# A trade's giro booking lies this many days around its business date.
SETTLEMENT_DAYS = 6
# A trade without its giro booking after this many days is reported.
UNSETTLED_DAYS = 10
PAGE_SIZE = 500
# Without a start date, the first run reads this far back.
DEFAULT_DAYS = 365

ISIN = re.compile(r"\b([A-Z]{2}[A-Z0-9]{9}[0-9])\b")
WKN = re.compile(r"\b([A-Z0-9]{6})\b")
INTEREST = re.compile(r"zins", re.IGNORECASE)
DIVIDEND = re.compile(r"dividend|ertrag|aussch[uü]ttung|ausschuettung", re.IGNORECASE)


def _dec(v) -> Decimal | None:
    if isinstance(v, dict):
        v = v.get("value")
    if v in (None, ""):
        return None
    try:
        return Decimal(str(v))
    except InvalidOperation:
        return None


def _unit(v) -> str:
    return (v or {}).get("unit") or "EUR" if isinstance(v, dict) else "EUR"


def _day(v) -> date | None:
    try:
        return date.fromisoformat(str(v)[:10]) if v else None
    except ValueError:
        return None


def _noon(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, 12, tzinfo=BERLIN).astimezone(UTC)


def _type(t: dict) -> str:
    """The transactionType of a booking (an EnumText) as lowercase text."""
    tt = t.get("transactionType") or {}
    if isinstance(tt, str):
        return tt.lower()
    return f"{tt.get('key') or ''} {tt.get('text') or ''}".lower()


def _text(t: dict) -> str:
    """remittanceInfo without the line numbers comdirect puts in front of each 35-character line."""
    raw = t.get("remittanceInfo") or ""
    lines = [raw[i:i + 37] for i in range(0, len(raw), 37)] if re.match(r"^\d{2}", raw) else [raw]
    return " ".join(" ".join(re.sub(r"^\d{2}", "", line).split()) for line in lines).strip()


def _is_securities(t: dict) -> bool:
    return "wertpapier" in _type(t) or "securities" in _type(t)


def _is_income(t: dict) -> bool:
    kind = _type(t)
    return "zinsen" in kind or "dividend" in kind or "interest" in kind


def _account_id(t: dict) -> str:
    return f"acc-{t.get('reference') or ''}"


def _unknown(tx_id: str, when: datetime, raw_type: str, payload: dict, currency: str = "EUR") -> Transaction:
    return Transaction(id=tx_id, kind=Kind.UNKNOWN, datetime=when, currency=currency, net=Decimal(0),
                       label=raw_type, raw_type=raw_type, raw=payload)


def to_transactions(bookings: list[dict], depot_transactions: list[dict], wkn_to_isin: dict[str, str],
                    today: date | None = None, names: dict[str, str] | None = None) -> list[Transaction]:
    """Giro bookings and depot transactions (booked only) → transactions, oldest first.

    ``wkn_to_isin`` and ``names`` (ISIN → name) come from the depot positions and
    transactions: dividends name their security by ISIN or WKN in the posting text.
    """
    names = names or {}
    today = today or datetime.now(BERLIN).date()
    bookings = [b for b in bookings if b.get("bookingStatus", "BOOKED") == "BOOKED" and b.get("reference")]
    used: set[str] = set()
    out: list[Transaction] = []

    # Trades: the depot transaction plus the giro booking that settled it.
    for d in depot_transactions:
        if d.get("bookingStatus", "BOOKED") != "BOOKED":
            continue
        kind_key = ((d.get("transactionType") or {}).get("key") if isinstance(d.get("transactionType"), dict)
                    else d.get("transactionType")) or ""
        instrument = d.get("instrument") or {}
        isin = instrument.get("isin") or ""
        wkn = instrument.get("wkn") or ""
        name = instrument.get("name") or instrument.get("shortName") or isin
        business = _day(d.get("businessDate") or d.get("bookingDate"))
        if business is None:
            continue
        tx_id = f"dep-{d.get('transactionId')}"
        shares = _dec(d.get("quantity"))
        gross = _dec(d.get("transactionValue"))
        if kind_key not in ("BUY", "SELL"):
            out.append(_unknown(tx_id, _noon(business), f"DEPOT_{kind_key or 'OTHER'}",
                                {"transactionType": kind_key, "isin": isin, "stück": str(shares or "")}))
            continue
        buy = kind_key == "BUY"
        cash = _settlement(bookings, used, buy, business, gross or Decimal(0), isin, wkn)
        if cash is None:
            if (today - business).days > UNSETTLED_DAYS:
                out.append(_unknown(tx_id, _noon(business), f"{kind_key}_OHNE_KONTOBUCHUNG",
                                    {"isin": isin, "stück": str(shares or ""), "kurswert": str(gross or "")}))
            else:
                log.info("comdirect: %s %s from %s has no giro booking yet", kind_key, isin, business)
            continue
        used.add(cash["reference"])
        net = abs(_dec(cash.get("amount")) or Decimal(0))
        costs = abs(net - gross) if gross is not None else Decimal(0)
        out.append(Transaction(
            id=tx_id, kind=Kind.BUY if buy else Kind.SELL, datetime=_noon(business), currency=_unit(cash.get("amount")),
            net=net, isin=isin or None, name=name, shares=abs(shares) if shares is not None else None,
            gross=gross, fee=costs, label="Kauf" if buy else "Verkauf", external_cash=True))

    # Giro bookings: only dividends are booked (the giro account itself isn't); a payout whose
    # security can't be told is reported.
    for b in bookings:
        if b["reference"] in used or not _is_income(b):
            continue
        value = _dec(b.get("amount")) or Decimal(0)
        text = _text(b)
        if value <= 0 or (INTEREST.search(text) and not DIVIDEND.search(text)):
            continue
        currency = _unit(b.get("amount"))
        when = _noon(_day(b.get("bookingDate")) or _day(b.get("valutaDate")) or today)
        security = _security(text, wkn_to_isin)
        if security:
            out.append(Transaction(id=_account_id(b), kind=Kind.DIVIDEND, datetime=when, currency=currency, net=value,
                                   isin=security, name=names.get(security, security), label="Dividende", text=text))
        else:
            out.append(_unknown(_account_id(b), when, "DIVIDENDE_OHNE_WERTPAPIER",
                                {"betrag": f"{value} {currency}", "buchungstext": text[:140]}, currency))
    return sorted(out, key=lambda t: (t.datetime, t.id))


def _settlement(bookings: list[dict], used: set[str], buy: bool, business: date, gross: Decimal, isin: str,
                wkn: str) -> dict | None:
    """The giro booking that settled a trade: securities type, right sign, near the business date,
    an amount that is the market value plus (buy) or minus (sale) costs - one naming the security first."""
    best = None
    for b in bookings:
        if b["reference"] in used or not _is_securities(b):
            continue
        value = _dec(b.get("amount"))
        day = _day(b.get("bookingDate")) or _day(b.get("valutaDate"))
        if value is None or day is None or (value < 0) != buy:
            continue
        if not -1 <= (day - business).days <= SETTLEMENT_DAYS:
            continue
        amount = abs(value)
        costs = amount - gross if buy else gross - amount
        if costs < Decimal("-0.01") or (gross and costs > gross / 2):
            continue
        text = b.get("remittanceInfo") or ""
        named = bool((isin and isin in text) or (wkn and wkn in text))
        score = (not named, abs((day - business).days), costs)
        if best is None or score < best[0]:
            best = (score, b)
    return best[1] if best else None


def _security(text: str, wkn_to_isin: dict[str, str]) -> str | None:
    m = ISIN.search(text)
    if m:
        return m.group(1)
    for m in WKN.finditer(text):
        if m.group(1) in wkn_to_isin:
            return wkn_to_isin[m.group(1)]
    return None


class ComdirectAdapter(BrokerAdapter):
    key = "comdirect"
    label = "comdirect"
    credential_fields: ClassVar[list[CredentialField]] = [
        CredentialField("username", "Zugangsnummer", help="Die 8-stellige Zugangsnummer des comdirect-Bankings."),
        CredentialField("pin", "PIN", secret=True),
        CredentialField("client_id", "Client-ID",
                        help="Aus dem comdirect-Banking: Persönlicher Bereich → Zugänge verwalten → REST API."),
        CredentialField("client_secret", "Client-Secret", secret=True),
        CredentialField("iban", "IBAN des Girokontos", help="Leer lassen, wenn du nur ein Girokonto hast."),
    ]
    reports_positions = True

    # Replaced in tests with an httpx.MockTransport.
    transport: ClassVar[httpx.BaseTransport | None] = None

    def __init__(self, credentials, session=None):
        super().__init__(credentials, session)
        self._http: httpx.Client | None = None
        self._pending: dict | None = None
        self._accounts: list[dict] | None = None
        self._depots: list[dict] | None = None
        self._positions: list[dict] | None = None

    # ── HTTP ────────────────────────────────────────────────────────────────
    @property
    def http(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(transport=type(self).transport, timeout=60)
        return self._http

    def _request_info(self) -> str:
        sid = self.session.setdefault("session_id", secrets.token_hex(16))
        return json.dumps({"clientRequestId": {"sessionId": sid, "requestId": datetime.now().strftime("%H%M%S%f")[:9]}})

    def _headers(self, token: str, **extra: str) -> dict[str, str]:
        return {"Accept": "application/json", "Content-Type": "application/json",
                "Authorization": f"Bearer {token}", "x-http-request-info": self._request_info(), **extra}

    def _token(self, data: dict) -> httpx.Response:
        creds = {"client_id": self.credentials.get("client_id", ""),
                 "client_secret": self.credentials.get("client_secret", "")}
        try:
            return self.http.post(TOKEN_URL, data={**creds, **data},
                                  headers={"Accept": "application/json",
                                           "Content-Type": "application/x-www-form-urlencoded"})
        except httpx.HTTPError as e:
            raise AdapterError(f"comdirect nicht erreichbar: {e}") from e

    def _keep(self, body: dict) -> None:
        now = time.time()
        self.session["access_token"] = body["access_token"]
        self.session["refresh_token"] = body.get("refresh_token", "")
        self.session["expires_at"] = now + int(body.get("expires_in") or 599)

    def get(self, path: str, **params) -> dict:
        self._ensure_token()
        try:
            r = self.http.get(f"{API}/api{path}", params=params, headers=self._headers(self.session["access_token"]))
        except httpx.HTTPError as e:
            raise AdapterError(f"comdirect nicht erreichbar: {e}") from e
        if r.status_code == 401:
            self._clear_tokens()
            raise AuthRequired(Challenge("confirm", "Die comdirect-Sitzung ist abgelaufen. Bitte neu anmelden."))
        if r.status_code >= 400:
            raise AdapterError(f"comdirect: {_message(r)}")
        return r.json()

    def _pages(self, path: str, **params) -> list[dict]:
        out: list[dict] = []
        while True:
            page = self.get(path, **params, **{"paging-first": len(out), "paging-count": PAGE_SIZE})
            values = page.get("values") or []
            out.extend(values)
            matches = (page.get("paging") or {}).get("matches")
            if not values or matches is None or len(out) >= int(matches):
                return out

    # ── login ───────────────────────────────────────────────────────────────
    def _clear_tokens(self) -> None:
        for k in ("access_token", "refresh_token", "expires_at"):
            self.session.pop(k, None)

    def _ensure_token(self) -> None:
        if not self.session.get("access_token"):
            raise AuthRequired(Challenge("confirm", "Bitte bei comdirect anmelden."))
        if self.session.get("expires_at", 0) - 30 > time.time():
            return
        if not self._refresh():
            raise AuthRequired(Challenge("confirm", "Die comdirect-Sitzung ist abgelaufen. Bitte neu anmelden."))

    def _refresh(self) -> bool:
        if not self.session.get("refresh_token"):
            return False
        r = self._token({"grant_type": "refresh_token", "refresh_token": self.session["refresh_token"]})
        if r.status_code != 200:
            self._clear_tokens()
            return False
        self._keep(r.json())
        return True

    def _guard(self) -> None:
        if self.session.get("pin_rejected"):
            raise AdapterError(
                "comdirect hat Zugangsnummer, PIN oder TAN abgelehnt. Bitte die Zugangsdaten in den "
                "Broker-Einstellungen neu speichern - bis dahin versucht der Dienst es nicht noch einmal, damit "
                "das comdirect-Banking nicht gesperrt wird.")
        if self.session.get("open_challenges", 0) >= MAX_OPEN_CHALLENGES:
            raise AdapterError(
                f"{self.session['open_challenges']} TAN-Anfragen an comdirect wurden nicht bestätigt. Nach fünf "
                "sperrt comdirect das Online-Banking, darum fragt der Dienst nicht weiter. Melde dich einmal mit "
                "TAN auf comdirect.de an und speichere danach die Zugangsdaten in den Broker-Einstellungen neu.")
        missing = [f.label for f in self.credential_fields if f.name != "iban" and not self.credentials.get(f.name)]
        if missing:
            raise AdapterError(f"Für comdirect fehlt: {', '.join(missing)} - bitte in den Broker-Einstellungen "
                               "eintragen.")

    def login(self) -> None:
        if self.session.get("pin_rejected"):
            self._guard()
        if self.session.get("access_token") and (self.session.get("expires_at", 0) - 30 > time.time()
                                                 or self._refresh()):
            return
        self._guard()
        if self.on_user_action and (self.session.get("tan_type") not in (None, "P_TAN_PUSH")
                                    or self.session.get("push_without_link")):
            # A TAN to type, or a push whose approval can't be checked: only the web UI can do
            # that - don't open a challenge nobody answers.
            raise AuthRequired(Challenge("code", "comdirect braucht eine TAN. Bitte in der Weboberfläche anmelden."))
        self._start()
        typ = self._pending["typ"]
        if typ == "P_TAN_PUSH" and self.on_user_action:
            if not self._pending.get("link"):
                self.session["push_without_link"] = True
                raise AuthRequired(Challenge("confirm", "Bitte die comdirect-Anmeldung in der Weboberfläche "
                                                        "bestätigen."))
            self.on_user_action("Bitte bestätige die Anmeldung des Wealthfolio Broker Sync in der "
                                "comdirect photoTAN-App.")
            if not self._wait_push(PUSH_WAIT):
                raise AuthRequired(Challenge("confirm", "Die Anmeldung wurde nicht rechtzeitig in der photoTAN-App "
                                                        "bestätigt."))
            self._activate("")
            return
        raise AuthRequired(self._challenge())

    def _challenge(self, again: bool = False) -> Challenge:
        p = self._pending or {}
        prefix = "Noch nicht bestätigt. " if again else ""
        if p.get("typ") == "P_TAN_PUSH":
            return Challenge("confirm", prefix + "Bestätige die Anmeldung in der comdirect photoTAN-App.")
        if p.get("typ") == "P_TAN":
            return Challenge("code", "Scanne die photoTAN-Grafik mit der comdirect photoTAN-App und gib die TAN ein.",
                             image=f"data:image/png;base64,{p.get('challenge') or ''}")
        if p.get("typ") == "M_TAN":
            return Challenge("code", f"Gib die mobileTAN ein, die an {p.get('challenge') or 'dein Handy'} ging.")
        return Challenge("code", "Gib die TAN ein.")

    def _start(self) -> None:
        r = self._token({"grant_type": "password", "username": self.credentials["username"],
                         "password": self.credentials["pin"]})
        if r.status_code in (400, 401):
            self.session["pin_rejected"] = True
            raise AdapterError(f"comdirect hat die Anmeldung abgelehnt (Zugangsnummer, PIN oder Client-Daten): "
                               f"{_message(r)}")
        if r.status_code != 200:
            raise AdapterError(f"comdirect: {_message(r)}")
        token = r.json()["access_token"]
        try:
            r = self.http.get(f"{API}/api/session/clients/user/v1/sessions", headers=self._headers(token))
            sessions = r.json() if r.status_code == 200 else []
            if not sessions:
                raise AdapterError(f"comdirect: keine Sitzung ({_message(r)})")
            ident = sessions[0]["identifier"]
            body = {"identifier": ident, "sessionTanActive": True, "activated2FA": True}
            # Every challenge counts towards comdirect's limit - before the call, so a crash can't hide one.
            self.session["open_challenges"] = self.session.get("open_challenges", 0) + 1
            r = self.http.post(f"{API}/api/session/clients/user/v1/sessions/{ident}/validate", json=body,
                               headers=self._headers(token))
        except httpx.HTTPError as e:
            raise AdapterError(f"comdirect nicht erreichbar: {e}") from e
        info = r.headers.get("x-once-authentication-info")
        if r.status_code not in (200, 201) or not info:
            raise AdapterError(f"comdirect: TAN-Anforderung fehlgeschlagen ({_message(r)})")
        challenge = json.loads(info)
        self.session["tan_type"] = challenge.get("typ")
        link = (challenge.get("link") or {}).get("href") or ""
        self._pending = {"token": token, "session": body, "id": challenge.get("id"), "typ": challenge.get("typ"),
                         "challenge": challenge.get("challenge"), "link": link}

    def _wait_push(self, seconds: float) -> bool:
        """Poll the challenge's status link until the app approved it."""
        p = self._pending or {}
        if not p.get("link"):
            return True  # nothing to poll: the user says they approved
        url = p["link"] if p["link"].startswith("http") else f"{API}{p['link']}"
        deadline = time.monotonic() + seconds
        while True:
            try:
                r = self.http.get(url, headers=self._headers(p["token"]))
                if r.status_code == 200 and (r.json() or {}).get("status") == "AUTHENTICATED":
                    return True
            except (httpx.HTTPError, ValueError):
                pass
            if time.monotonic() >= deadline:
                return False
            time.sleep(PUSH_POLL)

    def _activate(self, code: str) -> None:
        p = self._pending
        extra = {"x-once-authentication-info": json.dumps({"id": p["id"]})}
        if code:
            extra["x-once-authentication"] = code.strip()
        try:
            r = self.http.patch(f"{API}/api/session/clients/user/v1/sessions/{p['session']['identifier']}",
                                json=p["session"], headers=self._headers(p["token"], **extra))
        except httpx.HTTPError as e:
            raise AdapterError(f"comdirect nicht erreichbar: {e}") from e
        if r.status_code != 200:
            if code:
                self.session["wrong_tans"] = self.session.get("wrong_tans", 0) + 1
                if self.session["wrong_tans"] > MAX_WRONG_TANS:
                    self.session["pin_rejected"] = True
                    raise AdapterError("comdirect hat die TAN zweimal abgelehnt. Nach drei falschen TANs sperrt "
                                       "comdirect das Online-Banking - bitte zuerst einmal auf comdirect.de mit "
                                       "richtiger TAN anmelden und dann die Zugangsdaten neu speichern.")
                raise AuthRequired(Challenge("code", f"comdirect hat die TAN abgelehnt ({_message(r)}). Bitte "
                                                     "prüfen und erneut eingeben."))
            raise AuthRequired(self._challenge(again=True))
        r = self._token({"grant_type": "cd_secondary", "token": p["token"]})
        if r.status_code != 200:
            raise AdapterError(f"comdirect: Anmeldung nicht abgeschlossen ({_message(r)})")
        self._keep(r.json())
        self.session["open_challenges"] = 0
        self.session["wrong_tans"] = 0
        self._pending = None

    def complete_login(self, code: str) -> None:
        if self._pending is None:
            return
        if self._pending["typ"] == "P_TAN_PUSH":
            if not self._wait_push(CONFIRM_WAIT):
                raise AuthRequired(self._challenge(again=True))
            self._activate("")
        else:
            if not code.strip():
                raise AuthRequired(self._challenge())
            self._activate(code)

    def close(self) -> None:
        if self._http is not None:
            self._http.close()
            self._http = None

    @classmethod
    def replay(cls, recording: dict) -> ComdirectAdapter:
        adapter = cls({"username": "replay", "pin": "replay", "client_id": "replay", "client_secret": "replay",
                       "iban": recording.get("iban", "")},
                      {"access_token": "replay", "expires_at": 4102444800})
        adapter._http = httpx.Client(transport=replay_transport(recording))
        adapter.today = date.fromisoformat(recording["today"]) if recording.get("today") else None
        return adapter

    # ── data ────────────────────────────────────────────────────────────────
    today: date | None = None

    def _giro(self) -> dict:
        if self._accounts is None:
            self._accounts = self._pages("/banking/clients/user/v2/accounts/balances")
        wanted = (self.credentials.get("iban") or "").replace(" ", "").upper()
        for b in self._accounts:
            account = b.get("account") or {}
            if wanted and (account.get("iban") or "").replace(" ", "").upper() == wanted:
                return b
        if wanted:
            raise AdapterError(f"Die IBAN {wanted} gehört nicht zu diesem comdirect-Zugang.")
        giro = [b for b in self._accounts if ((b.get("account") or {}).get("accountType") or {}).get("key") == "CA"]
        if not (giro or self._accounts):
            raise AdapterError("comdirect meldet kein Konto für diesen Zugang.")
        return (giro or self._accounts)[0]

    def _all_positions(self) -> list[dict]:
        if self._positions is None:
            if self._depots is None:
                self._depots = self._pages("/brokerage/clients/user/v3/depots")
            self._positions = []
            for d in self._depots:
                self._positions += self.get(f"/brokerage/v3/depots/{d['depotId']}/positions",
                                            **{"with-attr": "instrument"}).get("values") or []
        return self._positions

    def get_accounts(self) -> list[BrokerAccount]:
        giro = self._giro()
        account = giro.get("account") or {}
        return [BrokerAccount(giro.get("accountId", ""), account.get("iban") or account.get("accountDisplayId") or "",
                              account.get("currency") or "EUR")]

    def get_cash(self) -> list[CashBalance]:
        # The giro account isn't booked (only dividends land on the cash account): no balance to compare.
        return []

    def get_positions(self) -> list[Position]:
        out = []
        for p in self._all_positions():
            instrument = p.get("instrument") or {}
            shares = _dec(p.get("quantity"))
            isin = instrument.get("isin") or p.get("wkn")
            if not isin or not shares:
                continue
            out.append(Position(isin, instrument.get("name") or "", shares, _unit(p.get("currentValue")),
                                _dec(p.get("currentValue")), cost=_dec(p.get("purchasePrice"))))
        return out

    def get_transactions(self, since: datetime | None) -> list[Transaction]:
        today = self.today or datetime.now(BERLIN).date()
        start = since.astimezone(BERLIN).date() if since else today - timedelta(days=DEFAULT_DAYS)
        # A day more for the giro bookings: a trade's settlement can lie before its business date.
        first_day = (start - timedelta(days=1)).isoformat()
        bookings = self._pages(f"/banking/v1/accounts/{self._giro()['accountId']}/transactions",
                               transactionState="BOOKED", **{"min-bookingDate": first_day})
        if self._depots is None:
            self._depots = self._pages("/brokerage/clients/user/v3/depots")
        depot_txs: list[dict] = []
        for d in self._depots:
            depot_txs += self._pages(f"/brokerage/v3/depots/{d['depotId']}/transactions", bookingStatus="BOOKED")
        wkn_to_isin = {(p.get("instrument") or {}).get("wkn") or p.get("wkn"): (p.get("instrument") or {}).get("isin")
                       for p in self._all_positions() if (p.get("instrument") or {}).get("isin")}
        names = {(p.get("instrument") or {}).get("isin"): (p.get("instrument") or {}).get("name") or ""
                 for p in self._all_positions()}
        for d in depot_txs:
            i = d.get("instrument") or {}
            if i.get("wkn") and i.get("isin"):
                wkn_to_isin[i["wkn"]] = i["isin"]
            if i.get("isin") and i.get("name"):
                names[i["isin"]] = i["name"]
        log.info("comdirect: %d giro bookings, %d depot transactions since %s", len(bookings), len(depot_txs), start)
        txs = to_transactions(bookings, depot_txs, wkn_to_isin, today, names)
        first = _noon(start) - timedelta(hours=12)
        return [t for t in txs if t.datetime >= first]


def _message(r: httpx.Response) -> str:
    try:
        body = r.json()
    except ValueError:
        return f"HTTP {r.status_code}"
    if isinstance(body, dict):
        msgs = body.get("messages") or []
        text = "; ".join(m.get("message", "") for m in msgs if isinstance(m, dict)) or body.get("error_description") \
            or body.get("error") or body.get("code")
        if text:
            return f"{text} (HTTP {r.status_code})"
    return f"HTTP {r.status_code}"


def replay_transport(recording: dict) -> httpx.MockTransport:
    """Answers the read calls from a recording: balances, giro bookings, depots, positions, depot transactions."""
    def page(values: list) -> httpx.Response:
        return httpx.Response(200, json={"paging": {"index": 0, "matches": len(values)}, "values": values})

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/accounts/balances"):
            return page(recording.get("balances", []))
        if "/banking/" in path and path.endswith("/transactions"):
            return page(recording.get("bookings", []))
        if path.endswith("/v3/depots"):
            return page(recording.get("depots", []))
        if path.endswith("/positions"):
            return page(recording.get("positions", []))
        if "/brokerage/" in path and path.endswith("/transactions"):
            return page(recording.get("depot_transactions", []))
        return httpx.Response(404, json={"code": "not.found"})

    return httpx.MockTransport(handle)
