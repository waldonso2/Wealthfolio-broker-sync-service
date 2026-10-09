"""The web UI from first visit to first sync, with the dummy adapter and a fake Wealthfolio."""

import re

from fastapi.testclient import TestClient

from brokersync import config as config_mod
from brokersync.adapters.dummy import CODE, DummyAdapter
from brokersync.vault import Vault
from brokersync.wealthfolio import WealthfolioClient

from .fakes import PASSWORD, FakeWealthfolio
from .test_sync import RecordingNotifier


def make(tmp_path):
    from brokersync.web.app import create_app

    wf = FakeWealthfolio()
    notifier = RecordingNotifier()
    app = create_app(tmp_path, wealthfolio=lambda url, pw: WealthfolioClient(url, pw, transport=wf.transport()),
                     adapters={"dummy": DummyAdapter}, notifier=notifier, run_in_thread=False)
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
    page = client.get("/brokers/dummy").text
    assert "Dummy Cash" in page and "Altes Konto" not in page
    token = csrf(client, "/brokers/dummy")
    r = client.post("/brokers/dummy", data={"csrf": token, "cred_username": "max", "enabled": "on",
                                            "cash_account_id": "acc-cash", "portfolio_account_id": "acc-cash"})
    assert "verschiedene Konten" in r.text
    r = client.post("/brokers/dummy", data={"csrf": token, "cred_username": "max", "enabled": "on",
                                            "cash_account_id": "acc-cash", "portfolio_account_id": "acc-depot"})
    # Saving new credentials leads straight to the broker login with the TAN.
    assert "Bestätigungscode" in r.text

    # Step 3: login with the code.
    token = csrf(client, "/brokers/dummy/login")
    r = client.post("/brokers/dummy/login", data={"csrf": token, "code": "999999"})
    assert "Falscher Code" in r.text
    r = client.post("/brokers/dummy/login", data={"csrf": token, "code": CODE})
    assert "angemeldet" in r.text
    session = Vault(tmp_path).broker("dummy")["session"]
    assert session["confirmed"] is True and "anchor" in session

    # Step 4: run now.
    r = client.post("/run", data={"csrf": token})
    assert "Abruf beendet" in r.text
    assert "3 neu" in r.text
    assert len(wf.activities) == 7
    r = client.post("/run", data={"csrf": token})
    assert len(wf.activities) == 7  # no duplicates
    assert "DUMMY_SPECIAL_EVENT" in client.get("/unknown").text


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
    stale.start_run("dummy")
    page = client.get("/").text
    assert "abgebrochen" in page and "läuft" not in page and "Jetzt abrufen" in page
    assert 'http-equiv="refresh"' not in page
    assert "Dienst wurde während des Abrufs beendet" in stale.runs()[0].message

    # A sync in progress elsewhere (the timer): shown as running, page refreshes itself,
    # and its run is not closed as stale.
    stale.start_run("dummy")
    with run_lock(tmp_path):
        page = client.get("/").text
        assert "Abruf läuft" in page and 'http-equiv="refresh"' in page
        assert stale.runs()[0].status == "running"
