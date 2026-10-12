"""How the pages show dates, times, amounts and unknown bookings (#73).

Every date and time on a page is German time (``TZ``) in German format: ``TT.MM.JJJJ`` for a day,
``TT.MM.JJJJ HH:MM`` for a moment. Wealthfolio and the state store UTC; a value without a time
zone is UTC too.
"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

# Times are shown in German time; the container usually runs in UTC.
TZ = ZoneInfo(os.environ.get("BROKERSYNC_TZ", "Europe/Berlin"))


def _parse(value) -> datetime | date | None:
    if not value:
        return None
    if isinstance(value, datetime | date):
        return value
    text = str(value).strip()
    try:
        if len(text) == 10:
            return date.fromisoformat(text)
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _local(moment: datetime) -> datetime:
    return (moment if moment.tzinfo else moment.replace(tzinfo=UTC)).astimezone(TZ)


def local_time(value, seconds: bool = False) -> str:
    """A moment in German time: 03.03.2026 11:00 (with ``seconds``: 11:00:01)."""
    parsed = _parse(value)
    if parsed is None:
        return str(value or "")
    if not isinstance(parsed, datetime):
        return parsed.strftime("%d.%m.%Y")
    return _local(parsed).strftime("%d.%m.%Y %H:%M:%S" if seconds else "%d.%m.%Y %H:%M")


def local_day(value) -> str:
    """The day of a date or moment in German time: 03.03.2026."""
    parsed = _parse(value)
    if parsed is None:
        return str(value or "")
    if isinstance(parsed, datetime):
        parsed = _local(parsed)
    return parsed.strftime("%d.%m.%Y")


def utc_iso(value) -> str:
    """The machine-readable form for ``<time datetime>``: ISO 8601 in UTC."""
    parsed = _parse(value)
    if isinstance(parsed, datetime):
        return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).astimezone(UTC).isoformat()
    return parsed.isoformat() if parsed else ""


def money(value) -> str:
    """German number format: 1.234,56."""
    return f"{Decimal(str(value)):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _amount(text) -> str:
    """'-12.5 EUR' -> '-12,50 EUR'; anything that isn't a number stays as it is."""
    parts = str(text).split()
    try:
        number = money(Decimal(parts[0].replace(",", ".") if parts[0].count(",") == 1 and "." not in parts[0]
                               else parts[0]))
    except (InvalidOperation, IndexError):
        return str(text)
    return " ".join([number, *parts[1:]])


# What the adapters put into an unknown booking's payload, by meaning. The payloads keep only what
# is needed to book it by hand (`State.add_unknown` masks IBANs and long numbers).
AMOUNT_KEYS = ("betrag", "Ausmachender Betrag", "kurswert")
SECURITY_KEYS = ("Bezeichnung", "isin", "ISIN", "related_isin")
SHARES_KEYS = ("stück", "quantity", "Nominal / Stück")
TEXT_KEYS = ("title", "subtitle", "buchungstext", "verwendungszweck", "hinweis", "status")


def _first(payload: dict, keys: tuple[str, ...]) -> list[str]:
    return [str(payload[k]) for k in keys if payload.get(k) not in (None, "")]


def describe_unknown(payload: dict | None) -> dict[str, str]:
    """The fields of an unknown booking someone needs to enter it by hand: amount, security, shares, text."""
    payload = payload or {}
    amount = _first(payload, AMOUNT_KEYS)
    securities = list(dict.fromkeys(_first(payload, SECURITY_KEYS)))
    shares = _first(payload, SHARES_KEYS)
    texts = list(dict.fromkeys(_first(payload, TEXT_KEYS)))
    return {
        "amount": _amount(amount[0]) if amount else "",
        "security": " · ".join(securities),
        "shares": shares[0] if shares else "",
        "text": " · ".join(texts),
    }


# How old the last run may be before the overview turns yellow: the timer runs daily.
STALE_DAYS = 2


def broker_light(last: dict | None, reconcile: dict | None, unknown: int, gaps: int, enabled: bool,
                 now: datetime | None = None) -> tuple[str, list[str]]:
    """The overview's traffic light for a broker and the reasons behind it (#83).

    red: the last run failed or needs a login; yellow: something to look at (deviations, unknown or missing
    bookings, failed activities, no run for a while); green: nothing to do; grey: never fetched.
    """
    if not last:
        return "grey", ["Noch nie abgerufen"]
    status = last.get("status")
    if status == "needs_auth":
        return "red", ["Anmeldung nötig"]
    if status == "error":
        failed = f" ({last['failed']} Buchungen)" if last.get("failed") else ""
        return "red", [f"Letzter Abruf fehlgeschlagen{failed}"]
    reasons = []
    deviations = (reconcile or {}).get("deviations") or []
    if deviations:
        reasons.append(f"{len(deviations)} Abweichung{'en' if len(deviations) != 1 else ''} zu Wealthfolio")
    if unknown:
        reasons.append(f"{unknown} unbekannte Buchung{'en' if unknown != 1 else ''}")
    if gaps:
        reasons.append(f"{gaps} fehlt in Wealthfolio")
    if status == "aborted":
        reasons.append("Letzter Abruf abgebrochen")
    started = _parse(last.get("started_at"))
    if enabled and isinstance(started, datetime) and status != "running":
        age = (now or datetime.now(UTC)) - (started if started.tzinfo else started.replace(tzinfo=UTC))
        if age.days >= STALE_DAYS:
            reasons.append(f"Seit {age.days} Tagen kein Abruf")
    return ("yellow" if reasons else "green"), reasons
