"""The comparison of the broker with Wealthfolio after each run (brokersync.coverage)."""

from datetime import UTC, datetime
from decimal import Decimal as D

from fastapi.testclient import TestClient

from brokersync import coverage
from brokersync.model import Kind, Transaction
from brokersync.state import State

from .fake_broker import FakeBroker
from .fakes import FakeWealthfolio
from .test_sync import setup
from .test_web import csrf

DIVIDEND = "TEST-20260105-3"
BUY = "TEST-20260105-2"


def tx(tx_id: str, kind=Kind.DEPOSIT, day=5) -> Transaction:
    return Transaction(id=tx_id, kind=kind, datetime=datetime(2026, 1, day, 12, tzinfo=UTC), currency="EUR",
                       net=D("10"), label="TEST")


def act(aid: str, tx_id: str, broker="fake", date="2026-01-05T12:00:00Z") -> dict:
    return {"id": aid, "accountId": "acc-cash", "activityType": "DEPOSIT", "date": date, "amount": "10",
            "currency": "EUR", "comment": f"TEST [SYNC {broker}:{tx_id}]"}


class FullFake(FakeBroker):
    """Reads its whole history every run, like Scalable; ``hidden`` ids are gone at the broker."""
    full_history = True
    hidden: frozenset[str] = frozenset()

    def get_transactions(self, since):
        return [t for t in super().get_transactions(since) if t.id not in self.hidden]


def full_setup(tmp_path, wf):
    syncer, notifier = setup(tmp_path, wf)
    syncer.adapters = {"fake": FullFake}
    return syncer, notifier


def test_check_classifies_what_is_gone_and_what_is_left_over():
    txs = [tx("a"), tx("b"), tx("c"), tx("d"), tx("e"), tx("new"), tx("u", Kind.UNKNOWN)]
    synced = {"a": ("imported", ["1"]),          # there
              "b": ("imported", ["2", "3"]),     # one of two legs gone
              "c": ("imported", ["4"]),          # gone
              "d": ("existing", ["csv-1"]),      # the import activity it matched is gone
              "e": ("ignored", [])}
    activities = [act("1", "a"), act("2", "b"), act("9", "gone"), act("8", "old", date="2025-01-01T00:00:00Z"),
                  act("7", "x", broker="other")]
    gaps = coverage.check("fake", txs, synced, activities, full_history=True,
                          since=datetime(2025, 6, 1, tzinfo=UTC))
    assert sorted((g.kind, g.ref) for g in gaps) == [
        ("ignored", "e"), ("missing", "c"), ("missing", "d"), ("orphan", "9"), ("partial", "b")]
    assert next(g for g in gaps if g.kind == "orphan").tx_id == "gone"
    # Without the whole history there is nothing to say about activities the broker didn't list.
    gaps = coverage.check("fake", txs, synced, activities, full_history=False, since=None)
    assert "orphan" not in {g.kind for g in gaps}


def test_a_deleted_activity_is_reported_not_recreated_until_the_user_says_so(tmp_path):
    wf = FakeWealthfolio()
    syncer, notifier = full_setup(tmp_path, wf)
    syncer.run()
    assert syncer.state.gaps("fake") == []
    # The user deletes the dividend and its sweep in Wealthfolio.
    for a in [a for a in wf.activities if DIVIDEND in (a.get("comment") or "")]:
        wf.activities.remove(a)
    count = len(wf.activities)
    [r] = syncer.run()
    assert (r.status, r.created) == ("ok", 0)
    assert len(wf.activities) == count
    assert [(g["kind"], g["ref"]) for g in syncer.state.gaps("fake")] == [("missing", DIVIDEND)]
    title, message, link = notifier.sent[-1]
    assert "Abweichung zu Wealthfolio" in title and "1 schon übernommene" in message and link.endswith("/check")
    syncer.run()
    assert sum("Abweichung zu Wealthfolio" in t for t, _, _ in notifier.sent) == 1  # reported once

    # "Wieder anlegen": the next run books it again.
    syncer.state.forget("fake", [DIVIDEND])
    [r] = syncer.run()
    assert r.created == 1
    assert sorted(a["activityType"] for a in wf.activities if DIVIDEND in (a.get("comment") or "")) == [
        "DIVIDEND", "TRANSFER_IN", "TRANSFER_OUT"]
    syncer.run()
    assert syncer.state.gaps("fake") == []


def test_a_missing_leg_is_partial_and_ignored_ones_stay_away(tmp_path):
    wf = FakeWealthfolio()
    syncer, notifier = full_setup(tmp_path, wf)
    syncer.run()
    wf.activities.remove(next(a for a in wf.activities if a["activityType"] == "BUY"))
    syncer.run()
    assert [(g["kind"], g["ref"]) for g in syncer.state.gaps("fake")] == [("partial", BUY)]
    syncer.state.ignore("fake", [BUY])
    [r] = syncer.run()
    assert r.created == 0 and not any(a["activityType"] == "BUY" for a in wf.activities)
    assert [(g["kind"], g["ref"]) for g in syncer.state.gaps("fake")] == [("ignored", BUY)]
    assert sum("Abweichung zu Wealthfolio" in t for t, _, _ in notifier.sent) == 1
    # "Doch anlegen": only the missing leg is new, the transfers are recognised as there.
    syncer.state.forget("fake", [BUY])
    [r] = syncer.run()
    assert r.created == 1
    assert sorted(a["activityType"] for a in wf.activities if BUY in (a.get("comment") or "")) == [
        "BUY", "TRANSFER_IN", "TRANSFER_OUT"]


def test_a_full_history_finds_activities_the_broker_no_longer_lists(tmp_path, monkeypatch):
    wf = FakeWealthfolio()
    syncer, notifier = full_setup(tmp_path, wf)
    syncer.run()
    assert len(syncer.state.unknown_events("fake")) == 1
    # Cancelled at the broker: the dividend disappears, and so does the unknown event.
    monkeypatch.setattr(FullFake, "hidden", frozenset({DIVIDEND, "TEST-20260105-4"}))
    count = len(wf.activities)
    syncer.run()
    assert len(wf.activities) == count and not wf.deleted  # nothing deleted
    gaps = syncer.state.gaps("fake")
    assert sorted(g["label"] for g in gaps) == ["DIVIDEND", "TRANSFER_IN", "TRANSFER_OUT"]
    assert {(g["kind"], g["tx_id"]) for g in gaps} == {("orphan", DIVIDEND)}
    assert "bei Testbroker nicht (mehr)" in notifier.sent[-1][1]
    assert syncer.state.unknown_events("fake") == []


def test_the_check_page_lets_the_user_rebook_or_ignore(tmp_path):
    from brokersync.web.app import create_app

    wf = FakeWealthfolio()
    syncer, _ = full_setup(tmp_path, wf)
    syncer.run()
    for a in [a for a in wf.activities if DIVIDEND in (a.get("comment") or "")]:
        wf.activities.remove(a)
    syncer.run()
    app = create_app(tmp_path, wealthfolio=syncer._wealthfolio, adapters={"fake": FullFake}, run_in_thread=False)
    client = TestClient(app, base_url="http://sync.local:8090")
    client.post("/setup-password", data={"password": "geheim-123456", "password2": "geheim-123456"})
    page = client.get("/check").text
    assert "Schon übernommen, fehlt aber in Wealthfolio: 1" in page and f'value="{DIVIDEND}"' in page
    token = csrf(client, "/check")
    r = client.post("/check/fake", data={"csrf": token, "action": "ignore", "tx": "not-a-gap"})
    assert "Keine Buchung ausgewählt" in r.text
    r = client.post("/check/fake", data={"csrf": token, "action": "ignore", "tx": DIVIDEND})
    assert "werden nicht mehr gebucht" in r.text and "Ignoriert: 1" in r.text
    assert State(tmp_path).synced("fake")[DIVIDEND][0] == "ignored"
    r = client.post("/check/fake", data={"csrf": token, "action": "rebook", "tx": DIVIDEND})
    assert "legt der nächste Abruf wieder an" in r.text
    assert DIVIDEND not in State(tmp_path).synced("fake")
    [r] = syncer.run()
    assert r.created == 1
