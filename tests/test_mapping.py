"""Mapping rules - the expectations mirror the Broker Importer addon's tests
(src/pdf/pdf.test.ts, src/scalable.test.ts) so both book identically."""

from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal as D

import pytest

from brokersync.mapping import Accounts, MappingError, SecurityMapping, fmt, iso, to_activities, trade_final_cash
from brokersync.model import Kind, Transaction

ACC = Accounts("cash", "depot")
DT = datetime(2026, 7, 7, 9, 4, 12, tzinfo=UTC)


def tx(**kw) -> Transaction:
    base = dict(id="e5f6-0789", kind=Kind.BUY, datetime=DT, currency="EUR", net=D("3016"), isin="IE00TEST0001",
                name="Test World Equity EUR (Acc)", shares=D("120"), gross=D("3015"), fee=D("1"), label="Kauf")
    base.update(kw)
    return Transaction(**base)


def cash_by_account(acts: list[dict]) -> dict[str, D]:
    """Cash effect per account, like the addon's reconcile cashEffect()."""
    out: dict[str, D] = defaultdict(D)
    sign = {"DEPOSIT": 1, "TRANSFER_IN": 1, "SELL": 1, "DIVIDEND": 1, "INTEREST": 1, "CREDIT": 1,
            "WITHDRAWAL": -1, "TRANSFER_OUT": -1, "BUY": -1, "FEE": -1, "TAX": -1}
    for a in acts:
        out[a["accountId"]] += sign[a["activityType"]] * D(a["amount"])
    return {k: v for k, v in out.items() if v != 0}


def test_trade_final_cash_matches_addon():
    assert trade_final_cash(Kind.BUY, "12.933359", "38.659999", "0") == "500.003646006641"
    assert trade_final_cash(Kind.BUY, "2.0000000000", "100.000000", "1") == "201"
    assert trade_final_cash(Kind.SELL, "117", "51.82", "266.2") == "5796.74"
    assert trade_final_cash(Kind.BUY, "0.1", "0.2", "0") == "0.02"
    assert trade_final_cash(Kind.SELL, "-5", "-10", "-1") == "49"
    assert trade_final_cash(Kind.SELL, "2", "160", "1", "25.5") == "293.5"
    assert trade_final_cash(Kind.BUY, "2", "100", "1", "-0.5") == "201.5"


def test_fmt_and_iso():
    assert fmt(D("3016.000")) == "3016"
    assert fmt(D("1E+2")) == "100"
    assert fmt(D("0.000")) == "0"
    assert iso(DT) == "2026-07-07T09:04:12.000Z"
    assert iso(DT, -2) == "2026-07-07T09:04:10.000Z"


def test_buy_funds_the_depot_with_a_grouped_transfer_pair():
    acts = to_activities(tx(), "tr", ACC)
    assert [(a["accountId"], a["activityType"], a["amount"]) for a in acts] == [
        ("cash", "TRANSFER_OUT", "3016"),
        ("depot", "TRANSFER_IN", "3016"),
        ("depot", "BUY", "3016"),
    ]
    assert {a.get("sourceGroupId") for a in acts[:2]} == {"sync-tr-e5f6-0789"}
    assert "sourceGroupId" not in acts[2]
    buy = acts[2]
    assert (buy["quantity"], buy["unitPrice"], buy["fee"]) == ("120", "25.125", "1")
    assert "tax" not in buy
    assert buy["comment"] == "Kauf Test World Equity EUR (Acc) [SYNC tr:e5f6-0789]"
    assert buy["asset"] == {"symbol": "IE00TEST0001", "name": "Test World Equity EUR (Acc)", "quoteCcy": "EUR"}
    assert all(a["asset"]["symbol"] == "$CASH-EUR" for a in acts[:2])
    assert cash_by_account(acts) == {"cash": D("-3016")}


def test_sell_and_dividend_sweep_everything_to_cash():
    sell = tx(id="s1", kind=Kind.SELL, net=D("988.45"), shares=D("10"), gross=D("1000"), fee=D("1"), tax=D("10.55"),
              label="Verkauf")
    div = tx(id="d1", kind=Kind.DIVIDEND, net=D("81.54"), shares=D("200"), gross=D("100"), fee=D(0), tax=D("18.46"),
             label="Dividende", name="Test High Dividend USD (Dist)")
    acts = to_activities(sell, "tr", ACC) + to_activities(div, "tr", ACC)
    s = next(a for a in acts if a["activityType"] == "SELL")
    assert (s["amount"], s["fee"], s["tax"]) == ("988.45", "1", "10.55")
    d = next(a for a in acts if a["activityType"] == "DIVIDEND")
    assert (d["amount"], d["tax"], d["quantity"]) == ("81.54", "18.46", "200")
    assert cash_by_account(acts) == {"cash": D("1069.99")}


def test_tax_refund_is_its_own_credit():
    div = tx(id="d2", kind=Kind.DIVIDEND, net=D("105"), shares=D("200"), gross=D("100"), fee=D(0), tax=D("-5"),
             label="Dividende")
    acts = to_activities(div, "tr", ACC)
    d = next(a for a in acts if a["activityType"] == "DIVIDEND")
    assert d["amount"] == "100" and "tax" not in d
    credit = next(a for a in acts if a["activityType"] == "CREDIT")
    assert (credit["amount"], credit["subtype"], credit["accountId"]) == ("5", "TAX_REFUND", "depot")
    assert cash_by_account(acts) == {"cash": D("105")}


def test_cash_only_kinds_book_on_the_cash_account():
    for kind, wf_type in [(Kind.DEPOSIT, "DEPOSIT"), (Kind.WITHDRAWAL, "WITHDRAWAL"), (Kind.FEE, "FEE"),
                          (Kind.TAX, "TAX"), (Kind.INTEREST, "INTEREST")]:
        acts = to_activities(Transaction("x", kind, DT, "EUR", D("12.5"), label="L"), "dkb", ACC)
        assert [(a["accountId"], a["activityType"], a["amount"]) for a in acts] == [("cash", wf_type, "12.5")]
    refund = to_activities(Transaction("r", Kind.TAX_REFUND, DT, "EUR", D("3")), "dkb", ACC)
    assert (refund[0]["activityType"], refund[0]["subtype"]) == ("CREDIT", "TAX_REFUND")
    interest = to_activities(Transaction("i", Kind.INTEREST, DT, "EUR", D("7.36"), gross=D("10"), tax=D("2.64")),
                             "tr", ACC)
    assert (interest[0]["amount"], interest[0]["tax"]) == ("7.36", "2.64")


def test_security_mapping_replaces_the_isin():
    acts = to_activities(tx(), "tr", ACC, {"IE00TEST0001": SecurityMapping("TWLD", "XETR")})
    assert acts[2]["asset"] == {"symbol": "TWLD", "exchangeMic": "XETR", "name": "Test World Equity EUR (Acc)",
                                "quoteCcy": "EUR"}


@pytest.mark.parametrize("bad", [
    dict(net=D("3100")),                      # amounts don't add up
    dict(gross=None),                         # no market value
    dict(shares=D(0)),                        # no shares
    dict(kind=Kind.UNKNOWN, raw_type="X"),    # unknown type is never booked
])
def test_inconsistent_transactions_are_rejected(bad):
    with pytest.raises(MappingError):
        to_activities(tx(**bad), "tr", ACC)
