"""One parser for every moment from Wealthfolio or the state (#119)."""

from datetime import UTC, date, datetime, timedelta, timezone

from brokersync import audit, coverage
from brokersync.dedup import ExistingIndex
from brokersync.mapping import Accounts
from brokersync.model import when


def test_every_form_becomes_utc():
    utc = datetime(2026, 1, 2, 10, 0, tzinfo=UTC)
    assert when("2026-01-02T10:00:00.000Z") == utc
    assert when("2026-01-02T11:00:00+01:00") == utc
    assert when("2026-01-02T10:00:00") == utc  # no zone: taken as UTC
    assert when("2026-01-02") == datetime(2026, 1, 2, tzinfo=UTC)  # only a day: its midnight
    assert when(date(2026, 1, 2)) == datetime(2026, 1, 2, tzinfo=UTC)
    assert when(datetime(2026, 1, 2, 11, tzinfo=timezone(timedelta(hours=1)))) == utc
    assert when(datetime(2026, 1, 2, 10)) == utc


def test_dates_without_zone_or_time_crash_nothing():
    acts = [{"id": "a1", "accountId": "c", "activityType": "DEPOSIT", "date": "2026-01-02", "amount": "5",
             "currency": "EUR", "comment": "x [SYNC fake:t1]"},
            {"id": "a2", "accountId": "d", "activityType": "BUY", "date": "2026-01-03T10:00:00", "amount": "5",
             "quantity": "1", "unitPrice": "5", "currency": "EUR", "comment": "Import"}]
    since = datetime(2026, 1, 1, tzinfo=UTC)
    gaps = coverage.check("fake", [], {"t1": ("created", ["a1"])}, acts, full_history=True, since=since)
    assert [g.kind for g in gaps] == [coverage.ORPHAN]
    assert audit.check(acts, Accounts("c", "d")) is not None
    ExistingIndex(acts)
