"""Depot holdings from MT535 (FinTS HKWPD), more tolerant than python-fints' own parser.

python-fints' ``MT535_Miniparser`` fails on a holding without ``:19A:`` (its
total value is never set) and knows shares only as ``UNIT`` with a comma. This
one reads, per ``:16R:FIN`` … ``:16S:FIN`` block:

- ``:35B:ISIN <isin>`` with the WKN (``/DE/…``) and the name on the next lines,
- ``:93B:`` - shares (``UNIT``) or nominal (``FAMT``), the ``AGGR`` balance first,
- ``:19A::HOLD//`` - the holding's value and currency (sign ``N`` = negative),
- ``:90A:``/``:90B:`` - the price, if there is no ``:19A:``.

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
    total_value: Decimal | None


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


def _holding(block: list[str]) -> Holding | None:
    isin = name = currency = None
    pieces = total = price = None
    for f in block:
        if f.startswith(":35B:"):
            parts = f[5:].split("|")
            m = re.match(r"ISIN\s+([A-Z]{2}[A-Z0-9]{9}\d)", parts[0])
            if m:
                isin = m.group(1)
            rest = [p.strip() for p in parts[1:] if p.strip() and not p.strip().startswith("/")]
            name = " ".join(rest) or None
        elif f.startswith(":93B:") and (pieces is None or "::AGGR//" in f):
            m = re.search(r"/[A-Z0-9]*/(?:UNIT|FAMT)/(N?)(\d+(?:,\d*)?)", f)
            if m:
                value = _number(m.group(2))
                pieces = -value if value is not None and m.group(1) else value
        elif f.startswith(":19A::HOLD//"):
            m = AMOUNT.match(f[12:])
            if m:
                currency = m.group(2) or currency
                value = _number(m.group(3))
                total = -value if value is not None and m.group(1) else value
        elif f.startswith((":90A:", ":90B:")) and price is None:
            m = re.search(r"/[A-Z0-9]*/(?:ACTU|PRCT)/([A-Z]{3})?(\d+(?:,\d*)?)", f)
            if m:
                price = _number(m.group(2))
                currency = currency or m.group(1)
    if not isin:
        return None
    if total is None and price is not None and pieces is not None:
        total = price * pieces
    return Holding(isin, name or isin, pieces, currency, total)


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
