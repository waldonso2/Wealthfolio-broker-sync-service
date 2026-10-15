"""Depot holdings from MT535 (FinTS HKWPD), more tolerant than python-fints' own parser.

python-fints' ``MT535_Miniparser`` fails on a holding without ``:19A:`` (its
total value is never set) and knows shares only as ``UNIT`` with a comma. This
one reads, per ``:16R:FIN`` … ``:16S:FIN`` block:

- ``:35B:ISIN <isin>`` with the WKN (``/DE/…``) and the name on the next lines,
- ``:93B:`` - shares (``UNIT``) or nominal (``FAMT``), the ``AGGR`` balance first,
- ``:19A::HOLD//`` - the holding's currency (its value isn't used: a bond priced in per cent (``PRCT``) would
  need the nominal, #118),
- ``:90A:``/``:90B:`` - the price's currency, if there is no ``:19A:``.

``structure`` describes an answer without its values (tags and qualifiers), so a
bank's format can be checked from the log without personal data.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


@dataclass
class Holding:
    """What the adapter reads of python-fints' ``Holding`` (same attribute names)."""

    ISIN: str
    name: str
    pieces: Decimal | None
    value_symbol: str | None


AMOUNT = re.compile(r"(N?)([A-Z]{3})?(\d+(?:,\d*)?)")  # [N]EUR123,45


def _number(text: str) -> Decimal | None:
    try:
        return Decimal(text.replace(",", "."))
    except InvalidOperation:
        return None


def clauses(lines: list[str]) -> list[str]:
    """Fields with their continuation lines joined by ``|``."""
    out: list[str] = []
    for line in lines:
        line = line.rstrip("\r")
        if line.startswith(":") or line.startswith("-"):
            out.append(line)
        elif out and line:
            out[-1] += "|" + line
    return out


def _blocks(fields: list[str]) -> list[list[str]]:
    blocks: list[list[str]] = []
    current: list[str] | None = None
    for f in fields:
        if f.startswith(":16R:FIN"):
            current = []
        elif f.startswith(":16S:FIN"):
            if current is not None:
                blocks.append(current)
            current = None
        elif current is not None:
            current.append(f)
    return blocks


def _isin_and_name(field: str) -> tuple[str | None, str | None]:
    """``:35B:ISIN <isin>`` with the WKN (``/DE/…``) and the name on the next lines."""
    parts = field[5:].split("|")
    m = re.match(r"ISIN\s+([A-Z]{2}[A-Z0-9]{9}\d)", parts[0])
    rest = [p.strip() for p in parts[1:] if p.strip() and not p.strip().startswith("/")]
    return (m.group(1) if m else None), (" ".join(rest) or None)


def _pieces(field: str) -> Decimal | None:
    m = re.search(r"/[A-Z0-9]*/(?:UNIT|FAMT)/(N?)(\d+(?:,\d*)?)", field)
    if not m:
        return None
    value = _number(m.group(2))
    return -value if value is not None and m.group(1) else value


def _currency(field: str) -> str | None:
    """Of the holding's value (``:19A::HOLD//``) or its price (``:90A:``/``:90B:``)."""
    if field.startswith(":19A::HOLD//"):
        m = AMOUNT.match(field[12:])
        return m.group(2) if m else None
    m = re.search(r"/[A-Z0-9]*/(?:ACTU|PRCT)/([A-Z]{3})?\d", field)
    return m.group(1) if m else None


def _holding(block: list[str]) -> Holding | None:
    isin = name = currency = price_currency = None
    pieces = None
    for f in block:
        if f.startswith(":35B:"):
            isin, name = _isin_and_name(f)
        elif f.startswith(":93B:") and (pieces is None or "::AGGR//" in f):
            pieces = _pieces(f)
        elif f.startswith(":19A::HOLD//"):
            currency = _currency(f) or currency
        elif f.startswith((":90A:", ":90B:")):
            price_currency = price_currency or _currency(f)
    if not isin:
        return None
    return Holding(isin, name or isin, pieces, currency or price_currency)


def parse(lines: list[str]) -> list[Holding]:
    return [h for h in (_holding(b) for b in _blocks(clauses(lines))) if h]


def structure(lines: list[str]) -> str:
    """The answer's layout without values: how often each tag and qualifier occurs."""
    tags: Counter[str] = Counter()
    for f in clauses(lines):
        m = re.match(r"(:\d{2}[A-Z]?:)(:?[A-Z0-9]{4}//)?(?:(UNIT|FAMT|ACTU|PRCT|FIN)\b)?", f)
        if m:
            tags["".join(g for g in m.groups() if g)] += 1
    return ", ".join(f"{t}×{n}" for t, n in tags.items()) or "leer"
