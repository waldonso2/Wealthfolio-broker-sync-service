"""Trade Republic via its unofficial app API, using pytr (read-only).

- **Login:** the web login (pytr's v2 login, as app.traderepublic.com): phone
  number and PIN, then a confirmation in the Trade Republic app - or a code
  from an authenticator app, if the account uses one. Unlike a device reset it
  doesn't log out the phone. The session cookies are kept (encrypted) in the
  session and resumed by later runs; once Trade Republic ends the session, a
  scheduled run asks for the confirmation via ntfy and waits for it.
- **Data:** the timeline (transactions and activity log) with each event's
  details, parsed by pytr's ``Event`` - the same parser pytr's own exports use.
  Events are mapped to the rules of the addon's CSV import (``transform.ts``):
  trades with fee and tax, dividends net with withholding tax, Saveback as a
  bonus-funded buy, interest, Vorabpauschale, tax corrections, deposits, card
  payments and transfers. Corporate actions, securities transfers and private
  markets are reported as unknown: the CSV import handles them.
- **Purely informational events** (orders created/cancelled, documents, ...),
  listed by pytr, are skipped silently; anything else pytr can't classify is
  reported.
- **Status:** cancelled/failed events are skipped.

The API is unofficial and may change; pytr follows those changes, so it is
pinned to a known version and updated deliberately.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from ..model import BrokerAccount, CashBalance, Kind, Position, Transaction
from .base import AdapterError, AuthRequired, BrokerAdapter, Challenge, CredentialField

SKIPPED_STATUS = {"CANCELED", "CANCELLED", "FAILED", "REJECTED", "PENDING"}
# Seconds to wait for a websocket answer.
TIMEOUT = 30
# Detail requests in flight at once.
BATCH = 20


def _dec(x) -> Decimal | None:
    # pytr hands out floats; round to 8 places and drop trailing zeros (no exponent).
    if x is None:
        return None
    d = Decimal(str(round(float(x), 8)))
    return d.quantize(Decimal(1)) if d == d.to_integral() else d.normalize()


def to_transactions(events: list[dict]) -> list[Transaction]:
    """Timeline events (with ``details``) → transactions, via pytr's parser."""
    from pytr.event import (
        ConditionalEventType,
        Event,
        PPEventType,
        events_known_ignored,
        events_known_ignored_subtitle,
        events_known_ignored_title,
    )

    out: list[Transaction] = []
    for raw in events:
        status = (raw.get("status") or "").upper()
        if status in SKIPPED_STATUS:
            continue
        event_type = (raw.get("eventType") or "").upper()
        try:
            ev = Event.from_dict(raw)
        except Exception as e:  # pytr can't read it: report, don't drop
            out.append(_unknown(raw, f"unreadable: {type(e).__name__}"))
            continue
        kind = ev.event_type
        if kind is None:
            if (event_type in events_known_ignored or raw.get("title") in events_known_ignored_title
                    or raw.get("subtitle") in events_known_ignored_subtitle) or raw.get("amount") is None:
                continue  # informational, no money or shares moved
            out.append(_unknown(raw, event_type or raw.get("subtitle") or raw.get("title") or "?"))
            continue

        value = _dec(ev.value) or Decimal(0)
        fees = abs(_dec(ev.fees) or Decimal(0))
        taxes = abs(_dec(ev.taxes) or Decimal(0))
        shares = abs(_dec(ev.shares)) if ev.shares is not None else None
        base = dict(id=raw["id"], datetime=ev.date.astimezone(UTC), currency=_currency(raw), label=_label(raw),
                    raw_type=event_type)
        security = dict(isin=ev.isin, name=ev.title or "", shares=shares)

        if kind == ConditionalEventType.TRADE_INVOICE and not ev.shares2:
            if value < 0:  # buy: paid = market value + fee + tax
                out.append(Transaction(kind=Kind.BUY, net=-value, gross=-value - fees - taxes, fee=fees, tax=taxes,
                                       **base, **security))
            elif shares and value > 0:  # sell: received = market value - fee - tax
                out.append(Transaction(kind=Kind.SELL, net=value, gross=value + fees + taxes, fee=fees, tax=taxes,
                                       **base, **security))
            else:  # e.g. "Wertlos" (worthless) - handled by the CSV import
                out.append(_unknown(raw, f"{event_type} {raw.get('subtitle') or ''}".strip()))
        elif kind == ConditionalEventType.SAVEBACK and value < 0:
            out.append(Transaction(kind=Kind.BUY, net=-value, gross=-value - fees, fee=fees, bonus_funded=True,
                                   **base, **security))
        elif kind == PPEventType.DIVIDEND and value > 0 and ev.isin and shares:
            out.append(Transaction(kind=Kind.DIVIDEND, net=value, gross=value + taxes, tax=taxes, **base, **security))
        elif kind == PPEventType.INTEREST and value > 0:
            out.append(Transaction(kind=Kind.INTEREST, net=value, gross=value + taxes, tax=taxes, **base,
                                   name=ev.title or ""))
        elif kind == PPEventType.TAXES and value < 0:
            out.append(Transaction(kind=Kind.TAX, net=-value, **base, name=_named(ev)))
        elif kind == PPEventType.TAX_REFUND and value > 0:
            out.append(Transaction(kind=Kind.TAX_REFUND, net=value, **base, name=_named(ev)))
        elif kind in (PPEventType.DEPOSIT, PPEventType.REMOVAL) and value != 0:
            # Money in is always a deposit (addon rule); money out is spending
            # unless a transfer pattern says it went to an own account.
            inbound = value > 0
            out.append(Transaction(kind=Kind.DEPOSIT if inbound else Kind.WITHDRAWAL, net=abs(value),
                                   counterparty=ev.title or "", counterparty_iban=_detail(raw, "IBAN"),
                                   **{**base, "label": _cash_label(raw, inbound)}))
        else:
            # Corporate actions, securities transfers, private markets, swaps ...
            name = kind.name if hasattr(kind, "name") else str(kind)
            out.append(_unknown(raw, f"{event_type or name} ({name})"))
    return out


def _cash_label(raw: dict, inbound: bool) -> str:
    if raw.get("subtitle"):
        return raw["subtitle"]
    event_type = (raw.get("eventType") or "").upper()
    if "CARD" in event_type:
        return "Kartenerstattung" if inbound else "Kartenzahlung"
    return "Einzahlung" if inbound else "Auszahlung"


def _detail(raw: dict, title: str) -> str:
    """Text of a detail row (e.g. the counterparty's IBAN), searched in all sections."""
    stack = list((raw.get("details") or {}).get("sections") or [])
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            if item.get("title") == title and isinstance(item.get("detail"), dict):
                text = item["detail"].get("text")
                if isinstance(text, str):
                    return text.replace(" ", "").upper()
            stack.extend(v for v in item.values() if isinstance(v, (list, dict)))
        elif isinstance(item, list):
            stack.extend(item)
    return ""


def _named(ev) -> str:
    return f"{ev.title} {ev.isin}" if ev.isin else (ev.title or "")


def _label(raw: dict) -> str:
    return (raw.get("subtitle") or raw.get("title") or "").strip()


def _currency(raw: dict) -> str:
    return (raw.get("amount") or {}).get("currency") or "EUR"


def _unknown(raw: dict, raw_type: str) -> Transaction:
    ts = raw.get("timestamp") or "1970-01-01T00:00:00.000+0000"
    ts = ts[:-2] + ":" + ts[-2:] if ts[-5] in "+-" else ts
    try:
        when = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        when = datetime.now(UTC)
    # Only what identifies the event type - no names, amounts or ids beyond the event's own.
    return Transaction(id=raw.get("id", ""), kind=Kind.UNKNOWN, datetime=when, currency=_currency(raw),
                       net=Decimal(0), label=_label(raw), raw_type=raw_type,
                       raw={"eventType": raw.get("eventType"), "title": raw.get("title"),
                            "subtitle": raw.get("subtitle"), "status": raw.get("status")})


class TradeRepublicAdapter(BrokerAdapter):
    key = "tr"
    label = "Trade Republic"
    credential_fields = [
        CredentialField("phone", "Telefonnummer", help="Mit Ländervorwahl, z. B. +4917612345678."),
        CredentialField("pin", "PIN", secret=True, help="Die 4-stellige PIN der Trade-Republic-App."),
    ]

    # Replaced in tests; real runs use pytr.
    reports_positions = True

    api_factory = None

    def __init__(self, credentials, session=None):
        super().__init__(credentials, session)
        self._api = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._tmp: tempfile.TemporaryDirectory | None = None
        self._replay: dict | None = None

    # ── login ───────────────────────────────────────────────────────────────
    def _make_api(self):
        phone = (self.credentials.get("phone") or "").replace(" ", "")
        pin = self.credentials.get("pin") or ""
        if not phone or not pin:
            raise AdapterError("Telefonnummer und PIN fehlen - bitte in den Broker-Einstellungen eintragen.")
        # pytr keeps cookies in a file; it lives only while this adapter does.
        self._tmp = tempfile.TemporaryDirectory(prefix="brokersync-tr-")
        cookies = Path(self._tmp.name) / "cookies.txt"
        fd = os.open(cookies, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(self.session.get("cookies") or "# Netscape HTTP Cookie File\n")
        factory = type(self).api_factory or _pytr_api
        return factory(phone, pin, cookies)

    def login(self) -> None:
        if self.session.get("pin_rejected"):
            raise AdapterError(
                "Trade Republic hat Telefonnummer oder PIN abgelehnt. Bitte in den Broker-Einstellungen neu "
                "eintragen - bis dahin versucht der Dienst es nicht noch einmal."
            )
        self._api = self._make_api()
        try:
            if self.session.get("cookies") and self._api.resume_websession():
                self._keep_cookies()
                return
            self._api.initiate_weblogin()
        except Exception as e:
            self._fail(e)
        if self._api.weblogin_needs_authenticator:
            raise AuthRequired(Challenge("code", "Gib den Code aus deiner Authenticator-App ein."))
        if self.on_user_action:
            # Scheduled run: ask on the phone and wait for it (pytr polls until
            # Trade Republic's deadline, about two minutes).
            self.on_user_action("Bitte bestätige die Anmeldung des Wealthfolio Broker Sync in der Trade-Republic-App.")
            self.complete_login("")
            return
        raise AuthRequired(Challenge("confirm", "Bestätige die Anmeldung in der Trade-Republic-App."))

    def complete_login(self, code: str) -> None:
        try:
            self._api.complete_weblogin(code or None)
        except TimeoutError as e:
            raise AuthRequired(Challenge("confirm", "Die Anmeldung wurde nicht rechtzeitig bestätigt. "
                                                    "Bitte neu starten.")) from e
        except Exception as e:
            self._fail(e)
        self._keep_cookies()

    def _keep_cookies(self) -> None:
        jar = self._api._websession.cookies
        if hasattr(jar, "save"):
            jar.save(ignore_discard=True)
            self.session["cookies"] = Path(jar.filename).read_text()

    def _fail(self, e: Exception):
        text = str(e)
        status = getattr(getattr(e, "response", None), "status_code", None)
        if status in (400, 401, 403) and "login" in str(getattr(getattr(e, "response", None), "url", "")):
            self.session["pin_rejected"] = True
            raise AdapterError("Trade Republic hat Telefonnummer oder PIN abgelehnt.") from e
        if "TOO_MANY_REQUESTS" in text or "Too many attempts" in text:
            raise AdapterError("Zu viele Anmeldeversuche bei Trade Republic - bitte später erneut versuchen.") from e
        if isinstance(e, AuthRequired | AdapterError):
            raise e
        raise AdapterError(f"Trade Republic: {text or type(e).__name__}") from e

    def close(self) -> None:
        if self._api is not None and self._loop is not None:
            try:
                self._loop.run_until_complete(self._api.close())
            except Exception:
                pass
        if self._loop is not None:
            self._loop.close()
            self._loop = None
        if self._tmp is not None:
            self._tmp.cleanup()
            self._tmp = None

    # ── websocket ───────────────────────────────────────────────────────────
    def _run(self, coro):
        # One event loop for the adapter's life: pytr's websocket belongs to it.
        if self._loop is None:
            self._loop = asyncio.new_event_loop()
        try:
            return self._loop.run_until_complete(asyncio.wait_for(coro, TIMEOUT * 10))
        except (AuthRequired, AdapterError):
            raise
        except Exception as e:
            self._fail(e)

    async def _one(self, subscribe):
        """Subscribe and return the first answer for that subscription."""
        sub_id = await subscribe
        while True:
            got_id, _, response = await asyncio.wait_for(self._api.recv(), TIMEOUT)
            if got_id == sub_id:
                await self._api.unsubscribe(sub_id)
                return response

    async def _timeline(self, since: datetime | None) -> list[dict]:
        events: dict[str, dict] = {}
        for fetch in (self._api.timeline_transactions, self._api.timeline_activity_log):
            after = None
            while True:
                page = await self._one(fetch(after))
                older = False
                for item in page.get("items", []):
                    ts = _parse_ts(item.get("timestamp"))
                    if since and ts and ts < since:
                        older = True
                        break
                    events.setdefault(item["id"], item)
                after = (page.get("cursors") or {}).get("after")
                if older or not after:
                    break
        wanted = [e for e in events.values()
                  if (e.get("action") or {}).get("type") == "timelineDetail"
                  and (e.get("action") or {}).get("payload") == e["id"]]
        for i in range(0, len(wanted), BATCH):
            batch = wanted[i:i + BATCH]
            pending = {await self._api.timeline_detail_v2(e["id"]): e for e in batch}
            while pending:
                got_id, _, response = await asyncio.wait_for(self._api.recv(), TIMEOUT)
                event = pending.pop(got_id, None)
                if event is not None:
                    event["details"] = response
                    await self._api.unsubscribe(got_id)
        return sorted(events.values(), key=lambda e: e.get("timestamp") or "")

    # ── data ────────────────────────────────────────────────────────────────
    def get_transactions(self, since: datetime | None) -> list[Transaction]:
        if self._replay is not None:
            events = [e for e in self._replay.get("events", [])
                      if since is None or (_parse_ts(e.get("timestamp")) or since) >= since]
            return to_transactions(events)
        return to_transactions(self._run(self._timeline(since)))

    def get_cash(self) -> list[CashBalance]:
        cash = self._replay.get("cash", []) if self._replay is not None else self._run(self._one(self._api.cash()))
        return [CashBalance(c.get("currencyId", "EUR"), _dec(c.get("amount")) or Decimal(0)) for c in cash or []]

    def get_positions(self) -> list[Position]:
        if self._replay is not None:
            portfolio = self._replay.get("portfolio", {})
        else:
            portfolio = self._run(self._one(self._api.compact_portfolio()))
        return [Position(p.get("instrumentId", ""), p.get("name", ""), _dec(p.get("netSize")) or Decimal(0), "EUR")
                for p in (portfolio or {}).get("positions", [])]

    def get_accounts(self) -> list[BrokerAccount]:
        return [BrokerAccount("tr", "Trade Republic", "EUR")]

    @classmethod
    def replay(cls, recording: dict) -> TradeRepublicAdapter:
        """Recording: ``{"events": [timeline items with "details"], "cash": [...], "portfolio": {...}}``."""
        adapter = cls({"phone": "+490000000000", "pin": "0000"})
        adapter._replay = recording
        return adapter


def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    ts = ts.replace("Z", "+00:00")
    if len(ts) > 5 and ts[-5] in "+-" and ":" not in ts[-5:]:
        ts = ts[:-2] + ":" + ts[-2:]
    try:
        return datetime.fromisoformat(ts).astimezone(UTC)
    except ValueError:
        return None


def _pytr_api(phone: str, pin: str, cookies: Path):
    from pytr.api import TradeRepublicApi

    return TradeRepublicApi(phone_no=phone, pin=pin, save_cookies=True, cookies_file=str(cookies),
                            use_v2_login=True)
