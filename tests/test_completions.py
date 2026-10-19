"""Unknown bookings completed on *Unbekannt* (waldonso2/wealthfolio-importer-addon#132) and splits (#133)."""

from decimal import Decimal as D

import pytest

from brokersync.history import COMPLETION_KINDS, completion
from brokersync.mapping import Accounts, MappingError, to_activities
from brokersync.model import Kind
from brokersync.state import State

from .fakes import FakeWealthfolio
from .helpers import first_password
from .test_sync import setup
from .test_web import csrf, make

ISIN = "IE00B4L5Y983"
ACCOUNTS = Accounts("acc-cash", "acc-depot")
UNKNOWN_ID = "TEST-20260105-4"  # the fake broker's unknown event on the day of test_sync.ANCHOR


def test_a_split_is_one_activity_with_its_ratio_on_the_depot():
    tx = completion("ev-1", {"kind": "split", "isin": ISIN, "ratio": "0.1", "day": "2026-04-20T08:00:00+00:00",
                             "name": "Brent 3x Short"})
    [a] = to_activities(tx, "tr", ACCOUNTS)
    assert (a["accountId"], a["activityType"], a["amount"], a["quantity"]) == ("acc-depot", "SPLIT", "0.1", "0")
    assert a["asset"]["symbol"] == ISIN and a["comment"].endswith("[SYNC tr:ev-1]")
    assert a["activityDate"].startswith("2026-04-20")
    with pytest.raises(MappingError, match="split ratio"):
        to_activities(completion("ev-1", {"kind": "split", "isin": ISIN, "ratio": "0", "day": "2026-04-20"}),
                      "tr", ACCOUNTS)


@pytest.mark.parametrize("kind", COMPLETION_KINDS)
def test_every_kind_books_by_the_booking_model(kind):
    c = {"kind": kind, "isin": ISIN, "day": "2026-01-05", "shares": "2", "amount": "23.20", "fee": "1",
         "ratio": "2"}
    tx = completion("ev-2", c)
    acts = to_activities(tx, "tr", ACCOUNTS)
    types = [a["activityType"] for a in acts]
    expected = {"buy": ["TRANSFER_OUT", "TRANSFER_IN", "BUY"],
                "sell": ["SELL", "TRANSFER_OUT", "TRANSFER_IN"],
                "in": ["DEPOSIT", "TRANSFER_OUT", "TRANSFER_IN", "BUY"],
                "out": ["SELL"],
                "split": ["SPLIT"]}
    assert types == expected[kind]
    if kind == "buy":
        assert tx.kind == Kind.BUY and (tx.net, tx.gross, tx.fee) == (D("23.20"), D("22.20"), D("1"))
    if kind == "out":
        assert acts[0]["amount"] in ("0", "0.00")


def test_a_completed_unknown_booking_is_booked_once_and_leaves_the_list(tmp_path):
    wf = FakeWealthfolio()
    syncer, _ = setup(tmp_path, wf)
    syncer.run()
    state = State(tmp_path)
    assert [e["tx_id"] for e in state.unknown_events("fake")] == [UNKNOWN_ID]
    state.add_completion("fake", UNKNOWN_ID, {"kind": "buy", "isin": ISIN, "day": "2026-01-05", "shares": "0.2",
                                              "amount": "23.20"})
    [r] = syncer.run()
    assert r.created == 1 and r.unknown == 0
    mine = [a for a in wf.activities if f"[SYNC fake:{UNKNOWN_ID}]" in a["comment"]]
    assert [a["activityType"] for a in mine] == ["TRANSFER_OUT", "TRANSFER_IN", "BUY"]
    assert state.unknown_events("fake") == []
    syncer.run()
    assert len([a for a in wf.activities if f"[SYNC fake:{UNKNOWN_ID}]" in a["comment"]]) == 3


def test_the_form_checks_and_saves_and_withdraws(tmp_path):
    client, wf, _ = make(tmp_path)
    first_password(client)
    State(tmp_path).add_unknown("fake", "ev-9", "SSP_CORPORATE_ACTION_NO_CASH", "2026-04-20T08:00:00+00:00",
                                {"eventType": "SSP_CORPORATE_ACTION_NO_CASH", "subtitle": "Reverse Split"})
    page = client.get("/unknown").text
    assert "Vervollständigen und buchen" in page
    token = csrf(client, "/unknown")
    base = {"csrf": token, "broker": "fake", "tx_id": "ev-9", "kind": "split"}
    r = client.post("/unknown/complete", data={**base, "isin": "IE00B4L5Y984", "ratio": "0,1"})
    assert "Keine gültige ISIN" in r.text and not State(tmp_path).completions()
    r = client.post("/unknown/complete", data={**base, "isin": ISIN})
    assert "Verhältnis: bitte angeben" in r.text
    r = client.post("/unknown/complete", data={**base, "isin": ISIN, "ratio": "0,1"})
    assert "der nächste Abruf bucht es" in r.text
    assert State(tmp_path).completions() == {("fake", "ev-9"): {
        "kind": "split", "isin": ISIN, "day": "2026-04-20T08:00:00+00:00", "currency": "EUR", "name": "",
        "ratio": "0.1"}}
    assert "Vorgemerkt als Split / Reverse Split" in client.get("/unknown").text
    client.post("/unknown/complete", data={**base, "action": "delete"})
    assert not State(tmp_path).completions()
    r = client.post("/unknown/complete", data={**base, "tx_id": "nope", "isin": ISIN, "ratio": "1"})
    assert "nicht mehr offen" in r.text
