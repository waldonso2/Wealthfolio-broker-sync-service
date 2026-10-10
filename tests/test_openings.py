"""Trades whose money comes from outside (comdirect), opening positions and starting a broker over."""

import json
from datetime import UTC, datetime
from decimal import Decimal as D

from fastapi.testclient import TestClient

from brokersync.mapping import Accounts, to_activities
from brokersync.model import Kind, Transaction
from brokersync.state import State

from .fakes import FakeWealthfolio
from .test_sync import setup
from .test_web import csrf

ACC = Accounts("acc-cash", "acc-depot")
WHEN = datetime(2026, 3, 2, 10, tzinfo=UTC)


def cash_effect(payloads, account):
    sign = {"DEPOSIT": 1, "TRANSFER_IN": 1, "WITHDRAWAL": -1, "TRANSFER_OUT": -1, "BUY": -1, "SELL": 1}
    return sum(sign.get(p["activityType"], 0) * D(p["amount"]) for p in payloads if p["accountId"] == account)


def test_external_cash_buys_and_sales_leave_the_cash_account_untouched():
    buy = Transaction("b", Kind.BUY, WHEN, "EUR", D("1009.80"), isin="IE00TEST0001", name="TEST Fonds",
                      shares=D(10), gross=D(1000), fee=D("9.80"), external_cash=True)
    payloads = to_activities(buy, "comdirect", ACC)
    assert [p["activityType"] for p in payloads] == ["DEPOSIT", "TRANSFER_OUT", "TRANSFER_IN", "BUY"]
    assert payloads[0]["amount"] == "1009.8" and payloads[0]["activityDate"] < payloads[1]["activityDate"]
    sell = Transaction("s", Kind.SELL, WHEN, "EUR", D("490.10"), isin="IE00TEST0001", name="TEST Fonds",
                       shares=D(5), gross=D(500), fee=D("9.90"), external_cash=True)
    payloads2 = to_activities(sell, "comdirect", ACC)
    assert [p["activityType"] for p in payloads2] == ["SELL", "TRANSFER_OUT", "TRANSFER_IN", "WITHDRAWAL"]
    assert cash_effect(payloads, ACC.cash) == cash_effect(payloads2, ACC.cash) == 0
    assert cash_effect(payloads, ACC.portfolio) == cash_effect(payloads2, ACC.portfolio) == 0
    # Without the flag nothing changes.
    plain = to_activities(Transaction("b", Kind.BUY, WHEN, "EUR", D("1009.80"), isin="IE00TEST0001", name="TEST",
                                      shares=D(10), gross=D(1000), fee=D("9.80")), "comdirect", ACC)
    assert [p["activityType"] for p in plain] == ["TRANSFER_OUT", "TRANSFER_IN", "BUY"]


def test_an_opening_position_is_booked_once_as_a_buy_with_its_deposit(tmp_path):
    wf = FakeWealthfolio()
    syncer, _ = setup(tmp_path, wf)
    syncer.run()
    syncer.state.add_opening("fake", "DE000TEST009", "2024-05-02", "TEST Alt AG", "200", "25.5", "4.9")
    [r] = syncer.run()
    assert r.created == 1
    booked = [a for a in wf.activities if "start-DE000TEST009-2024-05-02" in (a.get("comment") or "")]
    assert [a["activityType"] for a in booked] == ["DEPOSIT", "TRANSFER_OUT", "TRANSFER_IN", "BUY"]
    trade = booked[-1]
    assert (trade["quantity"], trade["unitPrice"], trade["fee"], trade["amount"]) == ("200", "25.5", "4.9", "5104.9")
    assert trade["date"].startswith("2024-05-02")
    [r2] = syncer.run()
    assert r2.created == 0


def test_the_check_page_offers_missing_positions_and_takes_an_opening(tmp_path):
    from brokersync.web.app import create_app

    from .fake_broker import FakeBroker

    wf = FakeWealthfolio()
    syncer, _ = setup(tmp_path, wf)
    syncer.run()
    state = State(tmp_path)
    # The last check: the broker holds 200 of a fund Wealthfolio has none of; the broker says it cost 25.50.
    state.set_reconcile("fake", [{"kind": "position", "key": "DE000TEST009", "name": "TEST Alt AG", "broker": "200",
                                  "wealthfolio": "0"}])
    state.set_meta("positions:fake", json.dumps([{"isin": "DE000TEST009", "name": "TEST Alt AG", "shares": "200",
                                                   "currency": "EUR", "cost": "25.50"}]))
    app = create_app(tmp_path, wealthfolio=syncer._wealthfolio, adapters={"fake": FakeBroker}, run_in_thread=False)
    client = TestClient(app, base_url="http://sync.local:8090")
    client.post("/setup-password", data={"password": "geheim123", "password2": "geheim123"})
    page = client.get("/check").text
    assert "Bestand ohne Kauf in Wealthfolio: 1" in page and 'value="25.50"' in page and 'value="200"' in page
    token = csrf(client, "/check")
    r = client.post("/check/fake/opening", data={"csrf": token, "isin": "DE000TEST009", "name": "TEST Alt AG",
                                                 "shares": "200", "day": "2099-01-01", "price": "25,50", "fee": "0"})
    assert "nicht in der Zukunft" in r.text
    r = client.post("/check/fake/opening", data={"csrf": token, "isin": "DE000TEST009", "name": "TEST Alt AG",
                                                 "shares": "200", "day": "2024-05-02", "price": "25,50", "fee": "4,90"})
    assert "der nächste Abruf bucht ihn" in r.text and "wird beim nächsten Abruf gebucht" in r.text
    assert state.openings("fake") == [{"isin": "DE000TEST009", "day": "2024-05-02", "name": "TEST Alt AG",
                                       "shares": "200", "price": "25.50", "fee": "4.90", "currency": "EUR"}]
    # Entered: no longer offered; it can be removed until it is booked.
    assert "Bestand ohne Kauf in Wealthfolio: 0" in r.text
    client.post("/check/fake/opening", data={"csrf": token, "isin": "DE000TEST009", "day": "2024-05-02",
                                             "action": "delete"})
    assert state.openings("fake") == []


def test_starting_over_deletes_only_the_syncs_activities(tmp_path):
    from brokersync.web.app import create_app

    from .fake_broker import FakeBroker

    wf = FakeWealthfolio()
    own = wf.add_existing(accountId="acc-cash", activityType="DEPOSIT", date="2026-01-01T10:00:00.000Z",
                          amount="50", currency="EUR", comment="von Hand", assetSymbol="", assetId="")
    syncer, _ = setup(tmp_path, wf)
    syncer.run()
    syncer.state.add_opening("fake", "DE000TEST009", "2024-05-02", "TEST Alt AG", "1", "10", "0")
    app = create_app(tmp_path, wealthfolio=syncer._wealthfolio, adapters={"fake": FakeBroker}, run_in_thread=False)
    client = TestClient(app, base_url="http://sync.local:8090")
    client.post("/setup-password", data={"password": "geheim123", "password2": "geheim123"})
    token = csrf(client, "/brokers/fake")
    r = client.post("/brokers/fake/reset", data={"csrf": token})
    assert "Häkchen" in r.text and len(wf.activities) == 8
    r = client.post("/brokers/fake/reset", data={"csrf": token, "confirm": "1"})
    assert "7 Buchungen des Dienstes gelöscht" in r.text
    assert wf.activities == [own]
    state = State(tmp_path)
    assert state.synced("fake") == {} and state.runs("fake") == [] and state.openings("fake")
    # The next run books everything again, the opening position included.
    [r] = syncer.run()
    assert r.created == 4
