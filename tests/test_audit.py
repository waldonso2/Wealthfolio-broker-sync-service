"""The account check: finds where the two-account model doesn't add up, changes nothing."""

import re
from datetime import UTC, datetime
from decimal import Decimal as D

from brokersync import audit
from brokersync.mapping import Accounts, TransferPattern, to_activities
from brokersync.model import Kind, Transaction
from brokersync.wealthfolio import WealthfolioClient

from .fakes import PASSWORD, FakeWealthfolio

ACC = Accounts("acc-cash", "acc-depot")
WHEN = datetime(2026, 3, 2, 10, tzinfo=UTC)


def book(wf, *txs, patterns=()):
    client = WealthfolioClient("http://wf", PASSWORD, transport=wf.transport())
    for tx in txs:
        for p in to_activities(tx, "scalable", ACC, patterns=list(patterns)):
            client.create_activity(p)
    return client


def trade(id, kind, net, day, **kw):
    return Transaction(id, kind, WHEN.replace(day=day), "EUR", D(net), isin="IE00TEST0001", name="Testfonds", **kw)


def everything(wf):
    return book(
        wf,
        Transaction("d", Kind.DEPOSIT, WHEN.replace(day=1), "EUR", D("1000")),
        trade("b", Kind.BUY, "501", 2, shares=D(5), gross=D(500), fee=D(1)),
        trade("s", Kind.SELL, "188.45", 3, shares=D(2), gross=D(200), fee=D(1), tax=D("10.55")),
        trade("v", Kind.DIVIDEND, "105", 4, shares=D(3), gross=D(100), tax=D(-5)),
        trade("p", Kind.BUY, "50", 5, shares=D("0.5"), gross=D(50), bonus_funded=True),
        Transaction("w", Kind.WITHDRAWAL, WHEN.replace(day=6), "EUR", D("100"), text="Sparkonto"),
        patterns=[TransferPattern("Sparkonto", keyword="Sparkonto", destination_account_id="acc-old")],
    )


def test_a_clean_booking_has_no_findings():
    wf = FakeWealthfolio()
    client = everything(wf)
    r = audit.run(client, ACC)
    assert (r.moments, r.lonely) == ([], [])
    assert r.depot_cash == 0 and r.cash == D("692.45")


def test_a_deleted_sale_leaves_its_sweep_behind():
    wf = FakeWealthfolio()
    everything(wf)
    sale = next(a for a in wf.activities if a["activityType"] == "SELL")
    wf.activities.remove(sale)
    r = audit.check(wf.activities, ACC)
    [m] = r.moments
    assert m.net == D("-188.45") and r.depot_cash == D("-188.45")
    assert [a["activityType"] for a in m.activities] == ["TRANSFER_OUT"]
    assert r.lonely == []  # the transfer pair itself is complete


def test_a_buy_without_funding_and_a_half_transfer_pair():
    wf = FakeWealthfolio()
    everything(wf)
    # A buy imported without its funding transfer ...
    wf.add_existing(accountId="acc-depot", activityType="BUY", date="2026-03-20T09:00:00.000Z", quantity="1",
                    unitPrice="77", amount="77", currency="EUR", comment="Kauf ohne Übertrag", assetSymbol="X",
                    assetId="X")
    # ... and the depot leg of a funding pair deleted by hand.
    leg = next(a for a in wf.activities if a["activityType"] == "TRANSFER_IN" and a["accountId"] == "acc-depot"
               and a["amount"] == "501")
    wf.activities.remove(leg)
    r = audit.check(wf.activities, ACC)
    assert sorted(m.net for m in r.moments) == [D("-501"), D("-77")]
    assert r.moments_total == r.depot_cash == D("-578")
    [lonely] = r.lonely
    assert lonely["accountId"] == "acc-cash" and lonely["activityType"] == "TRANSFER_OUT" and lonely["amount"] == "501"
    assert audit.source(lonely) == "Dienst"


def test_securities_transfers_move_no_cash():
    a = {"id": "t", "accountId": "acc-depot", "activityType": "TRANSFER_IN", "date": "2026-01-01T00:00:00Z",
         "amount": "1000", "quantity": "10", "assetId": "IE00TEST0001", "assetSymbol": "IE00TEST0001"}
    r = audit.check([a], ACC)
    assert (r.moments, r.lonely, r.depot_cash) == ([], [], 0)


def test_the_page(tmp_path):
    from fastapi.testclient import TestClient

    from brokersync import config as config_mod
    from brokersync.vault import Vault
    from brokersync.web.app import create_app

    from .fake_broker import FakeBroker

    wf = FakeWealthfolio()
    everything(wf)
    wf.activities.remove(next(a for a in wf.activities if a["activityType"] == "SELL"))
    cfg = config_mod.Config(wealthfolio_url="http://wf.local:8080")
    cfg.brokers["fake"] = config_mod.BrokerConfig(cash_account_id="acc-cash", portfolio_account_id="acc-depot")
    config_mod.save(tmp_path, cfg)
    Vault(tmp_path).update(lambda d: d.__setitem__("wealthfolio_password", PASSWORD))
    app = create_app(tmp_path, wealthfolio=lambda u, p: WealthfolioClient(u, p, transport=wf.transport()),
                     adapters={"fake": FakeBroker}, run_in_thread=False)
    client = TestClient(app)
    client.post("/setup-password", data={"password": "geheim123", "password2": "geheim123"})
    page = client.get("/check").text
    assert "Test Depot <strong>-188,45 EUR</strong>" in page
    assert "bleibt stehen: -188,45 EUR" in page and "Überträge ohne Gegenstück: 0" in page
    assert re.search(r"2026-03-03 10:00:01</td><td>TRANSFER_OUT", page)
    count = len(wf.activities)
    assert len(wf.activities) == count and not wf.deleted and not wf.updated  # read only
