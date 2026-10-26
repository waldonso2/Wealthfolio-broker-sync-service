"""ETF look-through (waldonso2/wealthfolio-importer-addon#139): what the depot really holds - pure, no I/O.

A holding is a fund with its constituents (weights as a share of the fund, from the issuer's file) or a
single stock (itself at weight 1). From them:

- ``exposures``: every company once, its share of the depot and through which holdings - a stock in
  three ETFs and held directly is one row. What a fund's weights leave open is ``UNASSIGNED``, never
  scaled away.
- ``overlap``: how much of two holdings is the same (sum over companies of the smaller weight).
- ``own_share``: the part of a holding no other holding brings.
- ``effective_count``: 1 / sum of squared shares - how many equally weighted stocks the depot is worth.

Companies are matched by ``Constituent.key`` (the ISIN, else what the source gives); lines of one fund
with the same key (share classes) are added up.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

UNASSIGNED = ""  # key of what the funds' weights leave open


@dataclass(frozen=True)
class Constituent:
    key: str
    name: str
    weight: Decimal  # share of the fund, 0..1
    country: str = ""
    sector: str = ""
    currency: str = ""


@dataclass(frozen=True)
class Holding:
    id: str  # Wealthfolio asset id
    name: str
    value: Decimal  # in the base currency
    constituents: tuple[Constituent, ...]


def stock(id: str, isin: str, name: str, value: Decimal, **details: str) -> Holding:
    return Holding(id, name, value, (Constituent(isin, name, Decimal(1), **details),))


@dataclass
class Exposure:
    key: str
    name: str
    country: str
    sector: str
    currency: str
    value: Decimal = Decimal(0)
    share: Decimal = Decimal(0)  # of the depot
    via: dict[str, Decimal] = field(default_factory=dict)  # holding id -> share of the depot


def weights(holding: Holding) -> dict[str, Decimal]:
    """Weight per company key; lines with the same key added up."""
    out: dict[str, Decimal] = {}
    for c in holding.constituents:
        out[c.key] = out.get(c.key, Decimal(0)) + c.weight
    return out


def exposures(holdings: list[Holding]) -> list[Exposure]:
    """Every company once, largest first; ``UNASSIGNED`` collects what the funds leave open."""
    total = sum((h.value for h in holdings), Decimal(0))
    rows: dict[str, Exposure] = {}

    def add(key: str, c: Constituent | None, holding: Holding, weight: Decimal) -> None:
        row = rows.get(key) or rows.setdefault(key, Exposure(
            key, c.name if c else "", c.country if c else "", c.sector if c else "", c.currency if c else ""))
        value = holding.value * weight
        row.value += value
        row.via[holding.id] = row.via.get(holding.id, Decimal(0)) + value / total

    for h in holdings:
        first = {c.key: c for c in reversed(h.constituents)}  # name, country ... of a key's first line
        for key, weight in weights(h).items():
            add(key, first[key], h, weight)
        rest = 1 - sum((c.weight for c in h.constituents), Decimal(0))
        if rest > 0:
            add(UNASSIGNED, None, h, rest)
    for row in rows.values():
        row.share = row.value / total if total else Decimal(0)
    return sorted(rows.values(), key=lambda r: (r.key == UNASSIGNED, -r.value))


def overlap(a: Holding, b: Holding) -> Decimal:
    wa, wb = weights(a), weights(b)
    return sum((min(w, wb[k]) for k, w in wa.items() if k in wb), Decimal(0))


def own_share(holding: Holding, others: list[Holding]) -> Decimal:
    elsewhere = {k for o in others if o.id != holding.id for k in weights(o)}
    return sum((w for k, w in weights(holding).items() if k not in elsewhere), Decimal(0))


def effective_count(rows: list[Exposure]) -> Decimal:
    squares = sum((r.share * r.share for r in rows if r.key != UNASSIGNED), Decimal(0))
    return 1 / squares if squares else Decimal(0)
