"""DKB adapter against a fake python-fints client: TAN in the app, PIN safety,
classification, and the sync rules for securities settlements and transfers."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from brokersync import config as config_mod
from brokersync.adapters import fints as fints_mod
from brokersync.adapters.base import AdapterError, AuthRequired
from brokersync.adapters.dkb import DkbAdapter
from brokersync.mapping import Accounts, TransferPattern, to_activities
from brokersync.model import Kind
from brokersync.sync import UNMATCHED_SECURITIES, Syncer
from brokersync.vault import Vault
from brokersync.wealthfolio import WealthfolioClient

from .fake_fints import FakeFinTS, FinTSClientTemporaryAuthError, NeedTANResponse
from .fakes import PASSWORD, FakeWealthfolio
from .helpers import first_password
from .test_sync import RecordingNotifier

RECORDING = json.loads((Path(__file__).parent / "contract" / "dkb" / "giro.json").read_text())["recording"]
CREDS = {"username": "max", "pin": "1234", "product_id": "TESTPRODUCT", "iban": ""}


@pytest.fixture
def fake(monkeypatch):
    FakeFinTS.instances = []
    opts = {}

    def factory(blz, user, pin, server, product_id, data):
        assert (blz, server, product_id) == ("12030000", "https://fints.dkb.de/fints", "TESTPRODUCT")
        return FakeFinTS(RECORDING, **opts)

    monkeypatch.setattr(DkbAdapter, "client_factory", staticmethod(factory))
    monkeypatch.setattr(fints_mod, "DECOUPLED_POLL", 0)
    return opts


def test_login_waits_for_the_app_confirmation_in_the_web_ui(fake):
    a = DkbAdapter(CREDS)
    with pytest.raises(AuthRequired) as e:
        a.login()
    assert e.value.challenge.kind == "confirm"
    fake_client = FakeFinTS.instances[-1]
    fake_client.confirm_after = 2
    with pytest.raises(AuthRequired) as again:
        a.complete_login("")  # clicked "confirmed" before confirming in the app
    assert again.value.challenge.message.startswith("Noch nicht bestätigt")
    a.complete_login("")
    a.close()
    assert a.session_state()["fints"]  # python-fints state kept for the next run


def test_scheduled_run_notifies_and_waits_for_the_app(fake):
    fake["confirm_after"] = 3
    a = DkbAdapter(CREDS)
    told = []
    a.on_user_action = told.append
    a.login()
    assert told == ["Bitte bestätige den Zugriff des Wealthfolio Broker Sync in der DKB-App."]
    assert FakeFinTS.instances[-1].polls == 3


def test_a_rejected_pin_is_never_tried_again(fake):
    fake["pin_ok"] = False
    a = DkbAdapter(CREDS)
    with pytest.raises(AdapterError, match="abgelehnt"):
        a.login()
    session = a.session_state()
    assert session["pin_rejected"] is True
    count = len(FakeFinTS.instances)
    with pytest.raises(AdapterError, match="nicht noch einmal"):
        DkbAdapter(CREDS, session).login()
    assert len(FakeFinTS.instances) == count  # the bank wasn't contacted


def test_missing_product_id_is_explained():
    with pytest.raises(AdapterError, match="Produkt-ID"):
        DkbAdapter({**CREDS, "product_id": ""}).login()


def test_fetches_without_tan_from_89_days_back_by_default(fake):
    fake["sca"] = False
    a = DkbAdapter(CREDS)
    a.login()
    a.get_transactions(None)
    (_, start), = [c for c in FakeFinTS.instances[-1].calls if c != "enter"]
    assert (datetime.now(fints_mod.BERLIN).date() - start).days == 89


def test_iban_selects_the_account(fake):
    fake["sca"] = False
    a = DkbAdapter({**CREDS, "iban": "DE99 9999 9999 9999 9999 99"})
    a.login()
    with pytest.raises(AdapterError, match="gehört nicht"):
        a.get_transactions(None)


def test_outbound_transfer_to_an_own_account_is_an_internal_transfer():
    txs = DkbAdapter.replay(RECORDING).get_transactions(datetime(2026, 8, 1, tzinfo=UTC))
    savings = next(t for t in txs if t.counterparty_iban == "DE75512108001245126199")
    pattern = TransferPattern("Trade Republic", iban="DE75512108001245126199", destination_account_id="tr-cash")
    acts = to_activities(savings, "dkb", Accounts("dkb-cash", "dkb-depot"), patterns=[pattern])
    assert [(a["accountId"], a["activityType"], a["amount"]) for a in acts] == [
        ("dkb-cash", "TRANSFER_OUT", "500"), ("tr-cash", "TRANSFER_IN", "500")]
    assert acts[0]["sourceGroupId"] == acts[1]["sourceGroupId"]
    assert acts[0]["comment"].startswith("-> Trade Republic: Sparrate September (Erika Mustermann)")
    # Without a pattern: spending.
    plain = to_activities(savings, "dkb", Accounts("dkb-cash", "dkb-depot"))
    assert [a["activityType"] for a in plain] == ["WITHDRAWAL"]
    # Inbound money never checks the patterns.
    salary = next(t for t in txs if t.kind == Kind.DEPOSIT)
    pattern_in = TransferPattern("Arbeitgeber", iban=salary.counterparty_iban, destination_account_id="x")
    assert [a["activityType"] for a in to_activities(salary, "dkb", Accounts("c", "p"), patterns=[pattern_in])] \
        == ["DEPOSIT"]


def setup_sync(tmp_path, wf, monkeypatch):
    monkeypatch.setattr(DkbAdapter, "client_factory",
                        staticmethod(lambda *a: FakeFinTS(RECORDING, sca=False)))
    cfg = config_mod.Config(wealthfolio_url="http://wf", public_url="http://sync:8090")
    cfg.brokers["dkb"] = config_mod.BrokerConfig(enabled=True, cash_account_id="acc-cash",
                                                 portfolio_account_id="acc-depot", start_date="2026-08-01")
    cfg.transfer_patterns = [{"label": "Trade Republic", "iban": "DE75512108001245126199",
                              "destinationAccountId": "acc-old"}]
    config_mod.save(tmp_path, cfg)
    v = Vault(tmp_path)
    v.update(lambda d: d.__setitem__("wealthfolio_password", PASSWORD))
    v.set_broker_credentials("dkb", CREDS)
    notifier = RecordingNotifier()
    syncer = Syncer(tmp_path, wealthfolio=lambda u, p: WealthfolioClient(u, p, transport=wf.transport()),
                    notifier=notifier, adapters={"dkb": DkbAdapter})
    return syncer, notifier


def test_sync_books_the_giro_and_checks_securities_against_the_pdf_import(tmp_path, monkeypatch):
    wf = FakeWealthfolio()
    syncer, notifier = setup_sync(tmp_path, wf, monkeypatch)
    [r] = syncer.run()
    # 7 bookings: salary, 3 card payments, transfer (2 legs), interest, fee; the depot debit is missing.
    assert (r.status, r.created, r.unknown) == ("ok", 7, 1)
    types = sorted(a["activityType"] for a in wf.activities)
    assert types == ["DEPOSIT", "FEE", "INTEREST", "TRANSFER_IN", "TRANSFER_OUT", "WITHDRAWAL", "WITHDRAWAL",
                     "WITHDRAWAL"]
    assert any("Wertpapier-Buchung ohne Gegenstück" in t for t, _, _ in notifier.sent)
    assert [e["raw_type"] for e in syncer.state.unknown_events()] == [UNMATCHED_SECURITIES]
    assert syncer.state.balances("dkb")[0]["amount"] == "1950.06"

    # The user imports the DKB PDF statement with the addon: its buy is funded by a
    # TRANSFER_OUT from the DKB cash account two days before the giro debit.
    wf.add_existing(accountId="acc-cash", activityType="TRANSFER_OUT", date="2026-09-08T09:05:58.000Z",
                    quantity="1", unitPrice="1", amount="1001", currency="EUR",
                    comment="Funds for IE00TEST0001 (TEST Fonds World) buy -> Portfolio [PDF 123]",
                    assetSymbol="", assetId="")
    [r2] = syncer.run()
    assert (r2.created, r2.existing, r2.unknown) == (0, 1, 0)
    assert syncer.state.unknown_events() == []
    [r3] = syncer.run()
    assert (r3.created, r3.existing) == (0, 0)


def test_web_login_with_app_confirmation_and_transfer_patterns(tmp_path, fake, hidden_pages):
    import re

    from fastapi.testclient import TestClient

    from brokersync.web.app import create_app

    wf = FakeWealthfolio()
    app = create_app(tmp_path, wealthfolio=lambda u, p: WealthfolioClient(u, p, transport=wf.transport()),
                     adapters={"dkb": DkbAdapter}, notifier=RecordingNotifier(), run_in_thread=False)
    client = TestClient(app)
    first_password(client)
    csrf = re.search(r'name="csrf" value="([^"]+)"', client.get("/").text).group(1)
    client.post("/setup/wealthfolio", data={"csrf": csrf, "url": "http://wf", "password": PASSWORD})
    page = client.get("/brokers/dkb").text
    assert "FinTS-Produkt-ID" in page and "IBAN des Girokontos" in page
    r = client.post("/brokers/dkb", data={"csrf": csrf, "cred_username": "max", "cred_pin": "1234",
                                          "cred_product_id": "TESTPRODUCT", "cred_iban": "", "enabled": "on",
                                          "cash_account_id": "acc-cash", "portfolio_account_id": "acc-depot"})
    assert "Ich habe in der App bestätigt" in r.text
    assert "1234" not in r.text
    r = client.post("/brokers/dkb/login", data={"csrf": csrf, "code": ""})
    assert "angemeldet" in r.text
    assert Vault(tmp_path).broker("dkb")["session"]["fints"]

    r = client.post("/transfers", data={"csrf": csrf, "label": "Trade Republic", "iban": "de75 5121 0800 1245 1261 99",
                                        "keyword": "", "destination": "acc-old"})
    assert "Gespeichert" in r.text and "DE75512108001245126199" in r.text
    assert config_mod.load(tmp_path).transfer_patterns[0]["destinationAccountId"] == "acc-old"
    client.post("/transfers", data={"csrf": csrf, "delete": "0"})
    assert config_mod.load(tmp_path).transfer_patterns == []


# ── error paths ─────────────────────────────────────────────────────────────
def test_a_temporarily_locked_access_is_never_tried_again(fake, monkeypatch):
    fake["sca"] = False

    def locked(self):
        raise FinTSClientTemporaryAuthError("locked")

    monkeypatch.setattr(FakeFinTS, "__enter__", locked)
    a = DkbAdapter(CREDS)
    with pytest.raises(AdapterError, match="vorübergehend gesperrt"):
        a.login()
    assert a.session_state()["pin_rejected"] is True


def test_other_bank_errors_are_adapter_errors_and_keep_the_pin(fake, monkeypatch):
    fake["sca"] = False

    def down(self):
        raise ConnectionError("server unreachable")

    monkeypatch.setattr(FakeFinTS, "__enter__", down)
    a = DkbAdapter(CREDS)
    with pytest.raises(AdapterError, match="DKB \\(FinTS\\): server unreachable"):
        a.login()
    assert not a.session_state().get("pin_rejected")


def test_no_account_for_the_login_is_explained(fake, monkeypatch):
    fake["sca"] = False
    monkeypatch.setattr(FakeFinTS, "get_sepa_accounts", lambda self: [])
    a = DkbAdapter(CREDS)
    a.login()
    with pytest.raises(AdapterError, match="kein Konto"):
        a.get_cash()


def test_a_tan_for_older_bookings_is_asked_in_the_web_ui(fake, monkeypatch):
    # The dialog starts without SCA, but the bank wants a TAN (here: a code) for the fetch.
    fake["sca"] = False
    asked = []

    def wants_tan(self, account, start_date=None, end_date=None):
        asked.append(start_date)
        return NeedTANResponse(decoupled=False, challenge="TAN aus der App eingeben")

    monkeypatch.setattr(FakeFinTS, "get_transactions", wants_tan)
    a = DkbAdapter(CREDS)
    a.login()
    with pytest.raises(AuthRequired) as e:
        a.get_transactions(datetime(2026, 1, 1, tzinfo=UTC))
    assert (e.value.challenge.kind, e.value.challenge.message) == ("code", "TAN aus der App eingeben")


def test_a_scheduled_run_gives_up_when_the_app_is_not_confirmed(fake, monkeypatch):
    fake["confirm_after"] = 1000
    monkeypatch.setattr(fints_mod, "DECOUPLED_WAIT", 0.05)
    a = DkbAdapter(CREDS)
    a.on_user_action = lambda message: None
    with pytest.raises(AuthRequired) as e:
        a.login()
    assert e.value.challenge.kind == "confirm"


def test_positions_skip_accounts_without_a_depot(fake, monkeypatch):
    fake["sca"] = False
    holding = type("Holding", (), {"ISIN": "IE00B4L5Y983", "name": "Testfonds", "pieces": 5.5,
                                    "value_symbol": "EUR", "total_value": 550.0})()

    def accounts(self):
        return [fints_mod._Account("DE02120300000000202051"), fints_mod._Account("DE02120300000000999999")]

    def holdings(self, account):
        if account.iban.endswith("202051"):
            raise ValueError("not a depot")
        return [holding]

    monkeypatch.setattr(FakeFinTS, "get_sepa_accounts", accounts)
    monkeypatch.setattr(FakeFinTS, "get_holdings", holdings)
    a = DkbAdapter(CREDS)
    a.login()
    [p] = a.get_positions()
    assert (p.isin, str(p.shares), p.currency) == ("IE00B4L5Y983", "5.5", "EUR")


def test_close_survives_a_broken_connection(fake, monkeypatch):
    fake["sca"] = False

    def broken(self, *args, **kw):
        raise ConnectionError("gone")

    a = DkbAdapter(CREDS)
    a.login()
    monkeypatch.setattr(FakeFinTS, "__exit__", broken)
    monkeypatch.setattr(FakeFinTS, "deconstruct", broken)
    a.close()  # no exception: the run's result counts more than a clean goodbye
    DkbAdapter(CREDS).close()  # never logged in


def imported(wf, day, kind, amount, gid):
    """A PDF import's trade (as the import books it): the trade on the depot and its transfer pair."""
    when = f"{day}T09:00:00.000Z"
    cash_leg = "TRANSFER_OUT" if kind == "BUY" else "TRANSFER_IN"
    depot_leg = "TRANSFER_IN" if kind == "BUY" else "TRANSFER_OUT"
    for account, activity, extra in (("acc-cash", cash_leg, {}), ("acc-depot", depot_leg, {}),
                                     ("acc-depot", kind, {"quantity": "10", "assetSymbol": "IE00TEST0001"})):
        wf.add_existing(**{"accountId": account, "activityType": activity, "date": when, "quantity": "1",
                           "unitPrice": "1", "amount": amount, "currency": "EUR", "sourceGroupId": gid,
                           "comment": f"{kind} TEST Fonds [PDF {gid}]", "assetSymbol": "", "assetId": "", **extra})


def test_imported_trades_before_the_giro_history_get_their_money(tmp_path, monkeypatch):
    wf = FakeWealthfolio()
    imported(wf, "2026-06-15", "BUY", "1001.90", "g1")  # before the giro history (start date 2026-08-01)
    imported(wf, "2026-06-20", "SELL", "500", "g2")
    wf.add_existing(accountId="acc-depot", activityType="DIVIDEND", date="2026-06-25T09:00:00.000Z",
                    quantity="10", unitPrice="1", amount="12.34", currency="EUR", comment="Dividende [PDF g3]",
                    assetSymbol="IE00TEST0001", assetId="")
    imported(wf, "2026-09-06", "BUY", "1001", "g4")  # in the history: its giro debit is read and matched
    syncer, _ = setup_sync(tmp_path, wf, monkeypatch)
    [r] = syncer.run()
    assert r.status == "ok"
    assert syncer.state.meta("history-start:dkb") == "2026-08-01"
    ours = [a for a in wf.activities if "[SYNC dkb:pre-" in (a.get("comment") or "")]
    assert sorted((a["activityType"], a["amount"], a["date"][:10]) for a in ours) == \
        [("DEPOSIT", "1001.9", "2026-06-15"), ("WITHDRAWAL", "500", "2026-06-20")]
    deposit = next(a for a in ours if a["activityType"] == "DEPOSIT")
    assert deposit["accountId"] == "acc-cash" and deposit["date"] < "2026-06-15T09:00:00"
    # The buy in the history was matched to its giro debit: no deposit, and nothing new next time.
    [r2] = syncer.run()
    assert (r2.created, r2.failed) == (0, 0)
    assert len([a for a in wf.activities if "[SYNC dkb:pre-" in (a.get("comment") or "")]) == 2


def test_without_a_recorded_history_start_the_first_run_tells(tmp_path, monkeypatch):
    from datetime import timedelta

    from brokersync.sync import BERLIN

    wf = FakeWealthfolio()
    syncer, _ = setup_sync(tmp_path, wf, monkeypatch)
    cfg = config_mod.load(tmp_path)
    cfg.brokers["dkb"].start_date = ""
    config_mod.save(tmp_path, cfg)
    run_id = syncer.state.start_run("dkb")
    syncer.state.finish_run(run_id, "ok")
    [r] = syncer.run()
    first = syncer.state.first_success("dkb").astimezone(BERLIN).date()
    assert syncer.state.meta("history-start:dkb") == (first - timedelta(days=89)).isoformat()


def test_positions_bought_only_by_an_import_are_found_by_the_isin_in_its_transfer(tmp_path, monkeypatch):
    from .test_fints import GiroAndDepot

    isin = "IE00TEST0000"  # fabricated, with a valid check digit

    class Depot(GiroAndDepot):
        def get_holdings(self, account):
            if account.accountnumber != "0000999888":
                raise ValueError("no depot")
            return [type("Holding", (), {"ISIN": isin, "name": "TEST FONDS", "pieces": 10.0,
                                         "value_symbol": "EUR", "total_value": 1000.0})()]

    wf = FakeWealthfolio()
    # The import booked the buy under the user's ticker; only its transfer names the ISIN.
    common = {"date": "2026-06-15T09:00:00.000Z", "quantity": "1", "unitPrice": "1", "amount": "1001.90",
              "currency": "EUR", "sourceGroupId": "pdf-dkb-1", "assetSymbol": "", "assetId": ""}
    wf.add_existing(**{**common, "accountId": "acc-cash", "activityType": "TRANSFER_OUT",
                       "date": "2026-06-15T08:59:58Z",
                       "comment": f"Funds for {isin} (TEST Fonds) buy -> Portfolio [PDF 1]"})
    wf.add_existing(**{**common, "accountId": "acc-depot", "activityType": "TRANSFER_IN",
                       "date": "2026-06-15T08:59:59Z", "comment": f"Funds from Cash for {isin} buy [PDF 1]"})
    wf.add_existing(**{**common, "accountId": "acc-depot", "activityType": "BUY", "quantity": "10", "unitPrice": "100",
                       "assetSymbol": "TFW", "assetId": "TFW", "comment": "Kauf TEST Fonds [PDF 1]"})
    syncer, _ = setup_sync(tmp_path, wf, monkeypatch)
    monkeypatch.setattr(DkbAdapter, "client_factory", staticmethod(lambda *a: Depot(RECORDING, sca=False)))
    monkeypatch.setattr(DkbAdapter, "reports_positions", True)
    syncer.recalc_wait = 0
    [r] = syncer.run()
    assert r.status == "ok"
    assert syncer.state.assets("dkb")[isin]["asset_id"] == "TFW"
    deviations = syncer.state.reconcile("dkb")["deviations"]
    assert not [d for d in deviations if d["kind"] == "position"], deviations


def test_an_isin_needs_a_valid_check_digit():
    from brokersync.assets import valid_isin

    assert valid_isin("US0378331005") and valid_isin("IE00TEST0000")
    assert not valid_isin("US0378331006") and not valid_isin("IE00TEST0001")


def test_a_savings_plan_executed_in_the_same_second_is_told_apart_by_amount(tmp_path):
    from brokersync import assets
    from brokersync.mapping import Accounts
    from brokersync.state import State

    wf = FakeWealthfolio()
    when = "2026-06-02T07:00:00.000Z"
    for isin, ticker, amount in (("IE00TEST0000", "AAA", "50"), ("US00TEST0008", "BBB", "25")):
        base = {"date": when, "quantity": "1", "unitPrice": "1", "amount": amount, "currency": "EUR",
                "assetSymbol": "", "assetId": ""}
        wf.add_existing(**{**base, "accountId": "acc-cash", "activityType": "TRANSFER_OUT",
                           "comment": f"Funds for {isin} (TEST) buy -> Portfolio [PDF {ticker}]"})
        wf.add_existing(**{**base, "accountId": "acc-depot", "activityType": "BUY", "quantity": "2",
                           "assetSymbol": ticker, "assetId": ticker, "comment": f"Sparplan TEST [PDF {ticker}]"})
    state = State(tmp_path)
    client = WealthfolioClient("http://wf", PASSWORD, transport=wf.transport())
    learned = assets.learn_from_imports(state, "dkb", client, Accounts("acc-cash", "acc-depot"),
                                        {"IE00TEST0000", "US00TEST0008"})
    assert learned == 2
    assert {i: a["asset_id"] for i, a in state.assets("dkb").items()} == {"IE00TEST0000": "AAA", "US00TEST0008": "BBB"}
    state.close()


def test_the_statement_reference_ties_a_trade_to_its_transfer_and_corrects_a_wrong_asset(tmp_path):
    from brokersync import assets
    from brokersync.mapping import Accounts
    from brokersync.state import State

    wf = FakeWealthfolio()
    when = "2026-06-02T07:00:00.000Z"
    # A savings plan: two buys in the same second with the same amount - only the statement tells them apart.
    for isin, ticker in (("IE00TEST0000", "AAA"), ("US00TEST0008", "BBB")):
        base = {"date": when, "quantity": "1", "unitPrice": "1", "amount": "50", "currency": "EUR",
                "assetSymbol": "", "assetId": ""}
        wf.add_existing(**{**base, "accountId": "acc-cash", "activityType": "TRANSFER_OUT",
                           "comment": f"Funds for {isin} (TEST) buy -> Portfolio [PDF {ticker}-1]"})
        wf.add_existing(**{**base, "accountId": "acc-depot", "activityType": "BUY", "quantity": "2",
                           "assetSymbol": ticker, "assetId": ticker, "comment": f"Sparplan TEST [PDF {ticker}-1]"})
    state = State(tmp_path)
    # Learned wrongly by an earlier version: both ISINs on the same asset.
    state.set_asset("dkb", "IE00TEST0000", asset_id="BBB", symbol="BBB", exchange_mic=None, name=None)
    state.set_asset("dkb", "US00TEST0008", asset_id="BBB", symbol="BBB", exchange_mic=None, name=None)
    client = WealthfolioClient("http://wf", PASSWORD, transport=wf.transport())
    learned = assets.learn_from_imports(state, "dkb", client, Accounts("acc-cash", "acc-depot"),
                                        {"IE00TEST0000", "US00TEST0008"})
    assert learned == 1  # the wrong one corrected, the right one kept
    assert {i: a["asset_id"] for i, a in state.assets("dkb").items()} == {"IE00TEST0000": "AAA", "US00TEST0008": "BBB"}
    state.close()


def test_a_deviating_position_relearns_its_asset_from_the_syncs_own_opening(tmp_path):
    from brokersync import assets
    from brokersync.mapping import Accounts
    from brokersync.state import State

    isin = "IE00TEST0000"
    wf = FakeWealthfolio()
    ref = f"[SYNC deutschebank:start-{isin}-2024-01-02]"
    base = {"date": "2024-01-02T11:00:00.000Z", "quantity": "1", "unitPrice": "1", "amount": "440",
            "currency": "EUR", "assetSymbol": "", "assetId": ""}
    wf.add_existing(**{**base, "accountId": "acc-cash", "activityType": "TRANSFER_OUT",
                       "date": "2024-01-02T10:59:58.000Z",
                       "comment": f"Funds for {isin} (TEST AG) buy -> Portfolio{ref}"})
    wf.add_existing(**{**base, "accountId": "acc-depot", "activityType": "BUY", "quantity": "22", "unitPrice": "20",
                       "assetSymbol": "TAG", "assetId": "asset-new", "comment": f"Anfangsbestand TEST AG{ref}"})
    state = State(tmp_path)
    state.set_asset("deutschebank", isin, asset_id="asset-old", symbol="OLD", exchange_mic=None, name=None)
    client = WealthfolioClient("http://wf", PASSWORD, transport=wf.transport())
    accounts = Accounts("acc-cash", "acc-depot")
    # Not deviating: the stored asset stays (it came from the sync's own activities, which win).
    assert assets.learn_from_imports(state, "deutschebank", client, accounts, {isin}) == 0
    # Deviating: time and amount of its own opening say which asset it is.
    assert assets.learn_from_imports(state, "deutschebank", client, accounts, {isin}, recheck={isin}) == 1
    assert state.assets("deutschebank")[isin]["asset_id"] == "asset-new"
    state.close()


def test_the_log_says_why_an_asset_isnt_learned(tmp_path, caplog):
    import logging

    from brokersync import assets
    from brokersync.mapping import Accounts
    from brokersync.state import State

    wf = FakeWealthfolio()
    base = {"date": "2026-06-02T07:00:00.000Z", "quantity": "1", "unitPrice": "1", "amount": "50",
            "currency": "EUR", "assetSymbol": "", "assetId": ""}
    wf.add_existing(**{**base, "accountId": "acc-cash", "activityType": "TRANSFER_OUT",
                       "comment": "Funds for IE00TEST0000 (TEST) buy -> Portfolio"})
    wf.add_existing(**{**base, "accountId": "acc-depot", "activityType": "BUY", "assetSymbol": "AAA",
                       "assetId": "AAA", "comment": "Kauf TEST"})
    state = State(tmp_path)
    state.set_asset("dkb", "DE000TEST009", asset_id="AAA", symbol="AAA", exchange_mic=None, name=None)
    client = WealthfolioClient("http://wf", PASSWORD, transport=wf.transport())
    caplog.set_level(logging.INFO, logger="brokersync.assets")
    assert assets.learn_from_imports(state, "dkb", client, Accounts("acc-cash", "acc-depot"),
                                     {"IE00TEST0000", "US00TEST0008"}) == 0
    assert "zu IE00TEST0000 nicht zugeordnet: Käufe/Verkäufe auf AAA; schon vergeben: AAA (gehört zu DE000TEST009)" \
        in caplog.text
    assert "zu US00TEST0008 nicht zugeordnet: kein Kauf/Verkauf, dessen Übertrag die ISIN nennt" in caplog.text
    state.close()


def test_an_opening_with_the_old_parsers_isin_keeps_its_id_but_gets_the_real_isin(tmp_path, monkeypatch):
    syncer, _ = setup_sync(tmp_path, FakeWealthfolio(), monkeypatch)
    syncer.state.add_opening("dkb", "IE00TEST0000|/DE/A0TEST", "2024-01-02", "IEN O.N.", "22", "20", "0")
    [tx] = syncer._openings("dkb")
    assert (tx.id, tx.isin) == ("start-IE00TEST0000|/DE/A0TEST-2024-01-02", "IE00TEST0000")


def test_an_asset_stored_under_the_old_parsers_isin_is_not_taken_from_the_real_isin(tmp_path):
    from brokersync import assets
    from brokersync.mapping import Accounts
    from brokersync.state import State

    isin = "IE00TEST0000"
    wf = FakeWealthfolio()
    ref = f"[SYNC deutschebank:start-{isin}|/DE/A0TEST-2024-01-02]"
    base = {"date": "2024-01-02T11:00:00.000Z", "quantity": "1", "unitPrice": "1", "amount": "440",
            "currency": "EUR", "assetSymbol": "", "assetId": ""}
    wf.add_existing(**{**base, "accountId": "acc-cash", "activityType": "TRANSFER_OUT",
                       "date": "2024-01-02T10:59:58.000Z",
                       "comment": f"Funds for {isin}|/DE/A0TEST (IEN O.N.) buy -> Portfolio{ref}"})
    wf.add_existing(**{**base, "accountId": "acc-depot", "activityType": "BUY", "quantity": "22", "unitPrice": "20",
                       "assetSymbol": f"{isin}|/DE/A0TEST", "assetId": "asset-opening",
                       "comment": f"Anfangsbestand{ref}"})
    state = State(tmp_path)
    state.set_asset("deutschebank", f"{isin}|/DE/A0TEST", asset_id="asset-opening", symbol="x", exchange_mic=None,
                    name=None)
    state.set_asset("deutschebank", isin, asset_id="asset-dividends", symbol=isin, exchange_mic=None, name=None)
    client = WealthfolioClient("http://wf", PASSWORD, transport=wf.transport())
    assert assets.learn_from_imports(state, "deutschebank", client, Accounts("acc-cash", "acc-depot"), {isin},
                                     recheck={isin}) == 1
    assert state.assets("deutschebank")[isin]["asset_id"] == "asset-opening"
    state.close()


def test_the_log_keeps_isins_but_masks_account_numbers():
    from brokersync import redact

    assert redact.redact("ISIN US0378331005, IE00BYPLS672") == "ISIN US0378331005, IE00BYPLS672"
    assert redact.redact("Depot 0503766396 Ref 000073163808330") == "Depot *** Ref ***"
