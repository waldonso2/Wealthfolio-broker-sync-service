"""Dummy test data must be easy to find in a real Wealthfolio: dated on the day
of the login and marked TEST."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from brokersync.adapters.dummy import CODE, DummyAdapter, transactions
from brokersync.mapping import Accounts, to_activities
from brokersync.model import Kind

BERLIN = ZoneInfo("Europe/Berlin")


def test_dated_on_the_login_day_and_before_the_login():
    for anchor in ("2026-10-09T13:42:00+00:00", "2026-10-08T22:20:00+00:00", "2026-10-09T00:01:00+02:00"):
        a = datetime.fromisoformat(anchor)
        txs = transactions(a)
        days = {t.datetime.astimezone(BERLIN).date() for t in txs}
        assert days == {a.astimezone(BERLIN).date()}, anchor
        times = [t.datetime for t in txs]
        assert times == sorted(times) and all(t <= a for t in times), anchor


def test_everything_carries_test_in_its_text():
    txs = [t for t in transactions(datetime(2026, 10, 9, 12, tzinfo=UTC)) if t.kind != Kind.UNKNOWN]
    for tx in txs:
        for p in to_activities(tx, "dummy", Accounts("c", "p")):
            assert "TEST" in p["comment"], p["comment"]


def test_login_fixes_the_date_so_the_timer_books_nothing_new():
    d = DummyAdapter({}, {})
    d.complete_login(CODE)
    first = [t.id for t in d.get_transactions(None)]
    again = DummyAdapter({}, d.session_state())
    again.login()
    assert [t.id for t in again.get_transactions(None)] == first
    assert first[0].startswith(f"TEST-{datetime.now(BERLIN):%Y%m%d}-")


def test_old_sessions_get_a_date_on_the_next_login():
    d = DummyAdapter({}, {"confirmed": True})
    d.login()
    assert "anchor" in d.session_state()
