"""The web UI from first visit to first sync, with the fake broker and a fake Wealthfolio."""

import re

import pytest
from fastapi.testclient import TestClient

from brokersync import config as config_mod
from brokersync.vault import Vault, new_setup_code
from brokersync.wealthfolio import WealthfolioClient

from .fake_broker import CODE, FakeBroker
from .fakes import PASSWORD, FakeWealthfolio
from .helpers import first_password
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
    # Not without the one-time code from the console, and never from another page (#89).
    assert client.post("/setup-password", data={"password": "Kiefer-Ozelot-9182",
                                                "password2": "Kiefer-Ozelot-9182"}).status_code == 400
    token = csrf(client, "/setup-password")
    r = client.post("/setup-password", data={"csrf": token, "code": "guessed", "password": "Kiefer-Ozelot-9182",
                                             "password2": "Kiefer-Ozelot-9182"})
    assert "Einrichtungscode stimmt nicht" in r.text
    code = new_setup_code(Vault(tmp_path))
    r = client.post("/setup-password", data={"csrf": token, "code": code, "password": "short", "password2": "short"})
    assert "mindestens 12 Zeichen" in r.text and f'value="{code}"' in r.text  # the code stays filled in
    r = first_password(client)
    assert "setup_code" not in Vault(tmp_path).load()  # used up
    # A checklist of what is left to set up (#110).
    assert "Einrichtung: 0 von 3 erledigt" in r.text and 'href="/setup/wealthfolio">Wealthfolio verbinden' in r.text
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
    assert "<strong>3</strong> neu" in r.text
    assert 'Saldo <strong class="ok-text">stimmt</strong>' in r.text and "502,68" not in r.text  # no amounts (#83)
    assert len(wf.activities) == 7
    r = client.post("/run", data={"csrf": token})
    assert len(wf.activities) == 7  # no duplicates
    assert "FAKE_SPECIAL_EVENT" in client.get("/unknown").text

    # Automatic fetch off: still shown with its last result, fetched by its own button.
    r = client.post("/brokers/fake", data={"csrf": token, "cred_username": "max", "cash_account_id": "acc-cash",
                                            "portfolio_account_id": "acc-depot"})
    page = client.get("/").text
    assert "nur manuell" in page and "ohne automatischen Abruf nur einzeln" in page and "OK" in page
    assert 'name="broker" value="fake"' in page


def test_login_required_and_csrf_checked(tmp_path):
    client, _, _ = make(tmp_path)
    first_password(client)
    assert client.post("/run", data={"csrf": "forged"}).status_code == 400
    client.post("/logout", data={"csrf": csrf(client, "/")})
    r = client.get("/", follow_redirects=False)
    assert r.headers["location"] == "/login"
    assert "Falsches Passwort" in client.post("/login", data={"password": "wrong"}).text
    assert "Alle abrufen" in client.post("/login", data={"password": "Kiefer-Ozelot-9182"}).text
    # The password can only be chosen once.
    r = first_password(client, "andere-123456", follow_redirects=False)
    assert r.headers["location"] == "/login"


def test_notifications_and_securities(tmp_path):
    client, _, notifier = make(tmp_path)
    first_password(client)
    page = client.get("/notifications").text
    # 128 bits of chance (#98), and the same topic on every visit until saved - it was saved at once.
    topic = re.search(r'name="topic" value="(wealthfolio-sync-[A-Za-z0-9_-]{22})"', page).group(1)
    assert f'value="{topic}"' in client.get("/notifications").text
    token = csrf(client, "/notifications")
    r = client.post("/notifications", data={"csrf": token, "server": "https://ntfy.sh", "topic": topic,
                                            "details": "1"})
    assert "bitte ausdrücklich bestätigen" in r.text and not config_mod.load(tmp_path).ntfy_details
    client.post("/notifications", data={"csrf": token, "server": "https://ntfy.example", "topic": topic,
                                        "token": "tk_secret"})
    assert config_mod.load(tmp_path).ntfy_token == "tk_secret"
    client.post("/notifications", data={"csrf": token, "server": "https://ntfy.example", "topic": topic,
                                        "forget_token": "1"})
    assert config_mod.load(tmp_path).ntfy_token == ""
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
    first_password(client)
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

    first_password(client)
    page = client.get("/").text
    # Its activities stay in Wealthfolio: shown once, with the count and how to find them.
    assert "Dummy (Test) entfernt" in page and "4 Buchungen" in page and "[SYNC dummy:" in page
    assert "/brokers/dummy" not in page
    r = client.post("/retired/dismiss", data={"csrf": csrf(client, "/"), "broker": "dummy"})
    assert "Dummy (Test) entfernt" not in r.text
    # A restart finds nothing left to clean up and doesn't show it again.
    client, _, _ = make(tmp_path)
    client.post("/login", data={"password": "Kiefer-Ozelot-9182"})
    assert "Dummy (Test) entfernt" not in client.get("/").text
    assert "Unbekannter Broker" in client.get("/brokers/dummy").text


def test_refetch_button(tmp_path):
    from brokersync.state import State
    from brokersync.sync import refetch_flag

    client, _, _ = make(tmp_path)
    first_password(client)
    page = client.get("/brokers/fake").text
    assert "Ab Startdatum neu abrufen" in page
    r = client.post("/brokers/fake/refetch", data={"csrf": csrf(client, "/brokers/fake")})
    assert "Der nächste Abruf holt noch einmal alles, was der Broker liefert." in r.text
    assert State(tmp_path).flag(refetch_flag("fake"))


def test_navigation_groups_the_settings(tmp_path):
    from brokersync.state import State

    client, _, _ = make(tmp_path)
    first_password(client)
    page = client.get("/").text
    nav = re.search(r'<nav class="main" aria-label="Hauptmenü">(.*?)</nav>', page, re.S).group(1)
    assert re.findall(r'<a href="([^"]+)"', nav) == ["/", "/check", "/unknown", "/settings"]
    assert 'class="count"' not in nav and 'class="subnav"' not in page

    index = client.get("/settings").text
    for href in ("/setup/wealthfolio", "/brokers", "/notifications", "/securities", "/logos", "/security"):
        assert f'href="{href}"' in index
    assert 'href="/settings" aria-current="page"' in index
    # Hidden for now (waldonso2/wealthfolio-importer-addon#84): not listed, not served; *Überträge* is listed
    # only while patterns are saved (test_ui_sessions).
    assert 'href="/duplicates"' not in index and client.get("/duplicates").status_code == 404
    assert 'href="/transfers"' not in index

    page = client.get("/logos").text
    assert 'class="subnav"' in page and 'href="/logos" aria-current="page"' in page
    assert 'href="/settings" aria-current="page"' in page

    State(tmp_path).add_unknown("fake", "x1", "ODD", "2026-01-02", {"eventType": "ODD"})
    page = client.get("/unknown").text
    assert '<span class="count" aria-label="1 offen">1</span>' in page
    assert '<td data-label="Art">' in page  # stacked on narrow screens


def test_errors_are_pages_of_the_ui_and_odd_numbers_no_crash(tmp_path):
    """#106: an expired form and an unknown page are pages in the layout; Infinity/NaN in a form, a broken
    CSV and an odd amount in a template give a message, no 500."""
    from brokersync.adapters import deutschebank_csv
    from brokersync.web import views

    client, _, _ = make(tmp_path)
    first_password(client)
    r = client.post("/run", data={"csrf": "forged"})
    assert r.status_code == 400 and "Formular abgelaufen" in r.text and "<html" in r.text and "detail" not in r.text
    r = client.get("/no-such-page")
    assert r.status_code == 404 and "Seite nicht gefunden" in r.text
    token = csrf(client, "/check")
    for bad in ("Infinity", "NaN", "1e999999", "-0"):
        r = client.post("/check/fake/opening", data={"csrf": token, "isin": "DE0007164600", "day": "2026-01-02",
                                                     "shares": bad, "price": "10", "fee": "0"})
        assert r.status_code == 200 and "Zahlen" in r.text or "größer 0" in r.text
    for blob in (b"\x00;\x00\n\x00", b"\x81\x8d\x8f\x90\x9d"):
        with pytest.raises(ValueError):
            deutschebank_csv.parse(blob)
    assert views.money("n/a") == "n/a" and views.money("NaN") == "NaN" and views.money("1234.5") == "1.234,50"


def test_an_unexpected_error_shows_only_an_id(tmp_path, monkeypatch, caplog):
    from brokersync.web.routes import overview

    client, _, _ = make(tmp_path)
    first_password(client)

    def broken(*_args):
        raise RuntimeError("secret detail from a bank")

    monkeypatch.setattr(overview, "_brokers", broken)
    quiet = TestClient(client.app, base_url="http://sync.local:8090", raise_server_exceptions=False)
    quiet.cookies = client.cookies
    r = quiet.get("/")
    assert r.status_code == 500 and "secret detail" not in r.text
    error_id = re.search(r"Fehler-ID ([0-9a-f]{6})", r.text).group(1)
    assert error_id in caplog.text  # the details are in the log, found by the id


def test_a_wrong_code_is_counted_at_once_and_survives_a_restarted_login(tmp_path):
    """A lock-out counter (comdirect: wrong TANs) is stored right away: starting the login again, or the
    service restarting meanwhile, can't reset it."""
    client, _, _ = make(tmp_path)
    first_password(client)
    Vault(tmp_path).set_broker_credentials("fake", {"username": "test"})
    token = csrf(client, "/brokers/fake/login")
    client.post("/brokers/fake/login/start", data={"csrf": token})
    client.post("/brokers/fake/login", data={"csrf": token, "code": "999999"})
    assert Vault(tmp_path).broker("fake")["session"]["wrong_tans"] == 1
    client.post("/brokers/fake/login/start", data={"csrf": token})  # started again
    client.post("/brokers/fake/login", data={"csrf": token, "code": "999998"})
    assert Vault(tmp_path).broker("fake")["session"]["wrong_tans"] == 2


def test_showing_the_login_page_asks_the_broker_nothing_and_shows_an_open_login_again(tmp_path):
    """#99: a GET (reload, back, prefetch) never starts a login; an open one is shown, not replaced."""
    client, _, _ = make(tmp_path)
    first_password(client)
    Vault(tmp_path).set_broker_credentials("fake", {"username": "test"})
    page = client.get("/brokers/fake/login").text
    assert "Anmeldung starten" in page and "Bestätigungscode" not in page
    token = csrf(client, "/brokers/fake/login")
    client.post("/brokers/fake/login/start", data={"csrf": token})
    first = client.app.inner.state.ctx.pending["fake"]
    assert "Bestätigungscode" in client.get("/brokers/fake/login").text
    assert client.app.inner.state.ctx.pending["fake"] is first  # the same login, no new challenge
