from datetime import UTC, datetime
from decimal import Decimal as D

import httpx
import pytest

from brokersync import config as config_mod
from brokersync.adapters import AdapterError, BrokerAdapter
from brokersync.adapters.dummy import DummyAdapter
from brokersync.model import Kind, Transaction
from brokersync.notify import Notifier
from brokersync.sync import AlreadyRunning, Syncer, run_lock
from brokersync.vault import Vault
from brokersync.wealthfolio import WealthfolioClient

from .fakes import PASSWORD, FakeWealthfolio


class RecordingNotifier(Notifier):
    def __init__(self):
        super().__init__("https://ntfy.example", "topic")
        self.sent: list[tuple[str, str, str | None]] = []

    def send(self, title, message, *, link=None, priority="default", tags=""):
        self.sent.append((title, message, link))
        return True


class BrokenAdapter(BrokerAdapter):
    key = "broken"
    label = "Kaputt"

    def login(self):
        raise AdapterError("broker is down")

    def get_accounts(self):
        return []

    def get_positions(self):
        return []

    def get_transactions(self, since):
        return []

    def get_cash(self):
        return []


# Login to the dummy at 16:00 German time: deposit 13:00, buy 14:00, dividend 15:00.
ANCHOR = "2026-01-05T15:00:00+00:00"


def setup(tmp_path, wf: FakeWealthfolio, brokers=("dummy",), confirmed=True):
    cfg = config_mod.Config(wealthfolio_url="http://wf.local:8080", public_url="http://sync.local:8090")
    for key in brokers:
        cfg.brokers[key] = config_mod.BrokerConfig(enabled=True, cash_account_id="acc-cash",
                                                   portfolio_account_id="acc-depot")
    config_mod.save(tmp_path, cfg)
    vault = Vault(tmp_path)
    vault.update(lambda d: d.__setitem__("wealthfolio_password", PASSWORD))
    for key in brokers:
        vault.set_broker_credentials(key, {"username": "test"})
    if confirmed:
        vault.set_broker_session("dummy", {"confirmed": True, "anchor": ANCHOR})
    notifier = RecordingNotifier()
    syncer = Syncer(tmp_path, wealthfolio=lambda url, pw: WealthfolioClient(url, pw, transport=wf.transport()),
                    notifier=notifier, adapters={"dummy": DummyAdapter, "broken": BrokenAdapter})
    return syncer, notifier


def test_first_run_books_everything_second_run_nothing(tmp_path):
    wf = FakeWealthfolio()
    syncer, notifier = setup(tmp_path, wf)
    [r] = syncer.run()
    assert (r.status, r.created, r.existing, r.failed, r.unknown) == ("ok", 3, 0, 0, 1)
    types = [a["activityType"] for a in wf.activities]
    assert types == ["DEPOSIT", "TRANSFER_OUT", "TRANSFER_IN", "BUY", "DIVIDEND", "TRANSFER_OUT", "TRANSFER_IN"]
    # The unknown event is reported once, with a link to the list.
    assert [(t, link) for t, _, link in notifier.sent] == [
        ("Dummy (Test): 1 unbekannte Buchungen", "http://sync.local:8090/unknown")]
    assert syncer.state.unknown_events()[0]["raw_type"] == "DUMMY_SPECIAL_EVENT"
    # Holdings check: 502.68 EUR cash and 5 shares, as the dummy reports.
    assert syncer.state.reconcile("dummy")["deviations"] == []

    posted = len(wf.created)
    [r2] = syncer.run()
    # Fetches from the last success minus the overlap, so the old events aren't even read again.
    assert (r2.status, r2.created, r2.existing) == ("ok", 0, 0)
    assert len(wf.created) == posted  # nothing even sent
    assert len(notifier.sent) == 1  # the unknown event isn't reported again


def test_lost_state_is_recovered_from_the_comments(tmp_path):
    wf = FakeWealthfolio()
    syncer, _ = setup(tmp_path, wf)
    syncer.run()
    count = len(wf.activities)
    (tmp_path / "state.db").unlink()
    syncer2, _ = setup(tmp_path, wf)
    [r] = syncer2.run()
    assert (r.created, r.existing) == (0, 3)
    assert len(wf.activities) == count


def test_activities_from_the_addons_csv_import_are_recognised(tmp_path):
    wf = FakeWealthfolio()
    # What the addon's CSV import booked for the same buy: other comment and time.
    wf.add_existing(accountId="acc-depot", activityType="BUY", date="2026-01-05T13:00:31.123Z", quantity="5",
                    unitPrice="100", amount="501", fee="1", currency="EUR", comment="Buy Testfonds [13:00:31.123]",
                    assetSymbol="IE00B4L5Y983", assetId="IE00B4L5Y983")
    wf.add_existing(accountId="acc-cash", activityType="DEPOSIT", date="2026-01-05T12:00:05.000Z", quantity="1",
                    unitPrice="1", amount="1000", currency="EUR", comment="Einzahlung", assetSymbol="$CASH-EUR")
    syncer, _ = setup(tmp_path, wf)
    [r] = syncer.run()
    assert (r.created, r.existing) == (1, 2)
    assert [a["activityType"] for a in wf.created] == ["DIVIDEND", "TRANSFER_OUT", "TRANSFER_IN"]


def test_partial_failure_is_retried_without_duplicating_created_legs(tmp_path):
    wf = FakeWealthfolio()
    wf.fail_types = {"BUY"}
    syncer, notifier = setup(tmp_path, wf)
    [r] = syncer.run()
    assert (r.status, r.created, r.failed) == ("error", 2, 1)
    assert "simulated failure" in r.messages[0]
    assert any("nicht übernommen" in t for t, _, _ in notifier.sent)
    wf.fail_types = set()
    [r2] = syncer.run()
    assert (r2.status, r2.created) == ("ok", 1)
    # Each transfer leg of the buy exists once.
    legs = [a for a in wf.activities if a.get("sourceGroupId") == "sync-dummy-TEST-20260105-2"]
    assert sorted(a["activityType"] for a in legs) == ["TRANSFER_IN", "TRANSFER_OUT"]


def test_a_failing_broker_is_reported_and_the_others_still_run(tmp_path):
    wf = FakeWealthfolio()
    syncer, notifier = setup(tmp_path, wf, brokers=("broken", "dummy"))
    results = {r.broker: r for r in syncer.run()}
    assert results["broken"].status == "error"
    assert results["dummy"].status == "ok" and results["dummy"].created == 3
    assert ("Kaputt: Abruf fehlgeschlagen", "broker is down", "http://sync.local:8090/") in notifier.sent


def test_needs_auth_notifies_with_the_login_link(tmp_path):
    wf = FakeWealthfolio()
    syncer, notifier = setup(tmp_path, wf, confirmed=False)
    [r] = syncer.run()
    assert r.status == "needs_auth"
    assert notifier.sent[0][0] == "Dummy (Test): Anmeldung nötig"
    assert notifier.sent[0][2] == "http://sync.local:8090/brokers/dummy/login"
    assert wf.created == []


def test_since_uses_last_success_with_overlap(tmp_path):
    seen = []

    class Recording(DummyAdapter):
        def get_transactions(self, since):
            seen.append(since)
            return []

    wf = FakeWealthfolio()
    syncer, _ = setup(tmp_path, wf)
    syncer.adapters = {"dummy": Recording}
    cfg = config_mod.load(tmp_path)
    cfg.brokers["dummy"].start_date = "2026-01-01"
    config_mod.save(tmp_path, cfg)
    syncer.run()
    syncer.run()
    assert seen[0] == datetime(2026, 1, 1, tzinfo=UTC)
    assert seen[1] is not None and seen[1] < datetime.now(UTC)


def test_inconsistent_transactions_fail_visibly(tmp_path):
    class Bad(DummyAdapter):
        def get_transactions(self, since):
            return [Transaction("b1", Kind.BUY, datetime(2026, 2, 1, tzinfo=UTC), "EUR", D("999"), isin="IE00B4L5Y983",
                                name="X", shares=D(1), gross=D(100), label="Kauf")]

    wf = FakeWealthfolio()
    syncer, _ = setup(tmp_path, wf)
    syncer.adapters = {"dummy": Bad}
    [r] = syncer.run()
    assert (r.status, r.failed) == ("error", 1)
    assert "don't add up" in r.messages[0]
    assert wf.created == []


def test_only_one_run_at_a_time(tmp_path):
    with run_lock(tmp_path):
        with pytest.raises(AlreadyRunning):
            with run_lock(tmp_path):
                pass


def test_wealthfolio_unreachable_is_an_error_not_a_crash(tmp_path):
    def down(request):
        raise httpx.ConnectError("connection refused")

    syncer, notifier = setup(tmp_path, FakeWealthfolio())
    syncer._wealthfolio = lambda url, pw: WealthfolioClient(url, pw, transport=httpx.MockTransport(down))
    [r] = syncer.run()
    assert r.status == "error" and "not reachable" in r.messages[0]
    assert notifier.sent[-1][0] == "Dummy (Test): Abruf fehlgeschlagen"


def test_a_failed_sweep_after_a_created_dividend_is_completed(tmp_path):
    wf = FakeWealthfolio()
    wf.fail_types = {"TRANSFER_OUT"}
    syncer, _ = setup(tmp_path, wf)
    syncer.run()
    assert [a["activityType"] for a in wf.activities] == ["DEPOSIT", "DIVIDEND"]
    wf.fail_types = set()
    syncer.run()
    div_group = [a["activityType"] for a in wf.activities if "TEST-20260105-3" in a["comment"]]
    assert sorted(div_group) == ["DIVIDEND", "TRANSFER_IN", "TRANSFER_OUT"]
    assert len([a for a in wf.activities if a["activityType"] == "BUY"]) == 1


def test_a_run_waits_briefly_for_the_lock_and_closes_stale_runs(tmp_path):
    import threading
    import time

    wf = FakeWealthfolio()
    syncer, _ = setup(tmp_path, wf)
    syncer.state.start_run("dummy")  # left over by a crashed process
    with run_lock(tmp_path):
        t = threading.Thread(target=syncer.run)
        t.start()
        time.sleep(0.3)  # the status page holds the lock briefly
    t.join(10)
    runs = syncer.state.runs()
    assert [r.status for r in runs] == ["ok", "aborted"]


def test_automatic_fetch_off_skips_the_broker_unless_it_is_requested_by_name(tmp_path):
    wf = FakeWealthfolio()
    syncer, _ = setup(tmp_path, wf)
    cfg = config_mod.load(tmp_path)
    cfg.brokers["dummy"].enabled = False
    config_mod.save(tmp_path, cfg)
    assert syncer.run() == [] and wf.activities == []  # timer and "Alle abrufen"
    [r] = syncer.run(["dummy"])  # the broker's own button
    assert (r.status, r.created) == ("ok", 3)


def test_a_known_unknown_event_gets_the_newer_payload_but_is_not_reported_again(tmp_path):
    from brokersync.state import State

    st = State(tmp_path)
    assert st.add_unknown("tr", "e1", "X", "2026-01-01", {"eventType": "X"}) is True
    assert st.add_unknown("tr", "e1", "X", "2026-01-01", {"eventType": "X", "betrag": "1 EUR"}) is False
    assert st.unknown_events("tr")[0]["payload"]["betrag"] == "1 EUR"


class CrashingAdapter(BrokenAdapter):
    """A bug in an adapter: an exception the sync doesn't expect, after a login."""

    def login(self):
        self.session["token"] = "kept"

    def get_transactions(self, since):
        raise KeyError("eventType")


def test_a_crashing_adapter_is_reported_keeps_its_session_and_the_others_still_run(tmp_path):
    wf = FakeWealthfolio()
    syncer, notifier = setup(tmp_path, wf, brokers=("broken", "dummy"))
    syncer.adapters = {"dummy": DummyAdapter, "broken": CrashingAdapter}
    results = {r.broker: r for r in syncer.run()}
    assert results["broken"].status == "error"
    assert results["broken"].messages == ["Unexpected error: 'eventType'"]
    assert ("Kaputt: Abruf fehlgeschlagen", "Unerwarteter Fehler: 'eventType'", "http://sync.local:8090/") \
        in notifier.sent
    assert results["dummy"].status == "ok" and results["dummy"].created == 3
    # The session is stored even though the run crashed after the login.
    assert syncer.vault.load()["brokers"]["broken"]["session"] == {"token": "kept"}
    assert [r.status for r in syncer.state.runs("broken")] == ["error"]


def test_a_broker_without_wealthfolio_accounts_is_an_error_and_unknown_brokers_are_ignored(tmp_path):
    wf = FakeWealthfolio()
    syncer, notifier = setup(tmp_path, wf)
    cfg = config_mod.load(tmp_path)
    cfg.brokers["dummy"].portfolio_account_id = ""
    cfg.brokers["gone"] = config_mod.BrokerConfig(enabled=True)  # an adapter this version doesn't have
    config_mod.save(tmp_path, cfg)
    [r] = syncer.run()
    assert (r.broker, r.status) == ("dummy", "error")
    assert "No Wealthfolio accounts assigned" in r.messages[0]
    assert wf.activities == []


class NoBalanceDummy(DummyAdapter):
    def get_cash(self):
        raise AdapterError("balance not offered")

    def get_positions(self):
        raise NotImplementedError


def test_a_broker_without_balance_still_books_but_skips_the_check(tmp_path):
    wf = FakeWealthfolio()
    syncer, _ = setup(tmp_path, wf)
    syncer.adapters = {"dummy": NoBalanceDummy}
    [r] = syncer.run()
    assert (r.status, r.created) == ("ok", 3)
    assert syncer.state.balances("dummy") == []
    assert syncer.state.reconcile("dummy") is None


def test_holdings_wealthfolio_cannot_answer_do_not_fail_the_run(tmp_path):
    wf = FakeWealthfolio()

    def handle(request):
        if request.url.path == "/api/v1/holdings":
            return httpx.Response(500, json={"code": 500, "message": "recalculating"})
        return wf.handle(request)

    syncer, notifier = setup(tmp_path, wf)
    syncer._wealthfolio = lambda url, pw: WealthfolioClient(url, pw, transport=httpx.MockTransport(handle))
    [r] = syncer.run()
    assert (r.status, r.created) == ("ok", 3)
    assert syncer.state.balances("dummy")  # the broker's balance is still shown
    assert syncer.state.reconcile("dummy") is None
    assert not any("Bestand" in t for t, _, _ in notifier.sent)
