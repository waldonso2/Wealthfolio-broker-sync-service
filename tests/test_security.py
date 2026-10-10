"""Everything the service stores is encrypted, the key is not in data/, the master passphrase, the brake on
wrong passwords, the headers, the log redaction, TLS - and a scan of data/ after a real run for anything
readable."""

import base64
import json
import logging
import sqlite3
import stat

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from brokersync import config as config_mod
from brokersync import crypto, redact, security
from brokersync import state as state_mod
from brokersync.state import State
from brokersync.vault import Vault, hash_password, needs_rehash, verify_password
from brokersync.wealthfolio import WealthfolioClient

from .fake_broker import FakeBroker
from .fakes import FakeWealthfolio
from .test_sync import RecordingNotifier, setup
from .test_web import csrf

PIN = "pin-7364-geheim"
UI_PW = "geheim-123456"


def client_for(tmp_path, *, secure=False):
    from brokersync.web.app import create_app

    wf = FakeWealthfolio()
    app = create_app(tmp_path, wealthfolio=lambda u, p: WealthfolioClient(u, p, transport=wf.transport()),
                     adapters={"fake": FakeBroker}, notifier=RecordingNotifier(), run_in_thread=False, secure=secure)
    return TestClient(app, base_url=("https" if secure else "http") + "://sync.local")


def logged_in(tmp_path, **kw):
    client = client_for(tmp_path, **kw)
    client.post("/setup-password", data={"password": UI_PW, "password2": UI_PW})
    return client


# ── what is on disk ─────────────────────────────────────────────────────────
def test_nothing_readable_in_data_after_a_run(tmp_path, caplog):
    wf = FakeWealthfolio()
    syncer, _ = setup(tmp_path, wf)
    Vault(tmp_path).set_broker_credentials("fake", {"username": "max-mustermann", "pin": PIN})
    Vault(tmp_path).set_broker_session("fake", {"confirmed": True, "anchor": "2026-01-05T15:00:00+00:00",
                                                "token": "tok-" + "x" * 40})
    cfg = config_mod.load(tmp_path)
    cfg.ntfy_topic, cfg.ntfy_token = "topic-very-secret", "ntfy-token-secret"
    cfg.transfer_patterns = [{"label": "Tagesgeld", "iban": "DE02500105170137075030", "keyword": "",
                              "destinationAccountId": ""}]
    config_mod.save(tmp_path, cfg)
    with caplog.at_level(logging.DEBUG):
        [r] = syncer.run()
    assert r.status == "ok"
    secrets = [PIN, "max-mustermann", "tok-" + "x" * 40, "topic-very-secret", "ntfy-token-secret",
               "DE02500105170137075030", "wf.local", "acc-cash"]
    for f in tmp_path.rglob("*"):
        if f.is_file() and f.name != "secret.key":  # the key itself: only here because tests have no systemd
            raw = f.read_bytes()
            assert not raw.startswith(b"SQLite format 3"), f.name
            for s in secrets:
                assert s.encode() not in raw, (f.name, s)
            assert stat.S_IMODE(f.stat().st_mode) & 0o077 == 0, f.name
    for s in secrets[:6]:
        assert s not in caplog.text


def test_an_installation_of_0_8_is_migrated(tmp_path):
    # What 0.8 left: the Fernet key in data/, secrets.enc as Fernet, config.json, a plain state.db.
    key = Fernet.generate_key()
    (tmp_path / "secret.key").write_bytes(key)
    old = {"wealthfolio_password": "wf-pw-alt", "ntfy_token": "tk-alt-123",
           "brokers": {"fake": {"credentials": {"pin": PIN}, "session": {}}}}
    (tmp_path / "secrets.enc").write_bytes(Fernet(key).encrypt(json.dumps(old).encode()))
    (tmp_path / "config.json").write_text(json.dumps({
        "wealthfolio_url": "http://wf.local:8080", "public_url": "http://192.168.1.51:8090",
        "ntfy_topic": "topic-alt", "brokers": {"fake": {"enabled": True}}}))
    db = sqlite3.connect(tmp_path / "state.db")
    db.executescript(state_mod.SCHEMA)
    db.execute("INSERT INTO synced (broker, tx_id, status, activity_ids, synced_at) "
               "VALUES ('fake', 't1', 'imported', '[]', '2026-01-01')")
    db.commit()
    db.close()

    done = security.migrate(tmp_path)
    assert len(done) == 4
    assert crypto.is_sealed((tmp_path / "secrets.enc").read_bytes())
    assert not (tmp_path / "config.json").exists() and not state_mod.is_plain(tmp_path / "state.db")
    assert Vault(tmp_path).load() == {"wealthfolio_password": "wf-pw-alt",
                                      "brokers": {"fake": {"credentials": {"pin": PIN}, "session": {}}}}
    cfg = config_mod.load(tmp_path)
    assert (cfg.ntfy_topic, cfg.ntfy_token, cfg.brokers["fake"].enabled) == ("topic-alt", "tk-alt-123", True)
    assert cfg.public_url == "https://192.168.1.51:8443"
    assert State(tmp_path).known("fake") == {"t1"}
    assert security.migrate(tmp_path) == []


def test_the_key_comes_from_systemd_and_is_required_once_data_exists(tmp_path, monkeypatch):
    creds = tmp_path / "creds"
    creds.mkdir()
    (creds / "brokersync-key").write_bytes(base64.urlsafe_b64encode(b"k" * 32))
    data = tmp_path / "data"
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(creds))
    monkeypatch.setenv("BROKERSYNC_KEY_SOURCE", "systemd-creds")
    Vault(data).update(lambda d: d.__setitem__("wealthfolio_password", "pw"))
    assert not (data / "secret.key").exists()
    assert crypto.host_key_source(data) == "systemd-creds"
    monkeypatch.delenv("CREDENTIALS_DIRECTORY")
    with pytest.raises(crypto.KeyMissing):
        Vault(data).load()


def test_a_file_cannot_be_swapped_for_another(tmp_path):
    config_mod.save(tmp_path, config_mod.Config(ntfy_topic="t"))
    (tmp_path / "secrets.enc").write_bytes((tmp_path / "config.enc").read_bytes())
    with pytest.raises(Exception, match="can't be decrypted"):
        Vault(tmp_path).load()


# ── master passphrase ───────────────────────────────────────────────────────
def test_with_a_passphrase_nothing_opens_without_it(tmp_path):
    wf = FakeWealthfolio()
    syncer, _ = setup(tmp_path, wf)
    syncer.run()
    syncer.state.close()
    security.change_passphrase(tmp_path, "eine sehr lange passphrase")
    assert Vault(tmp_path).load()["wealthfolio_password"]  # still unlocked in this process
    crypto.lock(tmp_path)
    assert crypto.is_locked(tmp_path)
    for read in (lambda: Vault(tmp_path).load(), lambda: config_mod.load(tmp_path), lambda: State(tmp_path)):
        with pytest.raises(crypto.Locked):
            read()
    # The host key alone (a copy of the container) doesn't open it either.
    with pytest.raises(crypto.DecryptError):
        crypto.unseal(crypto._data_key(tmp_path, None), (tmp_path / "secrets.enc").read_bytes(), "secrets")
    assert config_mod.load_notify(tmp_path).public_url == "http://sync.local:8090"  # host tier: readable
    assert not crypto.unlock(tmp_path, "falsch falsch falsch")
    assert crypto.unlock(tmp_path, "eine sehr lange passphrase")
    assert State(tmp_path).runs("fake")
    security.change_passphrase(tmp_path, None)
    crypto.lock(tmp_path)
    assert not crypto.is_locked(tmp_path) and State(tmp_path).runs("fake")
    with pytest.raises(ValueError, match="mindestens"):
        security.change_passphrase(tmp_path, "kurz")


def test_the_ui_sets_the_passphrase_locks_and_unlocks(tmp_path):
    client = logged_in(tmp_path)
    page = client.get("/security").text
    assert "Master-Passphrase" in page and "Passphrase setzen" in page
    token = csrf(client, "/security")
    r = client.post("/security/passphrase", data={"csrf": token, "password": "falsch-falsch", "action": "set",
                                                  "new": "x" * 14, "new2": "x" * 14})
    assert "stimmt nicht" in r.text and not crypto.passphrase_enabled(tmp_path)
    r = client.post("/security/passphrase", data={"csrf": token, "password": UI_PW, "action": "set",
                                                  "new": "meine passphrase!", "new2": "meine passphrase!"})
    assert "Master-Passphrase gesetzt" in r.text and crypto.passphrase_enabled(tmp_path)
    assert "Jetzt sperren" in client.get("/security").text  # still logged in, new keys
    r = client.post("/security/lock", data={"csrf": csrf(client, "/security")}, follow_redirects=False)
    assert r.headers["location"] == "/unlock"
    r = client.get("/brokers", follow_redirects=False)
    assert r.headers["location"] == "/unlock"
    assert client.post("/unlock", data={"passphrase": "nein"}).status_code == 403
    r = client.post("/unlock", data={"passphrase": "meine passphrase!"}, follow_redirects=False)
    assert r.headers["location"] == "/login"
    assert client.get("/brokers").status_code == 200


def test_a_locked_timer_run_says_so(tmp_path, monkeypatch):
    from brokersync import cli
    from brokersync.notify import Notifier

    config_mod.save(tmp_path, config_mod.Config(ntfy_topic="t", public_url="https://sync:8443"))
    security.change_passphrase(tmp_path, "eine sehr lange passphrase")
    crypto.lock(tmp_path)
    sent = []
    monkeypatch.setattr(Notifier, "send", lambda self, title, message, **kw: sent.append((title, kw["link"])))
    monkeypatch.setenv("BROKERSYNC_DATA", str(tmp_path))
    assert cli.main(["run"]) == 1
    assert sent == [("Broker Sync gesperrt", "https://sync:8443/unlock")]


# ── web UI ──────────────────────────────────────────────────────────────────
def test_wrong_passwords_are_braked(tmp_path):
    client = logged_in(tmp_path)
    client.post("/logout", data={"csrf": csrf(client, "/")})
    for _ in range(5):
        assert "Falsches Passwort" in client.post("/login", data={"password": "nein"}).text
    r = client.post("/login", data={"password": UI_PW})
    assert "Zu viele Fehlversuche" in r.text


def test_old_scrypt_hashes_still_work_and_are_upgraded(tmp_path):
    import hashlib

    salt = b"s" * 16
    old = f"scrypt${salt.hex()}${hashlib.scrypt(UI_PW.encode(), salt=salt, n=2**14, r=8, p=1).hex()}"
    Vault(tmp_path).update(lambda d: d.__setitem__("ui_password_hash", old))
    client = client_for(tmp_path)
    client.post("/login", data={"password": UI_PW})
    new = Vault(tmp_path).load()["ui_password_hash"]
    assert new.startswith("$argon2id$") and verify_password(UI_PW, new) and not needs_rehash(new)
    assert hash_password("x").startswith("$argon2id$")


def test_security_headers_and_secure_cookies(tmp_path):
    r = client_for(tmp_path).get("/login")
    assert "script-src 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY" and "strict-transport-security" not in r.headers
    secure = client_for(tmp_path, secure=True)
    r = secure.post("/setup-password", data={"password": UI_PW, "password2": UI_PW}, follow_redirects=False)
    assert "max-age=31536000" in r.headers["strict-transport-security"]
    assert "secure" in r.headers["set-cookie"].lower() and "samesite=strict" in r.headers["set-cookie"].lower()


def test_the_security_page_lists_the_checks(tmp_path):
    page = logged_in(tmp_path).get("/security").text
    for label in ("Schlüssel des Dienstes", "Zugangsdaten und Sitzungen", "Sync-Datenbank",
                  "Verbindung zur Oberfläche"):
        assert label in page
    assert "Liegt in data/" in page  # tests have no systemd: the key is in data/ and the page says so


# ── log, messages, notifications ────────────────────────────────────────────
def test_the_log_never_shows_secrets_or_account_numbers(tmp_path, caplog):
    Vault(tmp_path).set_broker_credentials("fake", {"pin": PIN})
    Vault(tmp_path).load()
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "login with %s for DE02 5001 0517 0137 0750 30 / %s",
                               (PIN, "1234567890"), None)
    redact.RedactFilter().filter(record)
    assert record.getMessage() == "login with *** for *** / ***"
    assert redact.redact("am 2026-10-10 um 12:00 für 19,14 EUR") == "am 2026-10-10 um 12:00 für 19,14 EUR"


def test_notifications_leave_out_details_unless_asked(monkeypatch):
    import httpx

    from brokersync.notify import Notifier

    bodies = []

    def handler(request):
        bodies.append(request.content.decode())
        return httpx.Response(200)

    Notifier("https://ntfy.example", "t", transport=httpx.MockTransport(handler), details=False).send(
        "DKB: Bestand weicht ab", "Cash EUR 8333.63 vs 9287.69")
    Notifier("https://ntfy.example", "t", transport=httpx.MockTransport(handler)).send(
        "x", "IBAN DE02500105170137075030")
    assert bodies == ["Details in der Weboberfläche.", "IBAN ***"]


# ── TLS ─────────────────────────────────────────────────────────────────────
def test_the_certificate_is_created_kept_and_renewed(tmp_path):
    from brokersync.tls import ensure_certificate

    d = tmp_path / "tls"
    assert "New self-signed" in ensure_certificate(d, ["sync"], ["192.168.1.51"])
    assert stat.S_IMODE((d / "key.pem").stat().st_mode) == 0o600
    assert "valid until" in ensure_certificate(d, ["sync"], ["192.168.1.51"])
    assert "New self-signed" in ensure_certificate(d, ["sync"], ["192.168.1.52"])  # new address


def test_plain_http_redirects_to_https():
    import asyncio

    from brokersync.web.serve import redirect_app

    sent = []

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "headers": [(b"host", b"192.168.1.51:8090")], "path": "/brokers/dkb",
             "raw_path": b"/brokers/dkb", "query_string": b"x=1"}
    asyncio.run(redirect_app(8443)(scope, None, send))
    assert sent[0]["status"] == 308
    assert dict(sent[0]["headers"])[b"location"] == b"https://192.168.1.51:8443/brokers/dkb?x=1"
