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


def test_web_login_with_app_confirmation_and_transfer_patterns(tmp_path, fake):
    import re

    from fastapi.testclient import TestClient

    from brokersync.web.app import create_app

    wf = FakeWealthfolio()
    app = create_app(tmp_path, wealthfolio=lambda u, p: WealthfolioClient(u, p, transport=wf.transport()),
                     adapters={"dkb": DkbAdapter}, notifier=RecordingNotifier(), run_in_thread=False)
    client = TestClient(app)
    client.post("/setup-password", data={"password": "geheim-123456", "password2": "geheim-123456"})
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
    assert (p.isin, str(p.shares), str(p.value)) == ("IE00B4L5Y983", "5.5", "550.0")


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
