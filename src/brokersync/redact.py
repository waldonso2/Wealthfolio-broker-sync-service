"""Keeps secrets and account data out of the log.

Every string the vault holds (credentials, tokens, sessions) is registered when
the vault is read; a logging filter replaces them, and anything that looks
like an IBAN or a long account/card number, in every log record. It is a
safety net: code doesn't log secrets in the first place.
"""

from __future__ import annotations

import logging
import re

MASK = "***"
# IBANs (with or without spaces) and runs of 8+ digits (account, card, customer numbers).
PATTERNS = [
    re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){3,7}(?: ?[A-Z0-9]{1,3})?\b"),
    re.compile(r"(?<![\d.:-])\d{8,}(?![\d.:-])"),
]
# Shorter values (a PIN of 4 digits, "EUR", "true") would mask ordinary text.
MIN_SECRET = 6

_secrets: set[str] = set()


def register(data) -> None:
    """Remember every string in ``data`` (nested dicts/lists) as a secret."""
    if isinstance(data, str):
        if len(data) >= MIN_SECRET:
            _secrets.add(data)
    elif isinstance(data, dict):
        for v in data.values():
            register(v)
    elif isinstance(data, list | tuple):
        for v in data:
            register(v)


def register_long(data, min_length: int = 20) -> None:
    """Like ``register``, for strings of at least ``min_length`` characters only."""
    if isinstance(data, str):
        if len(data) >= min_length:
            _secrets.add(data)
    elif isinstance(data, dict):
        for v in data.values():
            register_long(v, min_length)
    elif isinstance(data, list | tuple):
        for v in data:
            register_long(v, min_length)


def redact(text: str) -> str:
    for s in sorted(_secrets, key=len, reverse=True):
        if s in text:
            text = text.replace(s, MASK)
    for p in PATTERNS:
        text = p.sub(MASK, text)
    return text


class RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        clean = redact(message)
        if record.exc_info and record.exc_info[1] is not None:
            exc = record.exc_info[1]
            record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
            del exc
        if clean != message or record.args:
            record.msg, record.args = clean, None
        return True


def install() -> None:
    """Add the filter to every handler of the root logger (call after logging.basicConfig)."""
    f = RedactFilter()
    for h in logging.getLogger().handlers:
        h.addFilter(f)
