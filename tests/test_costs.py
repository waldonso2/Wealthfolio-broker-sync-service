"""Costs from buy to sale (waldonso2/wealthfolio-importer-addon#142): hand examples."""

from datetime import date
from decimal import Decimal as D

from brokersync import costs

TODAY = date(2026, 1, 1)  # 1461 days after 2022-01-01: exactly 4 years
BROKER = {"dep-a": "a", "dep-b": "b"}


def fund(**kw):
    trades = (costs.Trade(date(2022, 1, 1), "BUY", D(1000), fee=D(1), account="dep-a"),  # one-off buy
              costs.Trade(date(2023, 1, 1), "BUY", D(500), account="dep-a"),  # savings plan, no fee
              costs.Trade(date(2025, 1, 1), "SELL", D(300), fee=D(1), tax=D(5), account="dep-a"))  # partial sale
    return costs.Position("f", "Fund", D(1800), D(1200), {"dep-a": D(1800)}, trades, **kw)


def test_a_fund_from_buy_to_sale_to_the_cent():
    models = {"a": costs.Model(fixed=D(1), spread=D("0.001"))}
    c = costs.position(fund(ter=D("0.002")), TODAY, BROKER, models)
    assert c.years == 4 and c.invested == 1501 and c.fees == 2 and c.tax == 5
    assert c.spread == D("1.8")  # 0.1 % of 1000 + 500 + 300
    assert c.running == D("12")  # 0.2 % × (1200 + 1800) / 2 × 4 years
    assert c.exit == D("2.8")  # 1 EUR + 0.1 % of 1800
    assert round(c.rate, 6) == round(D("15.8") / 1500 / 4, 6)  # taxes are not in it
    assert c.yearly == D("3.6")


def test_without_a_ter_or_a_fee_model_nothing_is_counted_as_zero():
    c = costs.position(fund(), TODAY, BROKER, {})
    assert c.running is None and c.rate is None and c.exit is None and c.yearly is None
    assert c.spread == 0 and c.fees == 2  # what was booked stays


def test_the_depot_names_the_share_without_a_rate_and_shares_the_custody_fee():
    stock = costs.Position("s", "Stock", D(600), D(600), {"dep-b": D(600)})
    other = costs.Position("o", "Other", D(200), D(200), {"dep-b": D(200)}, ter=D(0))
    models = {"a": costs.Model(fixed=D(1)), "b": costs.Model(custody=D(12))}
    d = costs.depot([fund(ter=D("0.002")), stock, other], TODAY, BROKER, models)
    by = {c.id: c for c in d.positions}
    assert by["s"].yearly is None and by["o"].yearly == 3  # 12 EUR shared 600 : 200
    assert d.unknown == D(600) / 2600 and d.not_estimated == 2  # broker b has no fee for a sale
    assert d.ter == (D("0.002") * 1800) / 2000 and d.exit == D(1) and d.tax == 5
    assert d.yearly == D("3.6") + 3
    assert d.rate == (by["f"].rate * 1800 + by["o"].rate * 200) / 2000
