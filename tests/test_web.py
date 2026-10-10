"""The web UI from first visit to first sync, with the fake broker and a fake Wealthfolio."""

import re

from fastapi.testclient import TestClient

from brokersync import config as config_mod
from brokersync.vault import Vault
from brokersync.wealthfolio import WealthfolioClient

from .fake_broker import CODE, FakeBroker
from .fakes import PASSWORD, FakeWealthfolio
from .test_sync import RecordingNotifier


def make(tmp_path):
    from brokersync.web.app import create_app

    wf = FakeWealthfolio()
    notifier = RecordingNotifier()
    app = create_app(tmp_path, wealthfolio=lambda url, pw: WealthfolioClient(url, pw, transport=wf.transport()),
                     adapters={"fake": FakeBroker}, notifier=notifier, run_in_thread=False)
    return TestClient(app, base_url="http://sync.local:8090"), wf, notifier


def csrf(client, path):
    r = client.get(path)
    m = re.search(r'name="csrf" value="([^"]+)"', r.text)
    assert m, f"no csrf on {path}"
    return m.group(1)


def test_assistant_from_first_visit_to_first_sync(tmp_path):
    client, wf, notifier = make(tmp_path)

    # First visit: choose the UI password.
    r = client.get("/", follow_redirects=False)
    assert r.headers["location"] == "/setup-password"
    r = client.post("/setup-password", data={"password": "short", "password2": "short"})
    assert "mindestens 8 Zeichen" in r.text
    r = client.post("/setup-password", data={"password": "geheim123", "password2": "geheim123"})
    assert "Einrichtung" in r.text and "Wealthfolio verbinden" in r.text
    assert config_mod.load(tmp_path).public_url == "http://sync.local:8090"

    # Step 1: Wealthfolio - a wrong password is reported, the right one connects.
    token = csrf(client, "/setup/wealthfolio")
    r = client.post("/setup/wealthfolio", data={"csrf": token, "url": "http://wf.local:8080/", "password": "nope"})
    assert "Verbindung fehlgeschlagen" in r.text
    r = client.post("/setup/wealthfolio", data={"csrf": token, "url": "http://wf.local:8080/", "password": PASSWORD})
    assert "Verbunden - 2 Konten" in r.text
    assert "wf.local:8080" in config_mod.load(tmp_path).wealthfolio_url
    # The password is not shown again.
    assert PASSWORD not in client.get("/setup/wealthfolio").text

    # Step 2: broker - credentials and the two accounts (inactive ones aren't offered).
    page = client.get("/brokers/fake").text
    assert "Test Cash" in page and "Altes Konto" not in page
    token = csrf(client, "/brokers/fake")
    r = client.post("/brokers/fake", data={"csrf": token, "cred_username": "max", "enabled": "on",
                                            "cash_account_id": "acc-cash", "portfolio_account_id": "acc-cash"})
    assert "verschiedene Konten" in r.text
    r = client.post("/brokers/fake", data={"csrf": token, "cred_username": "max", "enabled": "on",
                                            "cash_account_id": "acc-cash", "portfolio_account_id": "acc-depot"})
    # Saving new credentials leads straight to the broker login with the TAN.
    assert "Bestätigungscode" in r.text

    # Step 3: login with the code.
    token = csrf(client, "/brokers/fake/login")
    r = client.post("/brokers/fake/login", data={"csrf": token, "code": "999999"})
    assert "Falscher Code" in r.text
    r = client.post("/brokers/fake/login", data={"csrf": token, "code": CODE})
    assert "angemeldet" in r.text
    session = Vault(tmp_path).broker("fake")["session"]
    assert session["confirmed"] is True and "anchor" in session

    # Step 4: run now.
    r = client.post("/run", data={"csrf": token})
    assert "Abruf beendet" in r.text
    assert "3 neu" in r.text
    assert len(wf.activities) == 7
    r = client.post("/run", data={"csrf": token})
    assert len(wf.activities) == 7  # no duplicates
    assert "FAKE_SPECIAL_EVENT" in client.get("/unknown").text

    # Automatic fetch off: still shown with its last result, fetched by its own button.
    r = client.post("/brokers/fake", data={"csrf": token, "cred_username": "max", "cash_account_id": "acc-cash",
                                            "portfolio_account_id": "acc-depot"})
    page = client.get("/").text
    assert "automatischer Abruf aus" in page and "überspringen Broker" in page and "OK" in page
    assert 'name="broker" value="fake"' in page


def test_login_required_and_csrf_checked(tmp_path):
    client, _, _ = make(tmp_path)
    client.post("/setup-password", data={"password": "geheim123", "password2": "geheim123"})
    assert client.post("/run", data={"csrf": "forged"}).status_code == 400
    client.post("/logout", data={"csrf": csrf(client, "/")})
    r = client.get("/", follow_redirects=False)
    assert r.headers["location"] == "/login"
    assert "Falsches Passwort" in client.post("/login", data={"password": "wrong"}).text
    assert "Einrichtung" in client.post("/login", data={"password": "geheim123"}).text
    # The password can only be chosen once.
    r = client.post("/setup-password", data={"password": "andere123", "password2": "andere123"},
                    follow_redirects=False)
    assert r.headers["location"] == "/login"


def test_notifications_and_securities(tmp_path):
    client, _, notifier = make(tmp_path)
    client.post("/setup-password", data={"password": "geheim123", "password2": "geheim123"})
    page = client.get("/notifications").text
    topic = re.search(r'name="topic" value="(wealthfolio-sync-[0-9a-f]{12})"', page).group(1)
    token = csrf(client, "/notifications")
    r = client.post("/notifications", data={"csrf": token, "server": "https://ntfy.sh", "topic": topic,
                                            "public_url": "http://sync.local:8090", "test": "1"})
    assert "Testnachricht gesendet" in r.text
    assert notifier.sent[-1][0] == "Wealthfolio Broker Sync"
    assert config_mod.load(tmp_path).ntfy_topic == topic

    token = csrf(client, "/securities")
    client.post("/securities", data={"csrf": token, "isin": "ie00b4l5y983", "symbol": "EUNL", "exchange_mic": "XETR"})
    assert config_mod.load(tmp_path).security_mappings == {"IE00B4L5Y983": {"symbol": "EUNL", "exchangeMic": "XETR"}}
    client.post("/securities", data={"csrf": token, "isin": "IE00B4L5Y983", "delete": "1"})
    assert config_mod.load(tmp_path).security_mappings == {}


def test_healthz_is_public(tmp_path):
    client, _, _ = make(tmp_path)
    assert client.get("/healthz").json()["ok"] is True


def test_status_follows_the_real_state_of_the_sync(tmp_path):
    from brokersync.state import State
    from brokersync.sync import run_lock

    client, _, _ = make(tmp_path)
    client.post("/setup-password", data={"password": "geheim123", "password2": "geheim123"})
    # A run the process died in the middle of (restart, update, crash).
    stale = State(tmp_path)
    stale.start_run("fake")
    page = client.get("/").text
    assert "abgebrochen" in page and "läuft" not in page and "Alle abrufen" in page
    assert 'http-equiv="refresh"' not in page
    assert "Dienst wurde während des Abrufs beendet" in stale.runs()[0].message

    # A sync in progress elsewhere (the timer): shown as running, page refreshes itself,
    # and its run is not closed as stale.
    stale.start_run("fake")
    with run_lock(tmp_path):
        page = client.get("/").text
        assert "Abruf läuft" in page and 'http-equiv="refresh"' in page
        assert stale.runs()[0].status == "running"


def test_version_is_shown_in_the_header(tmp_path):
    import brokersync

    client, _, _ = make(tmp_path)
    for path in ("/setup-password", "/login"):
        assert f'<span class="version">v{brokersync.__version__}</span>' in client.get(path).text


def test_an_installation_with_the_old_dummy_broker_is_cleaned_up(tmp_path):
    from brokersync.state import State

    # What 0.3.6 left behind: the dummy next to a real broker, which stays untouched.
    cfg = config_mod.Config()
    cfg.brokers["dummy"] = config_mod.BrokerConfig(enabled=True, cash_account_id="acc-cash",
                                                   portfolio_account_id="acc-depot")
    cfg.brokers["fake"] = config_mod.BrokerConfig(enabled=True, cash_account_id="acc-cash",
                                                  portfolio_account_id="acc-depot")
    config_mod.save(tmp_path, cfg)
    vault = Vault(tmp_path)
    vault.set_broker_credentials("dummy", {"username": "x"})
    vault.set_broker_session("dummy", {"confirmed": True})
    vault.set_broker_credentials("fake", {"username": "max"})
    old = State(tmp_path)
    old.mark("dummy", "TEST-20261009-1", "imported", ["a1"])
    old.mark("dummy", "TEST-20261009-2", "imported", ["a2", "a3", "a4"])
    old.mark("dummy", "dummy-0003", "existing", ["csv-1"])
    old.finish_run(old.start_run("dummy"), "ok", created=2)
    old.add_unknown("dummy", "TEST-20261009-4", "DUMMY_SPECIAL_EVENT", "2026-10-09T12:00:00+00:00", {})
    old.set_reconcile("dummy", [{"name": "Cash EUR", "broker": "1", "wealthfolio": "2"}])
    old.set_balances("dummy", [("EUR", "502.68")])
    old.set_flag("assets-learned:dummy")
    old.mark("fake", "f-1", "imported", ["b1"])
    old.finish_run(old.start_run("fake"), "ok", created=1)

    client, _, _ = make(tmp_path)
    assert "dummy" not in config_mod.load(tmp_path).brokers and "fake" in config_mod.load(tmp_path).brokers
    assert vault.broker("dummy") == {} and vault.broker("fake")["credentials"] == {"username": "max"}
    state = State(tmp_path)
    assert state.known("dummy") == set() and state.runs("dummy") == [] and state.unknown_events() == []
    assert state.reconcile("dummy") is None and state.balances("dummy") == []
    assert not state.flag("assets-learned:dummy")
    assert state.known("fake") == {"f-1"} and len(state.runs("fake")) == 1

    client.post("/setup-password", data={"password": "geheim123", "password2": "geheim123"})
    page = client.get("/").text
    # Its activities stay in Wealthfolio: shown once, with the count and how to find them.
    assert "Dummy (Test) entfernt" in page and "4 Buchungen" in page and "[SYNC dummy:" in page
    assert "/brokers/dummy" not in page
    r = client.post("/retired/dismiss", data={"csrf": csrf(client, "/"), "broker": "dummy"})
    assert "Dummy (Test) entfernt" not in r.text
    # A restart finds nothing left to clean up and doesn't show it again.
    client, _, _ = make(tmp_path)
    client.post("/login", data={"password": "geheim123"})
    assert "Dummy (Test) entfernt" not in client.get("/").text
    assert "Unbekannter Broker" in client.get("/brokers/dummy").text


def test_refetch_button(tmp_path):
    from brokersync.state import State
    from brokersync.sync import refetch_flag

    client, _, _ = make(tmp_path)
    client.post("/setup-password", data={"password": "geheim123", "password2": "geheim123"})
    page = client.get("/brokers/fake").text
    assert "Ab Startdatum neu abrufen" in page
    r = client.post("/brokers/fake/refetch", data={"csrf": csrf(client, "/brokers/fake")})
    assert "Der nächste Abruf holt noch einmal alles, was der Broker liefert." in r.text
    assert State(tmp_path).flag(refetch_flag("fake"))
