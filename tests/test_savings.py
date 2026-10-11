"""The savings account (Tagesgeld) of a FinTS bank in a Wealthfolio account of its own - fabricated data."""

import json
from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal as D
from pathlib import Path

import pytest

from brokersync import config as config_mod
from brokersync.adapters import fints as fints_mod
from brokersync.adapters.base import AdapterError
from brokersync.adapters.dkb import DkbAdapter
from brokersync.mapping import Accounts, MappingError, to_activities
from brokersync.model import SAVINGS, Kind, Transaction
from brokersync.sync import Syncer
from brokersync.vault import Vault
from brokersync.wealthfolio import WealthfolioClient

from .fake_fints import FakeFinTS
from .fakes import PASSWORD, FakeWealthfolio
from .test_sync import RecordingNotifier

GIRO_IBAN = "DE02120300000000202051"
SAVINGS_IBAN = "DE12120300000000303031"
GIRO = json.loads((Path(__file__).parent / "contract" / "dkb" / "giro.json").read_text())["recording"]
CREDS = {"username": "test", "pin": "1234", "product_id": "TESTPRODUCT"}


def booking(day, amount, text, name="", iban="", posting="Umbuchung"):
    return {"date": day, "entry_date": day, "amount": amount, "currency": "EUR", "posting_text": posting,
            "purpose": text, "applicant_name": name, "applicant_iban": iban}


# Giro side of two transfers to / from the savings account, added to the giro recording.
GIRO_MOVES = [booking("2026-09-10", "-500.00", "Sparen", "Max Mustermann", SAVINGS_IBAN),
              booking("2026-09-20", "200.00", "Urlaub", "Max Mustermann", SAVINGS_IBAN)]
SAVINGS_RECORDING = {"balance": {"amount": "301.23", "currency": "EUR"},
           "transactions": [booking("2026-09-10", "500.00", "Sparen", "Max Mustermann", GIRO_IBAN),
                            booking("2026-09-21", "-200.00", "Urlaub", "Max Mustermann", GIRO_IBAN),
                            booking("2026-09-30", "1.23", "Zinsen 3. Quartal", posting="Abschluss")]}


class GiroAndSavings(FakeFinTS):
    """A giro and a Tagesgeld account; the bank reports both as type 1 (like the DKB)."""

    def __init__(self, *args, moves=True, **kw):
        recording = deepcopy(GIRO)
        if moves:
            recording["transactions"] += GIRO_MOVES
        super().__init__(recording, **kw)
        self.savings = fints_mod.ReplayClient(SAVINGS_RECORDING)

    def get_sepa_accounts(self):
        return [fints_mod._Account(GIRO_IBAN), fints_mod._Account(SAVINGS_IBAN)]

    def get_information(self):
        return {"bank": {}, "accounts": [
            {"iban": GIRO_IBAN, "type": 1, "product_name": "Girokonto"},
            {"iban": SAVINGS_IBAN, "type": 1, "product_name": "Tagesgeld"},
        ]}

    def get_transactions(self, account, start_date=None, end_date=None):
        if account.iban == SAVINGS_IBAN:
            self.calls.append(("savings", start_date))
            return self.savings.get_transactions(account, start_date, end_date)
        return super().get_transactions(account, start_date, end_date)

    def get_balance(self, account):
        return (self.savings if account.iban == SAVINGS_IBAN else super()).get_balance(account)


ACC = Accounts("acc-cash", "acc-depot", "acc-savings")


def tx(**kw):
    base = dict(id="t1", kind=Kind.DEPOSIT, datetime=datetime(2026, 9, 10, 10, tzinfo=UTC), currency="EUR",
                net=D("500"), label="Umbuchung", text="Sparen")
    return Transaction(**{**base, **kw})


def test_savings_bookings_go_to_the_savings_account():
    [a] = to_activities(tx(account=SAVINGS, kind=Kind.INTEREST), "dkb", ACC)
    assert (a["accountId"], a["activityType"]) == ("acc-savings", "INTEREST")
    [b] = to_activities(tx(), "dkb", ACC)
    assert b["accountId"] == "acc-cash"
    with pytest.raises(MappingError):
        to_activities(tx(account=SAVINGS), "dkb", Accounts("acc-cash", "acc-depot"))


def test_a_transfer_between_cash_and_savings_is_one_pair():
    out, back = (to_activities(tx(kind=k, own_transfer=True), "dkb", ACC) for k in (Kind.WITHDRAWAL, Kind.DEPOSIT))
    assert [(a["accountId"], a["activityType"]) for a in out] == \
        [("acc-cash", "TRANSFER_OUT"), ("acc-savings", "TRANSFER_IN")]
    assert [(a["accountId"], a["activityType"]) for a in back] == \
        [("acc-savings", "TRANSFER_OUT"), ("acc-cash", "TRANSFER_IN")]
    assert out[0]["sourceGroupId"] == out[1]["sourceGroupId"] and out[0]["amount"] == out[1]["amount"] == "500"
    assert all(a["comment"].endswith("[SYNC dkb:t1]") for a in out + back)


def test_the_adapter_finds_the_tagesgeld_and_pairs_the_transfers(monkeypatch):
    monkeypatch.setattr(DkbAdapter, "client_factory", staticmethod(lambda *a: GiroAndSavings(sca=False)))
    a = DkbAdapter(CREDS)
    a.login()
    a.with_savings = True
    a.savings_since = datetime(2026, 8, 1, tzinfo=UTC)
    txs = a.get_transactions(datetime(2026, 9, 5, tzinfo=UTC))
    assert a._account().iban == GIRO_IBAN  # the Tagesgeld (type 1 as well) is not taken for the giro account
    giro_out, giro_in = [t for t in txs if t.own_transfer]
    assert (giro_out.kind, giro_in.kind) == (Kind.WITHDRAWAL, Kind.DEPOSIT) and not giro_out.account
    savings = [t for t in txs if t.account == SAVINGS]
    assert all(t.id.startswith("sav-") for t in savings)
    assert [(t.kind, t.pair) for t in savings] == \
        [(Kind.DEPOSIT, giro_out.id), (Kind.WITHDRAWAL, giro_in.id), (Kind.INTEREST, "")]
    assert ("savings", datetime(2026, 8, 1).date()) in a._client.calls
    assert a.history_start == datetime(2026, 9, 5).date()  # the giro account's, not the savings account's
    assert {(b.account, b.amount) for b in a.get_cash()} == {("", D("1950.06")), (SAVINGS, D("301.23"))}


def test_without_a_savings_account_nothing_changes(monkeypatch):
    monkeypatch.setattr(DkbAdapter, "client_factory", staticmethod(lambda *a: GiroAndSavings(sca=False)))
    a = DkbAdapter(CREDS)
    a.login()
    txs = a.get_transactions(datetime(2026, 9, 5, tzinfo=UTC))
    assert not any(t.own_transfer or t.account for t in txs)
    assert not [c for c in a._client.calls if c[0] == "savings"] and len(a.get_cash()) == 1


def test_an_unknown_savings_iban_is_explained(monkeypatch):
    monkeypatch.setattr(DkbAdapter, "client_factory", staticmethod(lambda *a: GiroAndSavings(sca=False)))
    a = DkbAdapter({**CREDS, "savings_iban": "DE00 1203 0000 0000 9999 99"})
    a.login()
    a.with_savings = True
    with pytest.raises(AdapterError, match="Tagesgeldkontos gehört nicht"):
        a.get_transactions(None)


def setup(tmp_path, monkeypatch, wf, *, savings=True, moves=True):
    monkeypatch.setattr(DkbAdapter, "client_factory",
                        staticmethod(lambda *a: GiroAndSavings(sca=False, moves=moves)))
    cfg = config_mod.Config(wealthfolio_url="http://wf", public_url="https://sync:8443")
    cfg.brokers["dkb"] = config_mod.BrokerConfig(enabled=True, cash_account_id="acc-cash",
                                                 portfolio_account_id="acc-depot", start_date="2026-08-01",
                                                 savings_account_id="acc-savings" if savings else "")
    config_mod.save(tmp_path, cfg)
    v = Vault(tmp_path)
    v.update(lambda d: d.__setitem__("wealthfolio_password", PASSWORD))
    v.set_broker_credentials("dkb", CREDS)
    syncer = Syncer(tmp_path, wealthfolio=lambda u, p: WealthfolioClient(u, p, transport=wf.transport()),
                    notifier=RecordingNotifier(), adapters={"dkb": DkbAdapter})
    syncer.recalc_wait = 0
    return syncer


def savings_cash(wf):
    return sum((wf.CASH_SIGN[a["activityType"]] * D(a["amount"]) for a in wf.activities
                if a["accountId"] == "acc-savings"), D(0))


def test_a_run_books_the_savings_account_and_each_transfer_once(tmp_path, monkeypatch):
    wf = FakeWealthfolio()
    syncer = setup(tmp_path, monkeypatch, wf)
    [r] = syncer.run()
    assert r.status == "ok"
    on_savings = sorted((a["activityType"], a["amount"]) for a in wf.activities if a["accountId"] == "acc-savings")
    assert on_savings == [("INTEREST", "1.23"), ("TRANSFER_IN", "500"), ("TRANSFER_OUT", "200")]
    assert savings_cash(wf) == D("301.23")
    # The savings sides are marked with their pair: nothing new next time, nothing missing.
    [r2] = syncer.run()
    assert (r2.created, r2.failed) == (0, 0)
    assert syncer.state.gaps("dkb") == []
    assert not [d for d in syncer.state.reconcile("dkb")["deviations"] if d["key"].startswith("savings:")]
    balances = {(b["account"], b["amount"]) for b in syncer.state.balances("dkb")}
    assert ("savings", "301.23") in balances


def test_transfers_booked_before_the_savings_account_was_assigned_get_their_savings_side(tmp_path, monkeypatch):
    wf = FakeWealthfolio()
    [r] = setup(tmp_path, monkeypatch, wf, savings=False).run()
    assert r.status == "ok" and not [a for a in wf.activities if a["accountId"] == "acc-savings"]
    # Now the user assigns the Tagesgeld: its whole history from the start date, the transfers booked
    # on the giro account before (as withdrawal / deposit) get their savings side on their own.
    syncer = setup(tmp_path, monkeypatch, wf)
    [r2] = syncer.run()
    assert r2.status == "ok"
    assert sorted(a["activityType"] for a in wf.activities if a["accountId"] == "acc-savings") == \
        ["DEPOSIT", "INTEREST", "WITHDRAWAL"]
    assert savings_cash(wf) == D("301.23")
    assert syncer.state.meta("savings-account:dkb") == "acc-savings"


def test_the_broker_settings_offer_the_savings_account(tmp_path):
    from fastapi.testclient import TestClient

    from brokersync.web.app import create_app

    from .test_web import csrf

    wf = FakeWealthfolio()
    app = create_app(tmp_path, wealthfolio=lambda url, pw: WealthfolioClient(url, pw, transport=wf.transport()),
                     adapters={"dkb": DkbAdapter}, notifier=RecordingNotifier(), run_in_thread=False)
    client = TestClient(app, base_url="http://sync.local:8090")
    client.post("/setup-password", data={"password": "geheim-123456", "password2": "geheim-123456"})
    token = csrf(client, "/setup/wealthfolio")
    client.post("/setup/wealthfolio", data={"csrf": token, "url": "http://wf.local:8080/", "password": PASSWORD})
    assert "Tagesgeld-Konto (optional)" in client.get("/brokers/dkb").text
    form = {"csrf": csrf(client, "/brokers/dkb"), "enabled": "on", "cash_account_id": "acc-cash",
            "portfolio_account_id": "acc-depot"}
    r = client.post("/brokers/dkb", data={**form, "savings_account_id": "acc-cash"})
    assert "eigenes Wealthfolio-Konto" in r.text
    assert "dkb" not in config_mod.load(tmp_path).brokers  # not saved
    client.post("/brokers/dkb", data={**form, "savings_account_id": "acc-savings"}, follow_redirects=False)
    assert config_mod.load(tmp_path).brokers["dkb"].savings_account_id == "acc-savings"
