"""FIRE with German net figures: hand-checked numbers (fire.py)."""

import pytest

from brokersync import fire
from brokersync.fire import Inputs


def test_tax_rate_with_and_without_church_tax():
    assert fire.tax_rate() == pytest.approx(0.26375)
    assert fire.tax_rate(0.09) == pytest.approx(0.279951, abs=1e-6)
    assert fire.tax_rate(0.08) == pytest.approx(0.278186, abs=1e-6)


def test_a_withdrawal_below_the_allowance_pays_no_tax_but_the_minimum_health_insurance():
    w = fire.withdrawal(Inputs(expenses=3000, gain_share=0.2), 3000)
    # Taxable: 20 % gain, 70 % of it after the exemption = 14 % of the withdrawal (about 870) < 1000.
    assert w.tax == 0
    assert w.health == pytest.approx((0.169 + 0.036) * 1318.33 * 12)
    assert w.gross == pytest.approx(3000 + w.health)


def test_the_gross_withdrawal_covers_need_tax_and_insurance():
    i = Inputs(expenses=30000, gain_share=1.0, gkv=False, private=0)
    w = fire.withdrawal(i, 30000)
    # gross - 0.26375 * (0.7 gross - 1000) = 30000
    assert w.gross == pytest.approx((30000 - 0.26375 * 1000) / (1 - 0.26375 * 0.7), abs=0.01)
    assert w.gross - w.tax == pytest.approx(30000, abs=0.01)
    i.gkv = True
    w = fire.withdrawal(i, 30000)
    assert w.gross - w.tax - w.health == pytest.approx(30000, abs=0.01)
    assert w.health == pytest.approx(0.205 * max(0.7 * w.gross, 1318.33 * 12), abs=0.01)


def test_capital_is_the_safe_rate_perpetuity_without_a_pension_and_less_with_one():
    i = Inputs(expenses=20000, gain_share=0.0, gkv=False, retire_age=67, pension_age=67)
    assert fire.capital_needed(i, 20000) == pytest.approx(20000 / 0.035)
    i.retire_age, i.pension = 57, 12000
    bridge = sum(20000 / 1.05 ** t for t in range(10))
    rest = 8000 / 0.035 / 1.05 ** 10
    assert fire.capital_needed(i, 20000) == pytest.approx(bridge + rest)


def test_years_to_fi_and_coast_fire():
    assert fire.years_to(100, 100, 0, 0.05) == 0
    assert fire.years_to(200, 100, 0, 0.05) == pytest.approx(14.2067, abs=1e-4)  # ln 2 / ln 1.05
    assert fire.years_to(1000, 0, 100, 0) == 10
    assert fire.years_to(1000, 0, 0, 0.05) is None
    r = fire.plan(Inputs(expenses=20000, capital=300000, savings=20000, age=40, retire_age=60, gain_share=0.3,
                         part_time=10000))
    assert set(r.targets) == {"lean", "regular", "fat", "barista"}
    assert r.targets["lean"] < r.targets["barista"] < r.targets["regular"] < r.targets["fat"]
    assert r.coast == pytest.approx(r.targets["regular"] / 1.05 ** 20)
    assert r.coast_reached == (r.coast <= 300000)
    assert r.fi_age == pytest.approx(40 + r.years["regular"])
