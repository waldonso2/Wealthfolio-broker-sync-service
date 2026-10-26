"""FIRE with German net figures (waldonso2/wealthfolio-importer-addon#139) - pure, no I/O.

How much capital a life on the depot needs, in today's money (real returns):

- **Gross withdrawal** that leaves the expenses after tax and health insurance: only the gain share of a
  sale is taxed, less the partial exemption of equity funds (``EXEMPTION``) and the saver's allowance;
  the rate is the flat tax with solidarity surcharge and church tax (with its deduction, ``tax_rate``).
- **Health insurance** of a voluntary member of the statutory one (GKV): its rate on the taxable capital
  income, at least on ``GKV_MIN`` and at most on ``GKV_MAX`` a year; or a private premium the user enters.
- **Pension** from its age: until then the depot pays the whole withdrawal (the bridge), after it only what
  the (net) pension leaves open. A part-time income until the pension is *Barista FIRE*.
- **Capital** = present value of the bridge years + the rest as a perpetual withdrawal at the safe rate.
- **Years to FI** with today's capital and yearly savings; **Coast FIRE**: the capital that grows into the
  target by the retirement age without saving more.

Constants as of 2026 (``RULES_YEAR``); a simplified model calculation - no tax or investment advice. The
pre-assessment (Vorabpauschale) is left out: it is credited against the later sale.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

RULES_YEAR = 2026
FLAT_TAX = 0.25
SOLI = 0.055
EXEMPTION = 0.30  # Teilfreistellung of equity funds (§ 20 InvStG)
ALLOWANCE = {False: 1000.0, True: 2000.0}  # Sparerpauschbetrag, single / joint
GKV_RATE = 0.14 + 0.029  # reduced general rate + average additional rate 2026
CARE_RATE = {False: 0.036, True: 0.042}  # Pflegeversicherung, with children / childless
GKV_MIN = 1318.33 * 12  # Mindestbemessungsgrundlage 2026, a year
GKV_MAX = 5812.50 * 12  # Beitragsbemessungsgrenze 2026, a year


def tax_rate(church: float = 0.0) -> float:
    """Flat tax + soli + church tax on capital income; church tax is deductible (§ 32d Abs. 1 EStG)."""
    income_tax = FLAT_TAX / (1 + FLAT_TAX * church)
    return income_tax * (1 + SOLI + church)


@dataclass
class Inputs:
    expenses: float  # a year, today's money, without health insurance
    capital: float = 0.0  # today
    savings: float = 0.0  # a year
    age: int = 40
    retire_age: int = 55
    pension_age: int = 67
    pension: float = 0.0  # net a year, today's money
    part_time: float = 0.0  # net a year until the pension (Barista FIRE)
    real_return: float = 0.05
    safe_rate: float = 0.035
    gain_share: float = 0.5  # part of a sale that is gain
    equity_fund: bool = True
    joint: bool = False
    church: float = 0.0  # 0, 0.08 or 0.09
    gkv: bool = True  # voluntary statutory insurance; False: ``private`` a year
    childless: bool = False
    private: float = 0.0


@dataclass
class Withdrawal:
    gross: float  # taken from the depot a year
    tax: float
    health: float
    need: float  # what the depot has to cover, net


def withdrawal(i: Inputs, need: float) -> Withdrawal:
    """The gross withdrawal whose net covers ``need`` plus the health insurance it causes itself."""
    rate = tax_rate(i.church)
    taxable_share = i.gain_share * (1 - EXEMPTION if i.equity_fund else 1)
    allowance = ALLOWANCE[i.joint]
    gross = need
    for _ in range(50):  # the insurance depends on the withdrawal: a fixed point, reached in a few steps
        income = taxable_share * gross
        tax = max(0.0, income - allowance) * rate
        health = (GKV_RATE + CARE_RATE[i.childless]) * min(max(income, GKV_MIN), GKV_MAX) if i.gkv else i.private
        new = need + tax + health
        if abs(new - gross) < 0.005:
            break
        gross = new
    return Withdrawal(gross, tax, health, need)


def _annuity(amount: float, years: int, r: float) -> float:
    """Present value of ``amount`` a year for ``years`` years, paid at the start of each."""
    if years <= 0:
        return 0.0
    if r == 0:
        return amount * years
    return amount * (1 - (1 + r) ** -years) / r * (1 + r)


def capital_needed(i: Inputs, expenses: float, earned: float = 0.0) -> float:
    """Capital at the retirement age: the bridge to the pension, then the rest at the safe rate for good."""
    bridge = max(0, i.pension_age - i.retire_age)
    before = withdrawal(i, max(0.0, expenses - earned)).gross
    after = withdrawal(i, max(0.0, expenses - i.pension)).gross  # the part-time income ends with the pension
    return _annuity(before, bridge, i.real_return) + after / i.safe_rate / (1 + i.real_return) ** bridge


def years_to(target: float, capital: float, savings: float, r: float) -> float | None:
    """Years until ``capital`` plus ``savings`` a year reach ``target``; None if never."""
    if capital >= target:
        return 0.0
    if r == 0:
        return (target - capital) / savings if savings > 0 else None
    num, den = target * r + savings, capital * r + savings
    if den <= 0 or num <= 0:
        return None
    return math.log(num / den) / math.log(1 + r)


@dataclass
class Result:
    rate: float
    now: Withdrawal  # a year at the regular budget, before the pension
    targets: dict[str, float] = field(default_factory=dict)  # lean / regular / fat / barista -> capital
    years: dict[str, float | None] = field(default_factory=dict)
    coast: float = 0.0  # capital today that grows into the regular target by the retirement age
    coast_reached: bool = False
    fi_age: float | None = None


def plan(i: Inputs) -> Result:
    budgets = {"lean": 0.75 * i.expenses, "regular": i.expenses, "fat": 1.5 * i.expenses}
    targets = {k: capital_needed(i, v) for k, v in budgets.items()}
    if i.part_time:
        targets["barista"] = capital_needed(i, i.expenses, earned=i.part_time)
    years = {k: years_to(t, i.capital, i.savings, i.real_return) for k, t in targets.items()}
    coast = targets["regular"] / (1 + i.real_return) ** max(0, i.retire_age - i.age)
    fi = years["regular"]
    return Result(tax_rate(i.church), withdrawal(i, i.expenses), targets, years, coast, i.capital >= coast,
                  i.age + fi if fi is not None else None)
