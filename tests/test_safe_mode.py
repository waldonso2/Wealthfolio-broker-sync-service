"""Abrufmodus "Sicher" and PINs typed at every login (waldonso2/wealthfolio-importer-addon#85)."""

import re

import pytest
from fastapi.testclient import TestClient

from brokersync import config as config_mod
from brokersync import redact
from brokersync.adapters.base import AuthRequired, safety_state
from brokersync.adapters.dkb import DkbAdapter
from brokersync.state import State
from brokersync.vault import Vault
from brokersync.wealthfolio import WealthfolioClient

from .fake_broker import CODE, FakeBroker
from .fake_fints import FakeFinTS
from .fakes import PASSWORD, FakeWealthfolio
from .helpers import first_password
from .test_dkb import CREDS, RECORDING
from .test_dkb import fake as fake
from .test_sync import RecordingNotifier, setup

PIN = "4711-typed"


class SecondBroker(FakeBroker):
    key = "fake2"
    label = "Zweitbroker"


def make(tmp_path, adapters=None, safe=True):
    from brokersync.web.app import create_app

    wf = FakeWealthfolio()
    adapters = adapters or {"fake": FakeBroker}
    cfg = config_mod.Config(wealthfolio_url="http://wf.local:8080", sync_mode="safe" if safe else "auto")
    for key in adapters:
        cfg.brokers[key] = config_mod.BrokerConfig(enabled=True, cash_account_id="acc-cash",
                                                   portfolio_account_id="acc-depot")
    config_mod.save(tmp_path, cfg)
    vault = Vault(tmp_path)
    vault.update(lambda d: d.update(wealthfolio_password=PASSWORD, wealthfolio_checked=True))
    for key in adapters:
        vault.set_broker_credentials(key, {"username": "test"})
    app = create_app(tmp_path, wealthfolio=lambda u, p: WealthfolioClient(u, p, transport=wf.transport()),
                     adapters=adapters, notifier=RecordingNotifier(), run_in_thread=False)
    client = TestClient(app, base_url="http://sync.local:8090")
    first_password(client)
    token = re.search(r'name="csrf" value="([^"]+)"', client.get("/broker").text).group(1)
    return client, wf, token


def test_the_timer_contacts_no_broker_in_the_safe_mode_and_reminds(tmp_path):
    wf = FakeWealthfolio()
    syncer, notifier = setup(tmp_path, wf)
    cfg = config_mod.load(tmp_path)
    cfg.sync_mode = "safe"
    config_mod.save(tmp_path, cfg)
    assert syncer.run() == [] and wf.activities == [] and State(tmp_path).last_run("fake") is None
    assert notifier.sent[0][0] == "Abruf fällig" and "Testbroker" in notifier.sent[0][1]
    # Started in the UI after a login, it fetches - and ends the session afterwards.
    [r] = syncer.run(manual=True)
    assert r.status == "ok" and r.created == 3
    assert Vault(tmp_path).broker("fake")["session"] == {}


def test_the_session_ends_after_every_fetch_but_the_lock_out_guards_stay(tmp_path):
    wf = FakeWealthfolio()
    syncer, _ = setup(tmp_path, wf)
    cfg = config_mod.load(tmp_path)
    cfg.sync_mode = "safe"
    config_mod.save(tmp_path, cfg)
    Vault(tmp_path).set_broker_session("fake", {"confirmed": True, "anchor": "2026-03-02T10:00:00+00:00",
                                                "wrong_tans": 2, "open_challenges": 1})
    syncer.run(manual=True)
    assert Vault(tmp_path).broker("fake")["session"] == {"wrong_tans": 2, "open_challenges": 1}
    assert safety_state({"pin_rejected": True, "cookies": "x"}) == {"pin_rejected": True}


def test_fetch_now_starts_with_the_login_and_logs_out_afterwards(tmp_path):
    client, wf, token = make(tmp_path)
    page = client.get("/broker").text
    assert "Sicherer Modus" in page
    r = client.post("/run", data={"csrf": token, "broker": "fake"})
    # The POST started the login itself (#99).
    assert 'name="run" value="1"' in r.text and "Danach ruft der Dienst gleich ab" in r.text
    r = client.post("/brokers/fake/login", data={"csrf": token, "code": CODE, "run": "1"})
    assert "Abruf beendet" in r.text and len(wf.activities) == 7
    assert Vault(tmp_path).broker("fake")["session"] == {}
    # The next fetch needs the login again.
    r = client.post("/run", data={"csrf": token, "broker": "fake"})
    assert f"Gib den Bestätigungscode {CODE} ein" in r.text


def test_fetch_all_chains_the_logins_and_a_broker_can_be_skipped(tmp_path):
    client, wf, token = make(tmp_path, adapters={"fake": FakeBroker, "fake2": SecondBroker})
    r = client.post("/run", data={"csrf": token})
    assert "Anmeldung 1 von 2" in r.text and "überspringen" in r.text
    r = client.post("/brokers/fake/login", data={"csrf": token, "code": CODE, "run": "1"})
    assert "Anmeldung 2 von 2" in r.text and "Zweitbroker" in r.text
    assert wf.activities == []  # nothing fetched before the last login
    r = client.post("/brokers/fake2/login/skip", data={"csrf": token})
    assert "Abruf beendet" in r.text and len(wf.activities) == 7
    state = State(tmp_path)
    assert state.last_run("fake").status == "ok" and state.last_run("fake2") is None


def test_switching_to_the_safe_mode_ends_every_saved_login(tmp_path):
    client, _, token = make(tmp_path, safe=False)
    Vault(tmp_path).set_broker_session("fake", {"confirmed": True, "anchor": "2026-03-02T10:00:00+00:00"})
    assert 'value="safe"' in client.get("/security").text
    r = client.post("/security/mode", data={"csrf": token, "mode": "safe"})
    assert "Sicherer Modus an" in r.text
    assert config_mod.load(tmp_path).safe_mode and Vault(tmp_path).broker("fake")["session"] == {}
    assert "Unbekannter Abrufmodus" in client.post("/security/mode", data={"csrf": token, "mode": "x"}).text
    client.post("/security/mode", data={"csrf": token, "mode": "auto"})
    assert not config_mod.load(tmp_path).safe_mode


# ── FinTS: the PIN typed at every login ─────────────────────────────────────
def test_fints_asks_for_the_pin_when_none_is_stored(fake):
    a = DkbAdapter({**CREDS, "pin": ""})
    with pytest.raises(AuthRequired) as e:
        a.login()
    assert e.value.challenge.kind == "pin" and FakeFinTS.instances == []
    with pytest.raises(AuthRequired) as e:
        a.complete_login("")
    assert e.value.challenge.kind == "pin"
    with pytest.raises(AuthRequired) as e:
        a.complete_login(PIN)  # then the bank wants the app confirmation
    assert e.value.challenge.kind == "confirm" and a.credentials["pin"] == PIN
    a.complete_login("")
    a.close()
    assert PIN not in str(a.session_state())
    assert PIN not in redact.redact(f"error with {PIN}")


def test_fints_pin_typed_in_the_ui_is_used_for_the_run_and_never_stored(tmp_path, fake, caplog):
    import logging

    caplog.set_level(logging.DEBUG)
    client, wf, token = make(tmp_path, adapters={"dkb": DkbAdapter}, safe=False)
    Vault(tmp_path).set_broker_credentials("dkb", {**CREDS, "pin": ""})
    # Auto mode, but no PIN stored: "Jetzt abrufen" starts with the login too.
    r = client.post("/run", data={"csrf": token, "broker": "dkb"})
    assert 'type="password" id="code"' in r.text
    r = client.post("/brokers/dkb/login", data={"csrf": token, "code": PIN, "run": "1"})
    assert "Ich habe in der App bestätigt" in r.text
    r = client.post("/brokers/dkb/login", data={"csrf": token, "code": "", "run": "1"})
    assert "Abruf beendet" in r.text
    assert State(tmp_path).last_run("dkb").status == "ok"
    assert PIN not in str(Vault(tmp_path).load()) and PIN not in caplog.text
    for f in tmp_path.rglob("*"):
        if f.is_file():
            assert PIN.encode() not in f.read_bytes()


def test_a_stored_pin_can_be_dropped_in_the_broker_settings(tmp_path, fake):
    client, _, token = make(tmp_path, adapters={"dkb": DkbAdapter}, safe=False)
    Vault(tmp_path).set_broker_credentials("dkb", dict(CREDS))
    page = client.get("/brokers/dkb").text
    assert 'name="forget_pin"' in page
    client.post("/brokers/dkb", data={"csrf": token, "cred_username": "max", "cred_pin": "", "forget_pin": "1",
                                      "cred_product_id": "TESTPRODUCT", "cred_iban": "", "enabled": "on",
                                      "cash_account_id": "acc-cash", "portfolio_account_id": "acc-depot"})
    assert Vault(tmp_path).broker("dkb")["credentials"]["pin"] == ""
    assert 'name="forget_pin"' not in client.get("/brokers/dkb").text
    assert RECORDING  # the recorded bank answers the fake client replays
