"""Checks for what the forms of the web UI accept, on the server (the HTML ``pattern``s are only a help).

Each returns the cleaned value or raises ``Invalid`` with a German text for the page. Free text is capped
at ``MAX_TEXT`` characters: everything ends up in the encrypted config, the vault or a log line.
"""

from __future__ import annotations

import re
from datetime import date
from urllib.parse import urlparse

from ..assets import valid_isin

MAX_TEXT = 500
ISIN = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}\d$")
IBAN = re.compile(r"^[A-Z]{2}\d{2}[A-Z0-9]{11,30}$")
MIC = re.compile(r"^[A-Z0-9]{4}$")
# A Wealthfolio symbol: ticker, ISIN or a provider form like "SAP.DE", "BRK-B", "^GDAXI".
SYMBOL = re.compile(r"^[A-Za-z0-9.^:_-]{1,32}$")


class Invalid(ValueError):
    pass


def text(value: str, label: str, *, max_len: int = MAX_TEXT) -> str:
    value = (value or "").strip()
    if len(value) > max_len:
        raise Invalid(f"{label}: höchstens {max_len} Zeichen.")
    if any(ord(c) < 32 for c in value):
        raise Invalid(f"{label}: enthält Steuerzeichen.")
    return value


def http_url(value: str, label: str, *, required: bool = True) -> str:
    """An http(s) address with a host, without user info, query or fragment; trailing slash removed."""
    value = text(value, label).rstrip("/")
    if not value and not required:
        return ""
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise Invalid(f"{label}: bitte eine Adresse mit http:// oder https:// angeben.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise Invalid(f"{label}: ohne Zugangsdaten, ? oder # in der Adresse.")
    try:
        parsed.port  # noqa: B018 - raises for an invalid port
    except ValueError as e:
        raise Invalid(f"{label}: ungültiger Port.") from e
    return value


def iso_date(value: str, label: str) -> str:
    value = text(value, label)
    if not value:
        return ""
    try:
        day = date.fromisoformat(value)
    except ValueError as e:
        raise Invalid(f"{label}: bitte als Datum angeben (JJJJ-MM-TT).") from e
    if day > date.today():
        raise Invalid(f"{label}: darf nicht in der Zukunft liegen.")
    return day.isoformat()


def isin(value: str) -> str:
    value = text(value, "ISIN").replace(" ", "").upper()
    if not ISIN.match(value) or not valid_isin(value):
        raise Invalid("Keine gültige ISIN (12 Zeichen, Prüfziffer).")
    return value


SECURITY_ID = re.compile(r"^[A-Z0-9][A-Z0-9|/._-]{1,39}$")


def security_id(value: str) -> str:
    """The id a broker reports a position under: an ISIN as a rule, but not always one (crypto, older parsers)."""
    value = text(value, "Wertpapier").upper()
    if not SECURITY_ID.match(value):
        raise Invalid("Unbekanntes Wertpapier-Kennzeichen.")
    return value


def iban(value: str) -> str:
    """Empty, or an IBAN with a valid check sum (ISO 13616, mod 97)."""
    value = text(value, "IBAN").replace(" ", "").upper()
    if not value:
        return ""
    if not IBAN.match(value) or int("".join(str(int(c, 36)) for c in value[4:] + value[:4])) % 97 != 1:
        raise Invalid("Keine gültige IBAN (Prüfsumme stimmt nicht).")
    return value


def mic(value: str) -> str | None:
    value = text(value, "Börse").upper()
    if not value:
        return None
    if not MIC.match(value):
        raise Invalid("Börse: ein MIC aus 4 Zeichen, z. B. XETR.")
    return value


def symbol(value: str) -> str:
    value = text(value, "Symbol")
    if not SYMBOL.match(value):
        raise Invalid("Symbol: 1-32 Zeichen, nur Buchstaben, Ziffern und . ^ : _ -")
    return value
