"""Logos from logo.dev by ISIN, set as custom asset logos in Wealthfolio."""

import base64
import json
import logging
from datetime import UTC, datetime, timedelta

import httpx

from brokersync import config as config_mod
from brokersync import logos
from brokersync.state import State
from brokersync.vault import Vault
from brokersync.wealthfolio import WealthfolioClient

from .fakes import PASSWORD, FakeWealthfolio
from .helpers import first_password
from .test_sync import setup
from .test_web import csrf

APPLE, SAP, MSCI = "US0378331005", "DE0007164600", "IE00B4L5Y983"
TOKEN = "pk_test-logo-key-123"


def png(width=128, height=128, size=200) -> bytes:
    head = logos.PNG_SIGNATURE + (13).to_bytes(4, "big") + b"IHDR" + width.to_bytes(4, "big") \
        + height.to_bytes(4, "big")
    return head + b"\0" * max(0, size - len(head))


class FakeLogoDev:
    """logo.dev's image endpoint: a PNG per known ISIN, 404 for the rest."""

    def __init__(self, logos_by_isin: dict[str, bytes], status: int | None = None):
        self.logos = logos_by_isin
        self.status = status
        self.asked: list[str] = []
        self.params: list[dict] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        assert request.url.host == "img.logo.dev"
        isin = request.url.path.rsplit("/", 1)[1]
        self.asked.append(isin)
        self.params.append(dict(request.url.params))
        if self.status:
            return httpx.Response(self.status)
        if request.url.params.get("token") != TOKEN:
            return httpx.Response(401)
        if isin not in self.logos:
            return httpx.Response(404)
        return httpx.Response(200, content=self.logos[isin], headers={"content-type": "image/png"})

    def source(self, token: str) -> logos.LogoDev:
        return logos.LogoDev(token, transport=httpx.MockTransport(self.handle))


def asset(asset_id, *, isin=None, symbol=None):
    return {"id": asset_id, "displayCode": symbol or asset_id, "instrumentSymbol": symbol or asset_id,
            "metadata": {"identifiers": {"isin": isin}} if isin else None}


def run_pass(tmp_path, wf, dev, learned=None, **kw):
    state = State(tmp_path)
    with WealthfolioClient("http://wf.local:8080", PASSWORD, transport=wf.transport()) as client:
        return logos.update(state, client, dev.source(TOKEN), learned or {}, **kw), state


def test_sets_logos_by_isin_and_leaves_the_users_own(tmp_path):
    wf = FakeWealthfolio()
    wf.assets = [
        asset("a-apple", isin=APPLE, symbol="AAPL"),       # ISIN in Wealthfolio's metadata
        asset("a-sap", symbol=SAP),                        # booked under the ISIN
        asset("a-msci", symbol="EUNL"),                    # ticker only: the sync learned its ISIN
        asset("a-own", isin="US5949181045", symbol="MSFT"),  # the user set a logo
        asset("$CASH-EUR"),
    ]
    wf.logos["a-own"] = "users-own"
    dev = FakeLogoDev({APPLE: png(), MSCI: png(64, 64)})
    result, state = run_pass(tmp_path, wf, dev, {"a-msci": MSCI, "a-deleted": "US88160R1014"})
    assert (result.set, result.none, result.failed, result.message) == (2, 1, 0, "")
    assert sorted(dev.asked) == [SAP, MSCI, APPLE]  # never the asset with a logo, never a deleted one
    assert dev.params[0] == {"token": TOKEN, "format": "png", "size": "128", "fallback": "404"}
    assert base64.b64decode(wf.logos["a-apple"]) == png()
    assert wf.logos["a-own"] == "users-own"
    assert "a-sap" not in wf.logos
    assert json.loads(state.meta(logos.status_key("a-sap")))["status"] == "none"
    assert logos.last(state).set == 2

    # Next run: nothing to ask - set ones are done, "none" waits RETRY_DAYS.
    dev.asked.clear()
    result, _ = run_pass(tmp_path, wf, dev)
    assert dev.asked == [] and result.set == 0


def test_a_logo_the_user_removed_stays_removed_and_none_is_asked_again_later(tmp_path):
    wf = FakeWealthfolio()
    wf.assets = [asset("a-apple", isin=APPLE), asset("a-sap", symbol=SAP)]
    dev = FakeLogoDev({APPLE: png()})
    run_pass(tmp_path, wf, dev)
    del wf.logos["a-apple"]
    dev.logos[SAP] = png()
    dev.asked.clear()
    later = datetime.now(UTC) + timedelta(days=logos.RETRY_DAYS + 1)
    result, _ = run_pass(tmp_path, wf, dev, at=later)
    assert dev.asked == [SAP] and result.set == 1
    assert "a-apple" not in wf.logos


def test_logos_that_wealthfolio_would_reject_are_not_sent(tmp_path):
    wf = FakeWealthfolio()
    wf.assets = [asset("a1", isin=APPLE), asset("a2", isin=SAP), asset("a3", isin=MSCI)]
    dev = FakeLogoDev({APPLE: png(512, 512), SAP: b"\xff\xd8\xff jpeg", MSCI: png(size=200 * 1024)})
    result, state = run_pass(tmp_path, wf, dev)
    assert (result.set, result.failed) == (0, 3)
    assert wf.logos == {}
    assert state.meta(logos.status_key("a1")) is None  # tried again next run


def test_a_rejected_key_or_the_rate_limit_ends_the_pass(tmp_path):
    wf = FakeWealthfolio()
    wf.assets = [asset("a1", isin=APPLE), asset("a2", isin=SAP)]
    for status, text in ((401, "abgelehnt"), (429, "Abruflimit")):
        dev = FakeLogoDev({}, status=status)
        result, _ = run_pass(tmp_path, wf, dev)
        assert len(dev.asked) == 1 and result.open == 2 and text in result.message


def test_limit_per_run_and_one_question_per_isin(tmp_path):
    wf = FakeWealthfolio()
    wf.assets = [asset("a1", isin=APPLE), asset("a2", symbol=APPLE), asset("a3", isin=SAP), asset("a4", isin=MSCI)]
    dev = FakeLogoDev({APPLE: png(), SAP: png(), MSCI: png()})
    result, _ = run_pass(tmp_path, wf, dev, limit=2)
    assert sorted(dev.asked) == [SAP, MSCI]  # sorted by ISIN, two questions
    assert (result.set, result.open) == (2, 2)
    result, _ = run_pass(tmp_path, wf, dev, limit=2)
    assert dev.asked.count(APPLE) == 1 and set(wf.logos) == {"a1", "a2", "a3", "a4"}


def test_an_older_wealthfolio_is_reported_not_an_error(tmp_path):
    wf = FakeWealthfolio()
    wf.logos = None
    wf.assets = [asset("a1", isin=APPLE)]
    dev = FakeLogoDev({APPLE: png()})
    result, _ = run_pass(tmp_path, wf, dev)
    assert "3.8.0" in result.message and dev.asked == []


def test_after_every_run_only_when_turned_on_and_never_failing_a_broker(tmp_path, caplog):
    from brokersync import redact

    wf = FakeWealthfolio()
    wf.assets = [asset("a1", isin=APPLE)]
    dev = FakeLogoDev({APPLE: png()})
    syncer, _ = setup(tmp_path, wf)
    syncer._logo_source = dev.source
    syncer.run()
    assert dev.asked == []  # off by default

    cfg = config_mod.load(tmp_path)
    cfg.logos_enabled = True
    config_mod.save(tmp_path, cfg)
    syncer.run()
    assert dev.asked == []  # no key yet

    Vault(tmp_path).update(lambda d: d.__setitem__("logo_dev_token", TOKEN))
    redact.install()
    caplog.set_level(logging.INFO)
    [r] = syncer.run()
    assert r.status == "ok" and dev.asked == [APPLE] and "a1" in wf.logos
    assert TOKEN not in caplog.text

    # logo.dev down: the broker's run is still fine.
    dev.status = 500
    wf.assets.append(asset("a2", isin=SAP))
    [r] = syncer.run()
    assert r.status == "ok" and logos.last(syncer.state).failed == 1


def test_page_saves_the_key_in_the_vault_and_fetches(tmp_path):
    from fastapi.testclient import TestClient

    from brokersync.web.app import create_app

    wf = FakeWealthfolio()
    wf.assets = [asset("a1", isin=APPLE)]
    dev = FakeLogoDev({APPLE: png()})
    app = create_app(tmp_path, wealthfolio=lambda url, pw: WealthfolioClient(url, pw, transport=wf.transport()),
                     adapters={}, run_in_thread=False, logo_source=dev.source)
    client = TestClient(app, base_url="http://sync.local:8090")
    first_password(client)
    Vault(tmp_path).update(lambda d: d.__setitem__("wealthfolio_password", PASSWORD))
    token = csrf(client, "/logos")
    r = client.post("/logos", data={"csrf": token, "fetch": "1"})
    assert "Erst einschalten" in r.text
    r = client.post("/logos", data={"csrf": token, "enabled": "1", "token": TOKEN, "fetch": "1"})
    assert "1 Logos gesetzt" in r.text and "a1" in wf.logos
    assert config_mod.load(tmp_path).logos_enabled
    assert Vault(tmp_path).load()["logo_dev_token"] == TOKEN
    page = client.get("/logos").text
    assert TOKEN not in page and "(gespeichert)" in page and "Letzter Durchlauf" in page
    client.post("/logos", data={"csrf": token, "enabled": "1", "remove_token": "1"})
    assert "logo_dev_token" not in Vault(tmp_path).load()
