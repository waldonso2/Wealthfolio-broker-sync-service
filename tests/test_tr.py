"""Trade Republic adapter: timeline mapping (via pytr's parser), web login with
app confirmation, session cookies, PIN safety, and the holdings check."""

import json
from datetime import UTC, datetime
from decimal import Decimal as D
from pathlib import Path

import pytest
import requests

from brokersync import config as config_mod
from brokersync.adapters.base import AdapterError, AuthRequired
from brokersync.adapters.tr import TradeRepublicAdapter
from brokersync.mapping import Accounts, to_activities
from brokersync.model import CashBalance, Kind, Position
from brokersync.reconcile import compare
from brokersync.sync import Syncer
from brokersync.vault import Vault
from brokersync.wealthfolio import WealthfolioClient

from .fakes import PASSWORD, FakeWealthfolio
from .test_sync import RecordingNotifier

CASE = json.loads((Path(__file__).parent / "contract" / "tr" / "timeline.json").read_text())
RECORDING = CASE["recording"]
CREDS = {"phone": "+49 170 0000000", "pin": "1234"}


def txs():
    return TradeRepublicAdapter.replay(RECORDING).get_transactions(datetime(2026, 8, 1, tzinfo=UTC))


def by_raw(raw_type):
    return next(t for t in txs() if t.raw_type == raw_type)


def test_trades_book_like_the_csv_import():
    buy = by_raw("TRADING_TRADE_EXECUTED")
    assert (buy.kind, buy.net, buy.gross, buy.fee, buy.shares) == (Kind.BUY, D("111"), D("110"), D("1"),
                                                                     D("0.685102"))
    acts = to_activities(buy, "tr", Accounts("c", "p"))
    assert [(a["accountId"], a["activityType"]) for a in acts] == [
        ("c", "TRANSFER_OUT"), ("p", "TRANSFER_IN"), ("p", "BUY")]
    assert acts[0]["amount"] == acts[1]["amount"] == "111"
    # amount = shares × unit price (6 decimals, as the addon) + fee: within a cent of the cash paid.
    trade = acts[2]
    assert (trade["unitPrice"], trade["fee"]) == ("160.560033", "1")
    assert abs(D(trade["amount"]) - D("111")) < D("0.000001")
    sell = next(t for t in txs() if t.kind == Kind.SELL)
    assert (sell.net, sell.gross, sell.fee, sell.tax) == (D("200.93"), D("202.09"), D("1"), D("0.16"))
    acts = to_activities(sell, "tr", Accounts("c", "p"))
    assert [a["activityType"] for a in acts] == ["SELL", "TRANSFER_OUT", "TRANSFER_IN"]
    assert abs(D(next(a for a in acts if a["activityType"] == "SELL")["amount"]) - D("200.93")) < D("0.000001")


def test_dividend_net_with_withholding_tax():
    div = next(t for t in txs() if t.kind == Kind.DIVIDEND)
    (d, *_) = to_activities(div, "tr", Accounts("c", "p"))
    assert (d["activityType"], d["amount"], d["tax"], d["quantity"]) == ("DIVIDEND", "2.24", "0.78", "10.640298")


def test_saveback_is_a_bonus_funded_buy_without_cash_transfer():
    sb = next(t for t in txs() if t.bonus_funded)
    acts = to_activities(sb, "tr", Accounts("c", "p"))
    assert [(a["accountId"], a["activityType"], a.get("subtype")) for a in acts] == [
        ("p", "CREDIT", "BONUS"), ("p", "BUY", None)]
    assert acts[0]["amount"] == "15" and abs(D(acts[1]["amount"]) - D("15")) < D("0.000001")


def test_cash_events_and_taxes():
    kinds = {t.raw_type: (t.kind, t.net, t.label) for t in txs()}
    assert kinds["CARD_TRANSACTION"] == (Kind.WITHDRAWAL, D("4.28"), "Kartenzahlung")
    assert kinds["CARD_REFUND"] == (Kind.DEPOSIT, D("24.99"), "Kartenerstattung")
    assert kinds["OUTGOING_TRANSFER"][0] == Kind.WITHDRAWAL
    assert kinds["INTEREST_PAYOUT"][:2] == (Kind.INTEREST, D("4.87"))
    assert kinds["SSP_TAX_CORRECTION_INVOICE"][:2] == (Kind.TAX_REFUND, D("2.87"))
    vorab = next(t for t in txs() if t.label == "Vorabpauschale")
    assert (vorab.kind, vorab.net) == (Kind.TAX, D("0.19"))


def test_unknown_and_informational_events():
    out = txs()
    unknown = [t for t in out if t.kind == Kind.UNKNOWN]
    assert [t.raw_type for t in unknown] == ["SSP_CORPORATE_ACTION_INVOICE_SHARES (SPLIT)"]
    assert set(unknown[0].raw) == {"eventType", "title", "subtitle", "status"}  # nothing personal
    ids = {t.id for t in out}
    assert "00000000-0000-4000-8000-000000000014" not in ids  # address change: informational
    assert "00000000-0000-4000-8000-000000000015" not in ids  # cancelled transfer


# ── login ───────────────────────────────────────────────────────────────────
class FakeJar(requests.cookies.RequestsCookieJar):
    def __init__(self, filename):
        super().__init__()
        self.filename = filename

    def save(self, ignore_discard=True):
        line = "\t".join([".traderepublic.com", "TRUE", "/", "TRUE", "0", "tr_session", "abc"])
        Path(self.filename).write_text(f"# Netscape HTTP Cookie File\n{line}\n")


class FakeApi:
    instances: list = []

    def __init__(self, phone, pin, cookies, *, resume=False, authenticator=False, confirm=True, reject=False):
        self.phone, self.pin, self.cookies_file = phone, pin, cookies
        self.resume, self.authenticator, self.confirm, self.reject = resume, authenticator, confirm, reject
        self._websession = type("S", (), {"cookies": FakeJar(cookies)})()
        self.calls = []
        self.seen_cookies = Path(cookies).read_text()
        FakeApi.instances.append(self)

    def resume_websession(self):
        self.calls.append("resume")
        return self.resume

    def initiate_weblogin(self):
        self.calls.append("initiate")
        if self.reject:
            r = requests.Response()
            r.status_code, r.url = 401, "https://api.traderepublic.com/api/v2/auth/web/login"
            raise requests.HTTPError(response=r)
        return 120

    @property
    def weblogin_needs_authenticator(self):
        return self.authenticator

    def complete_weblogin(self, code=None):
        self.calls.append(("complete", code))
        if not self.confirm:
            raise TimeoutError

    async def close(self):
        pass


@pytest.fixture
def api(monkeypatch):
    FakeApi.instances = []
    opts = {}
    monkeypatch.setattr(TradeRepublicAdapter, "api_factory",
                        staticmethod(lambda phone, pin, cookies: FakeApi(phone, pin, cookies, **opts)))
    return opts


def test_web_login_confirmed_in_the_app_keeps_the_cookies(api):
    a = TradeRepublicAdapter(CREDS)
    with pytest.raises(AuthRequired) as e:
        a.login()
    assert e.value.challenge.kind == "confirm"
    assert FakeApi.instances[-1].phone == "+491700000000"
    a.complete_login("")
    a.close()
    assert "tr_session" in a.session_state()["cookies"]
    # The next run resumes the session with those cookies - no confirmation needed.
    api["resume"] = True
    b = TradeRepublicAdapter(CREDS, a.session_state())
    b.login()
    assert "tr_session" in FakeApi.instances[-1].seen_cookies
    assert FakeApi.instances[-1].calls == ["resume"]
    b.close()


def test_scheduled_run_asks_on_the_phone_and_waits(api):
    told = []
    a = TradeRepublicAdapter(CREDS, {"cookies": "expired"})
    a.on_user_action = told.append
    a.login()
    assert told and "Trade-Republic-App" in told[0]
    assert FakeApi.instances[-1].calls == ["resume", "initiate", ("complete", None)]


def test_not_confirmed_in_time(api):
    api["confirm"] = False
    a = TradeRepublicAdapter(CREDS)
    a.on_user_action = lambda m: None
    with pytest.raises(AuthRequired, match="nicht rechtzeitig"):
        a.login()


def test_authenticator_code(api):
    api["authenticator"] = True
    a = TradeRepublicAdapter(CREDS)
    with pytest.raises(AuthRequired) as e:
        a.login()
    assert e.value.challenge.kind == "code"
    a.complete_login("123456")
    assert ("complete", "123456") in FakeApi.instances[-1].calls


def test_rejected_pin_is_never_tried_again(api):
    api["reject"] = True
    a = TradeRepublicAdapter(CREDS)
    with pytest.raises(AdapterError, match="abgelehnt"):
        a.login()
    count = len(FakeApi.instances)
    with pytest.raises(AdapterError, match="nicht noch einmal"):
        TradeRepublicAdapter(CREDS, a.session_state()).login()
    assert len(FakeApi.instances) == count


# ── sync and holdings check ─────────────────────────────────────────────────
class ReplayTR(TradeRepublicAdapter):
    def __init__(self, credentials, session=None):
        super().__init__(credentials, session)
        self._replay = RECORDING

    def login(self):
        pass


def setup_sync(tmp_path):
    wf = FakeWealthfolio()
    cfg = config_mod.Config(wealthfolio_url="http://wf", public_url="http://sync:8090")
    cfg.brokers["tr"] = config_mod.BrokerConfig(enabled=True, cash_account_id="acc-cash",
                                                portfolio_account_id="acc-depot", start_date="2026-08-01")
    config_mod.save(tmp_path, cfg)
    v = Vault(tmp_path)
    v.update(lambda d: d.__setitem__("wealthfolio_password", PASSWORD))
    v.set_broker_credentials("tr", CREDS)
    notifier = RecordingNotifier()
    syncer = Syncer(tmp_path, wealthfolio=lambda u, p: WealthfolioClient(u, p, transport=wf.transport()),
                    notifier=notifier, adapters={"tr": ReplayTR})
    return syncer, wf, notifier


def test_sync_books_the_timeline_and_reports_lasting_deviations(tmp_path):
    syncer, wf, notifier = setup_sync(tmp_path)
    [r] = syncer.run()
    assert (r.status, r.created, r.unknown) == ("ok", 12, 1)
    assert syncer.state.balances("tr") == [{"currency": "EUR", "amount": "1234.56",
                                             "at": syncer.state.balances("tr")[0]["at"]}]
    devs = syncer.state.reconcile("tr")["deviations"]
    # The fabricated recording doesn't add up (sold shares never bought etc.): deviations,
    # but not reported after one run.
    assert {d["key"] for d in devs} >= {"EUR", "US88032Q1094"}
    assert not any("Bestand weicht ab" in t for t, _, _ in notifier.sent)
    syncer.run()
    assert sum("Bestand weicht ab" in t for t, _, _ in notifier.sent) == 1
    syncer.run()  # same deviations: not reported again
    assert sum("Bestand weicht ab" in t for t, _, _ in notifier.sent) == 1
    # NVIDIA: bought 0.685102 and Trade Republic says 0.685102 - no deviation.
    assert "US67066G1040" not in {d["key"] for d in syncer.state.reconcile("tr")["deviations"]}


def test_compare_matches_mapped_symbols_and_missing_positions():
    wf_cash = [{"holdingType": "cash", "localCurrency": "EUR", "quantity": "100.004"}]
    wf_depot = [{"holdingType": "security", "quantity": "2", "instrument": {"symbol": "VWCE", "name": "All-World"}},
                {"holdingType": "security", "quantity": "1", "instrument": {"symbol": "OLD", "name": "Sold"}}]
    devs = compare([CashBalance("EUR", D("100"))], [Position("IE00BK5BQT80", "All-World", D("2"), "EUR")],
                   wf_cash, wf_depot, {"IE00BK5BQT80": "VWCE"})
    assert [(d.kind, d.key, d.broker, d.wealthfolio) for d in devs] == [("position", "OLD", "0", "1")]
    assert compare([CashBalance("EUR", D("100"))], None, wf_cash, wf_depot, {}) == []


def test_dashboard_shows_the_holdings_check(tmp_path):
    import re

    from fastapi.testclient import TestClient

    from brokersync.web.app import create_app

    syncer, wf, _ = setup_sync(tmp_path)
    syncer.run()
    app = create_app(tmp_path, wealthfolio=lambda u, p: WealthfolioClient(u, p, transport=wf.transport()),
                     adapters={"tr": ReplayTR}, notifier=RecordingNotifier(), run_in_thread=False)
    client = TestClient(app)
    client.post("/setup-password", data={"password": "geheim123", "password2": "geheim123"})
    page = client.get("/").text
    assert "Abgleich mit Wealthfolio" in page and "weicht ab" in page
    assert re.search(r"<td>Cash EUR</td><td>1234.56</td>", page)


def test_positions_in_the_current_and_the_older_format():
    from brokersync.adapters.tr import portfolio_positions

    assert [p.isin for p in TradeRepublicAdapter.replay(RECORDING).get_positions()] == ["US67066G1040"]
    new = {"categories": [{"positions": [{"isin": "A"}]}, {"positions": [{"isin": "B"}]}]}
    assert [p["isin"] for p in portfolio_positions(new)] == ["A", "B"]
    assert portfolio_positions({"positions": [{"instrumentId": "C"}]}) == [{"instrumentId": "C"}]
    assert portfolio_positions(None) == []


# ── CSV-imported securities under a mapped ticker ───────────────────────────
def add_csv_buy(wf, *, date="2026-09-02T09:00:00.000Z", symbol="NVDA"):
    """What the addon's CSV import booked for the NVIDIA buy, with the ticker the user mapped."""
    leg = dict(quantity="1", unitPrice="1", amount="111", currency="EUR", assetSymbol="$CASH-EUR")
    wf.add_existing(accountId="acc-cash", activityType="TRANSFER_OUT", date=date, comment="Funds for buy", **leg)
    wf.add_existing(accountId="acc-depot", activityType="TRANSFER_IN", date=date, comment="Funds from Cash", **leg)
    return wf.add_existing(accountId="acc-depot", activityType="BUY", date=date, quantity="0.685102",
                           unitPrice="160.56", amount="111", fee="1", currency="EUR", comment="NVIDIA - Buy",
                           assetSymbol=symbol, assetId=symbol)


def test_a_csv_buy_under_a_ticker_is_recognised(tmp_path):
    syncer, wf, _ = setup_sync(tmp_path)
    add_csv_buy(wf)
    syncer.run()
    buys = [a for a in wf.activities if a["activityType"] == "BUY" and "0.685102" == a["quantity"]]
    assert [b["assetSymbol"] for b in buys] == ["NVDA"]  # not booked a second time under the ISIN


def test_dividend_with_another_symbol_matches_only_when_unambiguous():
    from brokersync.dedup import ExistingIndex

    payload = {"accountId": "p", "activityType": "DIVIDEND", "activityDate": "2026-09-05T10:00:00.000Z",
               "amount": "2.24", "asset": {"symbol": "US20030N1019"}}
    one = {"id": "x", "accountId": "p", "activityType": "DIVIDEND", "date": "2026-09-05T08:00:00Z", "amount": "2.24",
           "assetSymbol": "CMCSA"}
    assert ExistingIndex([one]).find("[SYNC tr:1]", [payload]) == "x"
    two = [one, {**one, "id": "y", "assetSymbol": "OTHER"}]
    assert ExistingIndex(two).find("[SYNC tr:1]", [payload]) is None


def test_duplicates_from_before_the_fix_are_found_and_only_the_sync_copy_removed(tmp_path):
    from fastapi.testclient import TestClient

    from brokersync.web.app import create_app

    syncer, wf, _ = setup_sync(tmp_path)
    csv_buy = add_csv_buy(wf)
    # What 0.3.1 booked on top: the same buy under the ISIN, with its transfer legs.
    ref = " [SYNC tr:00000000-0000-4000-8000-000000000002]"
    for acc, t in (("acc-cash", "TRANSFER_OUT"), ("acc-depot", "TRANSFER_IN")):
        wf.add_existing(accountId=acc, activityType=t, date="2026-09-02T09:00:10.000Z", quantity="1", unitPrice="1",
                        amount="111", currency="EUR", assetSymbol="$CASH-EUR", comment=f"Funds{ref}",
                        sourceGroupId="sync-tr-00000000-0000-4000-8000-000000000002")
    sync_buy = wf.add_existing(accountId="acc-depot", activityType="BUY", date="2026-09-02T09:00:12.000Z",
                               quantity="0.685102", unitPrice="160.560033", amount="111", fee="1", currency="EUR",
                               comment=f"Kauforder NVIDIA{ref}", assetSymbol="US67066G1040",
                               assetId="US67066G1040")
    syncer.state.mark("tr", "00000000-0000-4000-8000-000000000002", "imported", [sync_buy["id"]])

    app = create_app(tmp_path, wealthfolio=lambda u, p: WealthfolioClient(u, p, transport=wf.transport()),
                     adapters={"tr": ReplayTR}, notifier=RecordingNotifier(), run_in_thread=False)
    client = TestClient(app)
    client.post("/setup-password", data={"password": "geheim123", "password2": "geheim123"})
    page = client.get("/duplicates").text
    assert "Trade Republic: 1 doppelt" in page and "NVDA" in page and "US67066G1040" in page
    import re

    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    r = client.post("/duplicates", data={"csrf": csrf, "broker": "tr"})
    assert "bestätige" in r.text and wf.deleted == []
    r = client.post("/duplicates", data={"csrf": csrf, "broker": "tr", "confirm": "1"})
    assert "1 doppelte Vorgänge entfernt (3 Buchungen)" in r.text
    assert len(wf.deleted) == 3 and sync_buy["id"] == wf.deleted[-1]
    assert any(a["id"] == csv_buy["id"] for a in wf.activities)
    assert "Trade Republic: 0 doppelt" in client.get("/duplicates").text
    # The removed transaction is "existing" now: the next sync doesn't create it again.
    syncer.run()
    assert [a["assetSymbol"] for a in wf.activities if a["activityType"] == "BUY" and a["quantity"] == "0.685102"] \
        == ["NVDA"]
