"""The banks' cash accounts in Wealthfolio's spending tracker (#134)."""

import logging

from brokersync import config as config_mod

from .fakes import FakeWealthfolio
from .helpers import first_password
from .test_sync import setup
from .test_web import csrf, make


def turn(tmp_path, on: bool, cash: str = "acc-cash") -> None:
    cfg = config_mod.load(tmp_path)
    cfg.brokers["fake"].spending, cfg.brokers["fake"].cash_account_id = on, cash
    config_mod.save(tmp_path, cfg)


def test_off_asks_wealthfolio_nothing_and_books_as_before(tmp_path):
    wf, plain = FakeWealthfolio(), FakeWealthfolio()
    syncer, _ = setup(tmp_path / "a", wf)
    setup(tmp_path / "b", plain)[0].run()
    wf.spending = None  # any request to the tracker would fail
    [r] = syncer.run()
    assert r.status == "ok"
    assert [a["comment"] for a in wf.activities] == [a["comment"] for a in plain.activities]


def test_on_tracks_the_cash_account_and_off_removes_only_what_the_sync_added(tmp_path):
    wf = FakeWealthfolio()
    wf.spending["accountIds"] = ["acc-household"]  # the user's own choice in Wealthfolio
    syncer, _ = setup(tmp_path, wf)
    turn(tmp_path, True)
    syncer.run()
    assert wf.spending["enabled"] and wf.spending["accountIds"] == ["acc-household", "acc-cash"]
    comments = [a["comment"] for a in wf.activities]
    assert not wf.updated  # nothing of an activity changes

    turn(tmp_path, False)
    syncer.run()
    assert wf.spending["accountIds"] == ["acc-household"]
    assert [a["comment"] for a in wf.activities] == comments and not wf.updated


def test_an_account_the_user_tracked_before_stays_when_turned_off(tmp_path):
    wf = FakeWealthfolio()
    wf.spending.update(enabled=True, accountIds=["acc-cash"])
    syncer, _ = setup(tmp_path, wf)
    turn(tmp_path, True)
    syncer.run()
    turn(tmp_path, False)
    syncer.run()
    assert wf.spending["accountIds"] == ["acc-cash"]


def test_a_new_cash_account_replaces_the_old_one(tmp_path):
    wf = FakeWealthfolio()
    syncer, _ = setup(tmp_path, wf)
    turn(tmp_path, True)
    syncer.run()
    turn(tmp_path, True, cash="acc-cash-2")
    syncer.run()
    assert wf.spending["accountIds"] == ["acc-cash-2"]


def test_wealthfolio_without_the_tracker_never_fails_a_broker(tmp_path, caplog):
    wf = FakeWealthfolio()
    wf.spending = None
    syncer, _ = setup(tmp_path, wf)
    turn(tmp_path, True)
    caplog.set_level(logging.WARNING)
    [r] = syncer.run()
    assert r.status == "ok" and "spending tracker not updated" in caplog.text


def test_the_service_page_saves_every_cash_account_at_once(tmp_path):
    """#151: one list of all brokers' cash accounts on /services/spending; the broker form only links there."""
    client, _, _ = make(tmp_path)
    first_password(client)
    token = csrf(client, "/brokers/fake")
    page = client.get("/brokers/fake").text
    assert 'name="spending"' not in page and 'href="/services/spending"' in page
    data = {"csrf": token, "cash_account_id": "acc-cash", "portfolio_account_id": "acc-depot"}
    client.post("/brokers/fake", data=data)
    page = client.get("/services/spending").text
    assert 'name="broker" value="fake"' in page
    r = client.post("/services/spending/settings", data={"csrf": token, "broker": "nope"})
    assert r.status_code == 422 and 'id="err-broker"' in r.text
    assert not config_mod.load(tmp_path).broker("fake").spending
    client.post("/services/spending/settings", data={"csrf": token, "broker": "fake"})
    assert config_mod.load(tmp_path).broker("fake").spending
    client.post("/brokers/fake", data=data)  # the broker form leaves it
    assert config_mod.load(tmp_path).broker("fake").spending
    client.post("/services/spending/settings", data={"csrf": token})
    assert not config_mod.load(tmp_path).broker("fake").spending
