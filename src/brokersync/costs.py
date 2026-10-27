"""What a position costs from buy to sale (waldonso2/wealthfolio-importer-addon#142) - pure, no I/O.

Per position, over the time it is held (from its first buy):

- **Order fees**: the ``fee`` of its BUY/SELL activities, as booked.
- **Spread**: the broker's spread (``Model.spread``) on the value of every trade - an estimate, there is no
  source for real spreads; 0 where the user entered none.
- **Running costs**: TER × average value × years held; the average value is the mean of cost basis and
  today's value (``shortcut:`` Wealthfolio's valuation history per asset would be exact). Unknown TER:
  no running costs and no cost rate, never 0 % (a single stock gets a TER of 0 typed by hand).
- **Custody fee**: the broker's fee a year, shared by its positions by value.
- **Exit**: order fee and spread of selling today's value - only what the user entered as the broker's
  fee model; without one ``None`` ("not estimated"), never 0.
- **Taxes** apart: the ``tax`` of its activities, no cost in the narrow sense, so not in the cost rate.

The **cost rate** (a year) = costs so far / average value / years held; the depot's is the value-weighted
mean over the positions whose rate is known, with the share of the depot where it is not.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

ZERO = Decimal(0)
MIN_YEARS = Decimal("0.0833")  # a month: a rate over a few days held says nothing


@dataclass(frozen=True)
class Model:
    """A broker's fees; ``None``: not entered."""
    fixed: Decimal | None = None  # EUR per sale
    percent: Decimal | None = None  # share of a sale's value
    spread: Decimal | None = None  # share of a trade's value
    custody: Decimal | None = None  # EUR a year

    def exit(self, value: Decimal) -> Decimal | None:
        if self.fixed is None and self.percent is None and self.spread is None:
            return None
        return (self.fixed or ZERO) + ((self.percent or ZERO) + (self.spread or ZERO)) * value


@dataclass(frozen=True)
class Trade:
    day: date
    kind: str  # BUY or SELL
    value: Decimal  # shares × price
    fee: Decimal = ZERO
    tax: Decimal = ZERO
    account: str = ""


@dataclass(frozen=True)
class Position:
    id: str
    name: str
    value: Decimal  # today, base currency
    cost: Decimal  # cost basis
    parts: dict[str, Decimal]  # account id -> value
    trades: tuple[Trade, ...] = ()
    ter: Decimal | None = None  # a year, share


@dataclass
class PositionCost:
    id: str
    name: str
    years: Decimal
    invested: Decimal  # value of the buys plus their fees
    fees: Decimal
    spread: Decimal
    running: Decimal | None
    custody: Decimal
    exit: Decimal | None
    tax: Decimal
    ter: Decimal | None
    yearly: Decimal | None  # running + custody a year from now, EUR
    rate: Decimal | None  # a year

    @property
    def so_far(self) -> Decimal:
        return self.fees + self.spread + (self.running or ZERO) + self.custody


@dataclass
class DepotCost:
    positions: list[PositionCost] = field(default_factory=list)
    ter: Decimal | None = None  # value-weighted over the positions with a TER
    rate: Decimal | None = None
    unknown: Decimal = ZERO  # share of the securities without a cost rate
    yearly: Decimal = ZERO  # EUR a year from now: TER × value + custody, of the known
    so_far: Decimal = ZERO
    exit: Decimal = ZERO
    not_estimated: int = 0  # positions without a fee model for the sale
    tax: Decimal = ZERO


def _custody(positions: list[Position], broker: dict[str, str], models: dict[str, Model]) -> dict[str, Decimal]:
    """Custody a year per position: each broker's fee shared by value over the positions on its accounts."""
    totals: dict[str, Decimal] = {}
    for p in positions:
        for account, value in p.parts.items():
            if broker.get(account):
                totals[broker[account]] = totals.get(broker[account], ZERO) + value
    out = {}
    for p in positions:
        out[p.id] = sum((((models.get(broker.get(a, "")) or Model()).custody or ZERO) * v / totals[broker[a]]
                         for a, v in p.parts.items() if broker.get(a) and totals[broker[a]]), ZERO)
    return out


def position(p: Position, today: date, broker: dict[str, str], models: dict[str, Model],
             custody: Decimal = ZERO) -> PositionCost:
    def model(account: str) -> Model:
        return models.get(broker.get(account, "")) or Model()

    buys = [t for t in p.trades if t.kind == "BUY"]
    first = min((t.day for t in buys), default=today)
    years = max(Decimal((today - first).days) / Decimal("365.25"), ZERO)
    fees = sum((t.fee for t in p.trades), ZERO)
    spread = sum((t.value * (model(t.account).spread or ZERO) for t in p.trades), ZERO)
    average = (p.cost + p.value) / 2
    running = p.ter * average * years if p.ter is not None else None
    exits = [model(a).exit(v) for a, v in p.parts.items()]
    exit = sum((e for e in exits if e is not None), ZERO) if exits and None not in exits else None
    rate = None
    if running is not None and average > 0:
        held = max(years, MIN_YEARS)
        rate = (fees + spread + running + custody * years) / average / held
    return PositionCost(p.id, p.name, years, sum((t.value + t.fee for t in buys), ZERO), fees, spread, running,
                        custody * years, exit, sum((t.tax for t in p.trades), ZERO), p.ter,
                        p.ter * p.value + custody if p.ter is not None else None, rate)


def depot(positions: list[Position], today: date, broker: dict[str, str], models: dict[str, Model]) -> DepotCost:
    """``broker``: account id -> broker key; ``models``: broker key -> its fees."""
    custody = _custody(positions, broker, models)
    out = DepotCost([position(p, today, broker, models, custody[p.id]) for p in positions])
    value = {p.id: p.value for p in positions}
    total = sum(value.values(), ZERO)
    with_ter = [c for c in out.positions if c.ter is not None]
    known = [c for c in out.positions if c.rate is not None]
    if with_ter and (v := sum((value[c.id] for c in with_ter), ZERO)):
        out.ter = sum((c.ter * value[c.id] for c in with_ter if c.ter is not None), ZERO) / v
    if known and (v := sum((value[c.id] for c in known), ZERO)):
        out.rate = sum((c.rate * value[c.id] for c in known if c.rate is not None), ZERO) / v
    out.unknown = 1 - sum((value[c.id] for c in known), ZERO) / total if total else ZERO
    out.yearly = sum((c.yearly for c in out.positions if c.yearly is not None), ZERO)
    out.so_far = sum((c.so_far for c in out.positions), ZERO)
    out.exit = sum((c.exit for c in out.positions if c.exit is not None), ZERO)
    out.not_estimated = sum(1 for c in out.positions if c.exit is None)
    out.tax = sum((c.tax for c in out.positions), ZERO)
    return out
