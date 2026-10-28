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
with the same key (share classes) are added up. ``unify`` first gives a line without an ISIN (iShares files
have none) the ISIN of a line elsewhere with the same plain name. ``findings`` are the cluster risks over
``LIMITS``: a single stock, a country, a sector, foreign currency, two overlapping funds, too few effective
stocks, cash above the deposit guarantee on one account, a cost rate above its limit (#142).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from itertools import combinations

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


ISIN = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}\d$")
# Legal forms and share classes: "APPLE INC" and "Apple Inc." are one company, "ALPHABET INC CLASS A" too.
_FORMS = re.compile(r"\b(INC|INCORPORATED|CORP|CORPORATION|CO|COMPANY|LTD|LIMITED|PLC|AG|SA|SE|NV|AB|ASA|OYJ|"
                    r"SPA|HOLDINGS?|GROUP|CLASS [A-Z]|CL [A-Z]|REG|REGISTERED|ADR|ORD|SHS)\b")


# A par value after the name, as Invesco writes it: "APPLE INC USD0.00001", "SAP NPV".
_PAR = re.compile(r"\s(?:[A-Z]{3}\d+(?:[.,]\d+)?|NPV)\b")


def plain(name: str) -> str:
    return " ".join(_FORMS.sub(" ", re.sub(r"[^A-Z0-9 ]", " ", _PAR.sub(" ", name.upper()))).split())


def unify(holdings: list[Holding]) -> list[Holding]:
    """Lines without an ISIN take the ISIN of a line with the same plain name; the rest meet by that name.
    shortcut: names only - a company named differently by two issuers stays two rows; OpenFIGI if it matters."""
    alias: dict[str, str] = {}
    for h in holdings:
        for c in h.constituents:
            if ISIN.match(c.key):
                alias.setdefault(plain(c.name), c.key)

    def key(c: Constituent) -> str:
        return c.key if ISIN.match(c.key) else alias.get(plain(c.name)) or f"~{plain(c.name) or c.key}"

    return [Holding(h.id, h.name, h.value, tuple(Constituent(key(c), c.name, c.weight, c.country, c.sector,
                                                             c.currency) for c in h.constituents))
            for h in holdings]


# Defaults of the page *Durchschau*; each can be changed there. Shares are of the depot, cash in EUR.
LIMITS = {"stock": Decimal("0.05"), "country": Decimal("0.60"), "sector": Decimal("0.30"),
          "currency": Decimal("0.50"), "overlap": Decimal("0.50"), "effective": Decimal(30),
          "cash": Decimal(100000), "cost": Decimal("0.005")}


@dataclass(frozen=True)
class Finding:
    key: str  # what the user accepts, e.g. "country:United States"
    kind: str  # a key of LIMITS
    name: str
    value: Decimal  # share of the depot, overlap, number of stocks or cash
    limit: Decimal


def _by(rows: list[Exposure], attr: str) -> dict[str, Decimal]:
    out: dict[str, Decimal] = {}
    for r in rows:
        # shortcut: names as the issuers write them ("United States" vs. "USA" stay apart); a table if it matters.
        if r.key != UNASSIGNED and (name := getattr(r, attr).strip()):
            out[name] = out.get(name, Decimal(0)) + r.share
    return out


def findings(holdings: list[Holding], limits: dict[str, Decimal], base: str,
             cash: dict[str, Decimal] | None = None, cost: Decimal | None = None) -> list[Finding]:
    """Cluster risks over the limits, largest first within each kind. ``cash``: cash per account name;
    ``cost``: the depot's cost rate a year (``costs.DepotCost.rate``)."""
    rows = exposures(holdings)
    out = [Finding(f"stock:{r.key}", "stock", r.name, r.share, limits["stock"])
           for r in rows if r.key != UNASSIGNED and r.share > limits["stock"]]
    for kind in ("country", "sector"):
        groups = sorted(_by(rows, kind).items(), key=lambda g: -g[1])
        out += [Finding(f"{kind}:{name}", kind, name, share, limits[kind]) for name, share in groups
                if share > limits[kind]]
    # shortcut: a constituent's own currency; a currency-hedged tranche still counts as foreign.
    foreign = sum((r.share for r in rows if r.currency and r.currency != base), Decimal(0))
    if foreign > limits["currency"]:
        out.append(Finding("currency", "currency", base, foreign, limits["currency"]))
    funds = [h for h in holdings if len(h.constituents) > 1]
    out += [Finding(f"overlap:{a.id}:{b.id}", "overlap", f"{a.name} / {b.name}", o, limits["overlap"])
            for a, b in combinations(funds, 2) if (o := overlap(a, b)) > limits["overlap"]]
    count = effective_count(rows)
    if count and count < limits["effective"]:
        out.append(Finding("effective", "effective", "", count, limits["effective"]))
    out += [Finding(f"cash:{name}", "cash", name, amount, limits["cash"])
            for name, amount in sorted((cash or {}).items()) if amount > limits["cash"]]
    if cost is not None and cost > limits["cost"]:
        out.append(Finding("cost", "cost", "", cost, limits["cost"]))
    return out
