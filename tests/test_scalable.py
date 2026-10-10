"""Scalable Capital through its official CLI: login, session handling, errors and a full sync.

The CLI is replaced by ``fixtures/scalable/fake_sc.py``, which answers like
``sc`` (JSON envelope, exit codes, files in its config directory).
"""

import json
import threading
from decimal import Decimal as D
from pathlib import Path

import pytest

from brokersync import config as config_mod
from brokersync.adapters import AdapterError, AuthRequired
from brokersync.adapters import scalable as sc_mod
from brokersync.adapters.scalable import ScalableAdapter
from brokersync.mapping import Accounts, to_activities
from brokersync.sync import Syncer
from brokersync.vault import Vault
from brokersync.wealthfolio import WealthfolioClient

from .fakes import PASSWORD, FakeWealthfolio
from .test_sync import RecordingNotifier

FAKE_SC = Path(__file__).parent / "fixtures" / "scalable" / "fake_sc.py"
CASE = json.loads((Path(__file__).parent / "contract" / "scalable" / "transactions.json").read_text())


@pytest.fixture
def fake_sc(tmp_path, monkeypatch):
    ctl = tmp_path / "fake-sc"
    ctl.mkdir()
    monkeypatch.setenv("BROKERSYNC_SC", str(FAKE_SC))
    monkeypatch.setenv("FAKE_SC_DIR", str(ctl))
    monkeypatch.setattr(sc_mod, "CONFIRM_WAIT", 0.5)
    rec = CASE["recording"]
    data = {"transactions first": rec["transactions"][0], "transactions c2": rec["transactions"][1],
            "broker holdings": rec["holdings"], "broker cash-breakdown": rec["cash"], "whoami": {"email": "x"}}
    data.update({f"details {k}": v for k, v in rec["details"].items()})
    (ctl / "data.json").write_text(json.dumps(data))
    return ctl


def calls(ctl) -> list[str]:
    return (ctl / "calls").read_text().splitlines() if (ctl / "calls").exists() else []


def test_device_login_then_runs_without_the_user(fake_sc):
    a = ScalableAdapter({}, {})
    with pytest.raises(AuthRequired) as e:
        a.login()
    assert e.value.challenge.url == "https://secure.scalable.example/device?user_code=ABCD-EFGH"
    assert "ABCD-EFGH" in e.value.challenge.message
    # Clicked before confirming in the browser: asked again, the login keeps waiting.
    with pytest.raises(AuthRequired) as again:
        a.complete_login("")
    assert "noch nicht" in again.value.challenge.message and again.value.challenge.url
    (fake_sc / "confirmed").touch()
    a.complete_login("")
    tmp = a.cli.home
    a.close()
    assert not tmp.exists()  # nothing of the session stays on disk
    session = a.session_state()
    assert set(session["files"]) == {"session.json", "auth-signing-key.json"}

    # The next run logs in with the stored session and keeps the rotated refresh token.
    b = ScalableAdapter({}, session)
    b.login()
    assert [t.id for t in b.get_transactions(None)][:3] == ["sc-0001", "sc-0002", "sc-0003"]
    b.close()
    assert json.loads(b.session_state()["files"]["session.json"])["session"]["refresh_token"] != "r1"
    assert not any(c.startswith("login") for c in calls(fake_sc)[1:])
    assert all("--local-read-only" in c for c in calls(fake_sc) if c.startswith("login"))


def test_cli_not_enabled_in_the_profile(fake_sc):
    (fake_sc / "mode").write_text("no_grant")
    with pytest.raises(AdapterError, match="Agentic Investing"):
        ScalableAdapter({}, {}).login()


def test_expired_session_in_a_scheduled_run_sends_the_link_and_waits(fake_sc, monkeypatch):
    (fake_sc / "confirmed").touch()
    first = ScalableAdapter({}, {})
    with pytest.raises(AuthRequired):
        first.login()
    first.complete_login("")
    first.close()
    (fake_sc / "confirmed").unlink()
    (fake_sc / "mode").write_text("relogin")

    a = ScalableAdapter({}, first.session_state())
    sent = []

    def confirm_later(message):
        sent.append(message)
        (fake_sc / "mode").write_text("")
        threading.Timer(0.3, (fake_sc / "confirmed").touch).start()

    a.on_user_action = confirm_later
    a.login()
    assert "ABCD-EFGH" in sent[0] and "https://secure.scalable.example/device" in sent[0]
    assert a.get_cash()[0].amount == D("508.25")
    a.close()


def test_missing_cli_is_a_clear_error(tmp_path, monkeypatch):
    monkeypatch.setenv("BROKERSYNC_SC", str(tmp_path / "nowhere" / "sc"))
    with pytest.raises(AdapterError, match="nicht installiert"):
        ScalableAdapter({}, {"files": {"session.json": "{}"}}).login()


def test_full_sync_books_like_the_addon(tmp_path, monkeypatch):
    monkeypatch.setattr(ScalableAdapter, "cli_factory", lambda files: sc_mod._Replay(CASE["recording"]))
    wf = FakeWealthfolio()
    cfg = config_mod.Config(wealthfolio_url="http://wf.local:8080", public_url="http://sync.local:8090")
    cfg.brokers["scalable"] = config_mod.BrokerConfig(enabled=True, cash_account_id="acc-cash",
                                                      portfolio_account_id="acc-depot")
    config_mod.save(tmp_path, cfg)
    vault = Vault(tmp_path)
    vault.update(lambda d: d.__setitem__("wealthfolio_password", PASSWORD))
    vault.set_broker_credentials("scalable", {"portfolio_id": ""})
    vault.set_broker_session("scalable", {"files": {"session.json": "{}"}})
    notifier = RecordingNotifier()
    syncer = Syncer(tmp_path, wealthfolio=lambda u, p: WealthfolioClient(u, p, transport=wf.transport()),
                    notifier=notifier, adapters={"scalable": ScalableAdapter})
    syncer.recalc_wait = 0
    [r] = syncer.run()
    assert (r.status, r.failed, r.unknown) == ("ok", 0, 3), r.messages
    assert r.created == 11
    cash = {h["localCurrency"]: D(h["quantity"]) for h in wf.holdings("acc-cash") if h["holdingType"] == "cash"}
    depot = {h["localCurrency"]: D(h["quantity"]) for h in wf.holdings("acc-depot") if h["holdingType"] == "cash"}
    # 1000 - 501 - 25 + 3.68 - 12.30 - 4.99 + 188.45 - 200 + 0.42 + 2.10 + 7.50
    assert cash == {"EUR": D("458.86")} and depot == {"EUR": D(0)}
    dividend = next(a for a in wf.activities if a["activityType"] == "DIVIDEND")
    assert (dividend["quantity"], dividend["amount"], dividend["tax"]) == ("1", "3.68", "1.32")
    assert [e["raw_type"] for e in syncer.state.unknown_events()] == [
        "ELTIF_TRANSACTION", "TRANSFER_IN", "DISTRIBUTION_CANCELLATION"]
    # Nothing personal in an unknown event's payload: no description texts.
    assert all("description" not in e["payload"] for e in syncer.state.unknown_events())


def test_a_csv_import_of_the_same_buy_is_recognised():
    from brokersync.dedup import ExistingIndex
    from brokersync.model import Kind, Transaction

    buy = Transaction("sc-0002", Kind.BUY, sc_mod._when("2026-01-06T09:12:34Z"), "EUR", D("501"),
                      isin="IE00TEST0001", name="TEST Fonds World", shares=D("5"), gross=D("500"), fee=D("1"))
    payloads = to_activities(buy, "scalable", Accounts("c", "p"))
    # What the addon's CSV import booked: the user's ticker, its own comment and time stamp.
    csv = {"id": "csv-1", "accountId": "p", "activityType": "BUY", "date": "2026-01-06T09:12:34.000Z",
           "quantity": "5", "amount": "501", "assetSymbol": "TWLD", "comment": "TEST Fonds World [09:12:34]"}
    assert ExistingIndex([csv]).find("[SYNC scalable:sc-0002]", payloads) == "csv-1"


def test_login_in_the_web_ui(tmp_path, fake_sc):
    import re

    from fastapi.testclient import TestClient

    from brokersync.web.app import create_app

    wf = FakeWealthfolio()
    app = create_app(tmp_path, wealthfolio=lambda u, p: WealthfolioClient(u, p, transport=wf.transport()),
                     adapters={"scalable": ScalableAdapter}, notifier=RecordingNotifier(), run_in_thread=False)
    client = TestClient(app)
    client.post("/setup-password", data={"password": "geheim123", "password2": "geheim123"})
    page = client.get("/brokers/scalable/login").text
    assert 'href="https://secure.scalable.example/device?user_code=ABCD-EFGH"' in page
    assert "ABCD-EFGH" in page and "Ich habe bestätigt" in page
    token = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    (fake_sc / "confirmed").touch()
    r = client.post("/brokers/scalable/login", data={"csrf": token, "code": ""})
    assert "Scalable Capital: angemeldet" in r.text
    assert "session.json" in Vault(tmp_path).broker("scalable")["session"]["files"]


def test_the_log_shows_what_scalable_sent_without_values(caplog):
    import logging

    caplog.set_level(logging.INFO, "brokersync.adapters.scalable")
    rec = dict(CASE["recording"], cash={"cash_balance": None, "buying_power": {"amount": "1.5"}})
    a = ScalableAdapter.replay(rec)
    a.get_transactions(None)
    assert "scalable: 19 transactions from sc (" in caplog.text
    assert "CASH_TRANSACTION/SETTLED/DEPOSIT: 2" in caplog.text
    with pytest.raises(AdapterError, match="keinen Kontostand"):
        a.get_cash()
    assert '"buying_power": {"amount": "str"}' in caplog.text
    assert "1.5" not in caplog.text and "sc-0001" not in caplog.text and "IE00TEST0001" not in caplog.text
