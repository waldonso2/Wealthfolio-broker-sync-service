"""Everything the service stores is encrypted, the key is not in data/, the master passphrase, the brake on
wrong passwords, the headers, the log redaction, TLS - and a scan of data/ after a real run for anything
readable."""

import base64
import json
import logging
import os
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
from .helpers import first_password
from .test_sync import RecordingNotifier, setup
from .test_web import csrf

PIN = "pin-7364-geheim"
UI_PW = "Kiefer-Ozelot-9182"


def client_for(tmp_path, *, secure=False):
    from brokersync.web.app import create_app

    wf = FakeWealthfolio()
    app = create_app(tmp_path, wealthfolio=lambda u, p: WealthfolioClient(u, p, transport=wf.transport()),
                     adapters={"fake": FakeBroker}, notifier=RecordingNotifier(), run_in_thread=False, secure=secure)
    return TestClient(app, base_url=("https" if secure else "http") + "://sync.local")


def logged_in(tmp_path, **kw):
    client = client_for(tmp_path, **kw)
    first_password(client, UI_PW)
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


def test_error_texts_are_stored_and_shown_masked(tmp_path):
    client = logged_in(tmp_path)
    Vault(tmp_path).update(lambda d: d.__setitem__("wealthfolio_password", "wf-pw-very-secret"))
    cfg = config_mod.load(tmp_path)
    cfg.brokers["fake"] = config_mod.BrokerConfig(enabled=True)
    config_mod.save(tmp_path, cfg)
    state = State(tmp_path)
    run = state.start_run("fake")
    text = "Bank: Konto DE02500105170137075030 gesperrt, Auftrag 123456789; login wf-pw-very-secret rejected"
    state.finish_run(run, "error", message=text)
    stored = state.last_run("fake").message
    assert "DE02500105170137075030" not in stored and "123456789" not in stored and "wf-pw-very-secret" not in stored
    # Runs stored by an older version: masked when shown.
    with state.db:
        state.db.execute("UPDATE runs SET message = ? WHERE id = ?", (text, run))
    page = client.get("/").text
    assert "gesperrt" in page
    for s in ("DE02500105170137075030", "123456789", "wf-pw-very-secret"):
        assert s not in page


def test_request_urls_are_not_logged():
    from brokersync import cli

    cli.setup_logging()
    assert logging.getLogger("httpx").getEffectiveLevel() == logging.WARNING
    assert logging.getLogger("httpcore").getEffectiveLevel() == logging.WARNING


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


def test_a_write_during_a_passphrase_change_is_kept_or_refused_never_lost(tmp_path, monkeypatch):
    """#120: the UI writes to state.db while the passphrase changes (*Ignorieren* in another tab)."""
    import threading

    syncer, _ = setup(tmp_path, FakeWealthfolio())
    syncer.run()
    ui = State(tmp_path)  # the web UI's, opened before
    outcome = []

    def write():
        try:
            ui.set_meta("x", "1")
            outcome.append("written")
        except state_mod.StateReplaced:
            outcome.append("refused")

    stage = security._stage_data_tier
    writer = threading.Thread(target=write)

    def staging(*args):
        writer.start()  # it waits for the change, it doesn't write into the copy being made
        writer.join(0.2)
        assert writer.is_alive()
        stage(*args)

    monkeypatch.setattr(security, "_stage_data_tier", staging)
    security.change_passphrase(tmp_path, "eine sehr lange passphrase")
    writer.join(5)
    assert outcome == ["refused"]  # the user gets an error and does it again
    assert State(tmp_path).meta("x") is None
    State(tmp_path).set_meta("x", "1")  # with the new file: fine


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
    # Removing it takes a second, deliberate step (#110).
    token = csrf(client, "/security")
    r = client.post("/security/passphrase", data={"csrf": token, "password": UI_PW, "action": "remove"})
    assert "bitte bestätigen" in r.text and crypto.passphrase_enabled(tmp_path)
    client.post("/security/passphrase", data={"csrf": token, "password": UI_PW, "action": "remove",
                                              "confirm_remove": "1"})
    assert not crypto.passphrase_enabled(tmp_path)


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


def test_parallel_attempts_count_before_the_check_and_are_capped():
    import threading

    from brokersync.web.gate import Throttle

    th = Throttle(limit=5, slots=2)
    release, inside = threading.Event(), []

    def slow_wrong():
        with th.attempt("10.0.0.9") as a:
            if not a.refused:
                inside.append(1)
                release.wait(2)

    threads = [threading.Thread(target=slow_wrong) for _ in range(8)]
    for t in threads:
        t.start()
    while len(inside) < 2:
        pass
    with th.attempt("10.0.0.9") as a:
        assert a.busy  # both check slots taken: no third argon2id run
    release.set()
    for t in threads:
        t.join()
    assert len(inside) <= 2
    for _ in range(3):  # the two that ran are counted already
        with th.attempt("10.0.0.9"):
            pass
    with th.attempt("10.0.0.9") as a:
        assert a.wait > 0 and not a.busy


def test_the_brake_keeps_ipv6_neighbours_together_has_a_global_limit_and_a_size_limit():
    from brokersync.web.gate import Throttle

    th = Throttle(limit=3, global_limit=6, max_clients=4)
    for i in range(3):
        with th.attempt(f"2001:db8::{i + 1}"):
            pass
    with th.attempt("2001:db8::ff") as a:  # the same /64
        assert a.wait
    for i in range(3):
        with th.attempt(f"10.0.0.{i + 1}"):
            pass
    with th.attempt("10.0.0.99") as a:  # six wrong ones in all: nobody for a while
        assert a.wait
    th = Throttle(limit=3, global_limit=1000, max_clients=4)
    for i in range(10):
        with th.attempt(f"10.0.1.{i}"):
            pass
    assert len(th._fails) <= 4
    with th.attempt("10.0.2.1") as a:
        a.ok = True
    assert "10.0.2.1" not in th._fails  # a right one leaves nothing behind


def test_behind_a_named_proxy_its_forwarded_header_is_believed(monkeypatch):
    from brokersync.web.serve import proxy_options

    monkeypatch.delenv("BROKERSYNC_TRUST_PROXY", raising=False)
    assert proxy_options() == {"proxy_headers": False}
    monkeypatch.setenv("BROKERSYNC_TRUST_PROXY", "192.168.1.2")
    assert proxy_options() == {"proxy_headers": True, "forwarded_allow_ips": "192.168.1.2"}


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
    r = first_password(secure, UI_PW, follow_redirects=False)
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
def _verify(ca_pem: bytes, chain_pem: bytes, name):
    from cryptography import x509
    from cryptography.x509.verification import PolicyBuilder, Store

    ca = x509.load_pem_x509_certificate(ca_pem)
    leaf, *rest = x509.load_pem_x509_certificates(chain_pem)
    return PolicyBuilder().store(Store([ca])).build_server_verifier(name).verify(leaf, rest)


def test_the_certificate_comes_from_the_containers_ca_and_browsers_can_verify_it(tmp_path):
    import ipaddress

    from cryptography import x509

    from brokersync.tls import ensure_certificate

    d = tmp_path / "tls"
    out = ensure_certificate(d, ["wealthfolio-broker-sync"], ["192.168.178.110", "2003:ce::1", "fd1e::1", "nonsense"])
    assert "New certificate" in out and "CA SHA-256" in out
    for f in ("ca-key.pem", "key.pem", "cert.pem", "ca.pem"):
        assert stat.S_IMODE((d / f).stat().st_mode) == 0o600
    ca_pem, chain = (d / "ca.pem").read_bytes(), (d / "cert.pem").read_bytes()
    _verify(ca_pem, chain, x509.IPAddress(ipaddress.ip_address("192.168.178.110")))
    _verify(ca_pem, chain, x509.DNSName("wealthfolio-broker-sync"))
    leaf = x509.load_pem_x509_certificate(chain)
    ips = {str(i) for i in leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
           .get_values_for_type(x509.IPAddress)}
    assert ips == {"192.168.178.110", "fd1e::1", "127.0.0.1"}  # no public address in a local certificate
    # Kept while it fits; a new address gets a new certificate from the same CA.
    assert "valid until" in ensure_certificate(d, ["wealthfolio-broker-sync"], ["192.168.178.110"])
    assert "New certificate" in ensure_certificate(d, ["wealthfolio-broker-sync"], ["192.168.178.111"])
    assert (d / "ca.pem").read_bytes() == ca_pem


def test_the_ca_cannot_vouch_for_a_public_site(tmp_path):
    from datetime import UTC, datetime, timedelta

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    from brokersync.tls import ensure_certificate

    d = tmp_path / "tls"
    ensure_certificate(d, ["sync"], ["192.168.1.5"])
    ca = x509.load_pem_x509_certificate((d / "ca.pem").read_bytes())
    ca_key = serialization.load_pem_private_key((d / "ca-key.pem").read_bytes(), None)
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(UTC)
    forged = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "bank")]))
              .issuer_name(ca.subject).public_key(key.public_key()).serial_number(1)
              .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=30))
              .add_extension(x509.SubjectAlternativeName([x509.DNSName("www.deutsche-bank.de")]), critical=False)
              .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
              .sign(ca_key, hashes.SHA256()))
    with pytest.raises(Exception, match="(?i)constraint|name"):
        _verify((d / "ca.pem").read_bytes(), forged.public_bytes(serialization.Encoding.PEM),
                x509.DNSName("www.deutsche-bank.de"))


def test_an_old_self_signed_certificate_is_replaced_and_a_foreign_one_kept(tmp_path):
    from cryptography import x509

    from brokersync.tls import ensure_certificate

    d = tmp_path / "tls"
    ensure_certificate(d, ["sync"], ["192.168.1.5"])
    # 0.9.0 wrote a self-signed certificate: make one (the CA itself is self-signed) and let it be replaced.
    (d / "cert.pem").write_bytes((d / "ca.pem").read_bytes())
    (d / "key.pem").write_bytes((d / "ca-key.pem").read_bytes())
    assert "New certificate" in ensure_certificate(d, ["sync"], ["192.168.1.5"])
    # A certificate of another issuer stays.
    other = tmp_path / "other"
    ensure_certificate(other, ["sync"], ["192.168.1.5"])
    (d / "cert.pem").write_bytes((other / "cert.pem").read_bytes())
    assert "Keeping" in ensure_certificate(d, ["sync"], ["192.168.1.5"])
    assert (d / "cert.pem").read_bytes() == (other / "cert.pem").read_bytes()
    assert x509.load_pem_x509_certificate((d / "ca.pem").read_bytes())  # our CA stays as it was


def test_the_ca_certificate_can_be_downloaded_and_is_explained(tmp_path, monkeypatch):
    from brokersync.tls import ensure_certificate

    ensure_certificate(tmp_path / "tls", ["sync"], ["192.168.1.5"])
    creds = tmp_path / "creds"
    creds.mkdir()
    (creds / "tls.ca").write_bytes((tmp_path / "tls" / "ca.pem").read_bytes())
    (creds / "tls.crt").write_bytes((tmp_path / "tls" / "cert.pem").read_bytes())
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(creds))
    monkeypatch.setenv("BROKERSYNC_KEY_FILE", str(tmp_path / "tls" / "ca-key.pem"))  # any key will do here
    client = logged_in(tmp_path / "data", secure=True)
    r = client_for(tmp_path / "data", secure=True).get("/ca.crt")  # no login needed
    assert r.status_code == 200 and r.content == (creds / "tls.ca").read_bytes()
    assert r.headers["content-type"] == "application/x-x509-ca-cert"
    page = client.get("/security").text
    assert "CA-Zertifikat herunterladen" in page and security.ca_fingerprint() in page


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


# ── key rotation ────────────────────────────────────────────────────────────
@pytest.mark.parametrize("passphrase", [None, "eine sehr lange passphrase"])
def test_a_new_host_key_re_encrypts_everything_and_the_old_one_no_longer_opens_it(tmp_path, monkeypatch, passphrase):
    old_key, new_key = tmp_path / "old.key", tmp_path / "new.key"
    old_key.write_bytes(base64.urlsafe_b64encode(b"o" * 32))
    new_key.write_bytes(base64.urlsafe_b64encode(b"n" * 32))
    data = tmp_path / "data"
    monkeypatch.setenv("BROKERSYNC_KEY_FILE", str(old_key))
    wf = FakeWealthfolio()
    syncer, _ = setup(data, wf)
    syncer.run()
    syncer.state.close()
    if passphrase:
        security.change_passphrase(data, passphrase)
    security.rotate_host_key(data, new_key.read_bytes())
    # Only staged: until setup.sh stores the new key, the old one opens everything (#91).
    assert Vault(data).load()["wealthfolio_password"] and security.recover(data) == "undone"
    security.rotate_host_key(data, new_key.read_bytes())
    monkeypatch.setenv("BROKERSYNC_KEY_FILE", str(new_key))  # setup.sh stored it: the switch
    assert security.recover(data) == "finished" and not list(data.glob("*.rekey"))
    monkeypatch.setenv("BROKERSYNC_KEY_FILE", str(old_key))
    with pytest.raises(Exception):  # noqa: B017 - the old key opens nothing any more
        Vault(data).load()
    monkeypatch.setenv("BROKERSYNC_KEY_FILE", str(new_key))
    assert Vault(data).load()["wealthfolio_password"]
    assert config_mod.load(data).brokers["fake"].enabled and config_mod.load_notify(data).public_url
    assert State(data).runs("fake")
    if passphrase:
        crypto.lock(data)
        assert crypto.unlock(data, passphrase) and State(data).runs("fake")


def test_rotation_needs_the_service_unlocked_and_a_real_key(tmp_path, monkeypatch, capsys):
    import io

    from brokersync import cli

    monkeypatch.setenv("BROKERSYNC_DATA", str(tmp_path))
    config_mod.save(tmp_path, config_mod.Config())
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(b"short\n")))
    assert cli.main(["rotate-key"]) == 1 and "zu kurz" in capsys.readouterr().err
    security.change_passphrase(tmp_path, "eine sehr lange passphrase")
    crypto.lock(tmp_path)
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(base64.urlsafe_b64encode(b"n" * 32))))
    assert cli.main(["rotate-key"]) == 1 and "gesperrt" in capsys.readouterr().err


def test_unknown_events_keep_no_account_or_depot_numbers(tmp_path):
    payload = {"betrag": "17.95", "buchungstext": "WERTPAPIERE",
               "verwendungszweck": "Depot 0000999888 Wertpapierertrag 19.08.2026 000012345678901 WKN A0TEST "
                                   "TEST FONDS ISIN LU0000000001 IBAN DE02 1203 0000 0000 2020 51"}
    st = state_mod.State(tmp_path)
    for _ in range(2):  # new, then the update of a known one
        st.add_unknown("dkb", "tx1", "UNMATCHED", "2026-08-20T10:00:00+00:00", payload)
        [event] = st.unknown_events("dkb")
        text = event["payload"]["verwendungszweck"]
        assert text == "Depot *** Wertpapierertrag 19.08.2026 *** WKN A0TEST TEST FONDS ISIN LU0000000001 IBAN ***"
        assert event["payload"]["betrag"] == "17.95"
    st.close()


# ── crash safety of the re-encryption (#91) ─────────────────────────────────
def _filled(tmp_path, monkeypatch):
    key = tmp_path / "host.key"
    key.write_bytes(base64.urlsafe_b64encode(b"h" * 32))
    monkeypatch.setenv("BROKERSYNC_KEY_FILE", str(key))
    data = tmp_path / "data"
    syncer, _ = setup(data, FakeWealthfolio())
    syncer.run()
    syncer.state.close()
    return data


def _all_readable(data) -> None:
    assert Vault(data).load()["wealthfolio_password"]
    assert config_mod.load(data).brokers["fake"].enabled and config_mod.load_notify(data).public_url
    st = State(data)
    assert st.runs("fake")
    st.close()


@pytest.mark.parametrize("crash_at", range(1, 13))  # staging, the journal and every swap
@pytest.mark.parametrize("old,new", [(None, "eine sehr lange passphrase"), ("eine sehr lange passphrase", None)])
def test_a_passphrase_change_survives_a_crash_at_any_point(tmp_path, monkeypatch, crash_at, old, new):
    data = _filled(tmp_path, monkeypatch)
    if old:
        security.change_passphrase(data, old)
    real = os.replace
    calls = {"n": 0}

    def crashing(src, dst):
        calls["n"] += 1
        if calls["n"] == crash_at:
            raise OSError("power cut")
        return real(src, dst)

    monkeypatch.setattr(security.os, "replace", crashing)
    monkeypatch.setattr(crypto.os, "replace", crashing)
    try:
        security.change_passphrase(data, new)
    except OSError:
        pass
    monkeypatch.setattr(security.os, "replace", real)
    monkeypatch.setattr(crypto.os, "replace", real)
    security.recover(data)
    assert not list(data.glob("*.rekey")) and not (data / security.JOURNAL).exists()
    for passphrase in (new, old):  # one of the two opens all of it - never a mix
        crypto.lock(data)
        if passphrase and not crypto.unlock(data, passphrase):
            continue
        try:
            _all_readable(data)
            break
        except Exception:
            continue
    else:
        raise AssertionError("the data opens with neither passphrase")


def test_state_db_is_never_re_encrypted_in_place(tmp_path, monkeypatch):
    data = _filled(tmp_path, monkeypatch)
    paths = []
    real = state_mod.rekey
    monkeypatch.setattr(state_mod, "rekey", lambda d, o, n, path=None: (paths.append(path), real(d, o, n, path))[1])
    security.change_passphrase(data, "eine sehr lange passphrase")
    assert paths and all(p is not None and p.name != "state.db" for p in paths)


def test_a_new_host_key_that_was_never_stored_is_undone_on_the_next_start(tmp_path, monkeypatch):
    from brokersync import cli

    data = _filled(tmp_path, monkeypatch)
    security.rotate_host_key(data, base64.urlsafe_b64encode(b"n" * 32))
    monkeypatch.setenv("BROKERSYNC_DATA", str(data))
    assert cli.main(["recover"]) == 0
    assert not list(data.glob("*.rekey")) and not (data / security.JOURNAL).exists()
    _all_readable(data)


def test_old_sessions_do_not_pile_up_in_the_redaction(tmp_path):
    """#114: every new session replaces the old one's strings; credentials stay registered for good."""
    vault = Vault(tmp_path)
    vault.set_broker_credentials("fake", {"username": "dauerhaft-geheim"})
    for i in range(1000):
        vault.set_broker_session("fake", {"token": f"session-token-{i:04d}-" + "x" * 20})
        vault.load()
    current = "session-token-0999-" + "x" * 20
    assert redact.redact(f"a {current} b") == "a *** b"
    assert redact.redact("user dauerhaft-geheim") == "user ***"
    assert len(redact._scoped["session:fake"]) == 1
    assert "session-token-0001-" + "x" * 20 not in redact._all()


# ── hardening (#107) ────────────────────────────────────────────────────────
def test_only_known_names_reach_the_ui_and_the_redirect(tmp_path, monkeypatch):
    import asyncio

    from brokersync.web.gate import Hosts
    from brokersync.web.serve import known_hosts, redirect_app

    monkeypatch.setenv("BROKERSYNC_ALLOWED_HOSTS", "sync.example.org")
    config_mod.save(tmp_path, config_mod.Config(public_url="https://depot.home.arpa:8443"))
    names = known_hosts(tmp_path, None)
    assert {"localhost", "sync.example.org", "depot.home.arpa"} <= names
    hosts = Hosts(lambda: names | {"wealthfolio-broker-sync"})
    for ok in ("192.168.1.51:8443", "[fd00::5]:8443", "localhost", "wealthfolio-broker-sync.fritz.box:8443",
               "wealthfolio-broker-sync", "sync.example.org"):
        assert hosts.allows(ok), ok
    for bad in ("evil.example", "wealthfolio-broker-sync.evil.example", "", "depot.home.arpa.evil.example"):
        assert not hosts.allows(bad), bad

    from brokersync.web.app import create_app

    app = create_app(tmp_path, hosts=hosts)
    r = TestClient(app, base_url="http://evil.example").get("/login")
    assert r.status_code == 400 and "BROKERSYNC_ALLOWED_HOSTS" in r.text
    assert TestClient(app, base_url="http://192.168.1.51:8090").get("/healthz").status_code == 200
    sent = []

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "path": "/", "raw_path": b"/", "query_string": b"",
             "headers": [(b"host", b"evil.example:8090")]}
    asyncio.run(redirect_app(8443, hosts)(scope, None, send))
    assert sent[0]["status"] == 400 and not any(k == b"location" for k, _ in sent[0]["headers"])


def test_no_inline_styles_and_the_health_check_says_little(tmp_path):
    from pathlib import Path

    from brokersync.web import gate

    assert "unsafe-inline" not in gate.CSP
    templates = Path(gate.__file__).parent / "templates"
    assert not [f.name for f in templates.glob("*.html") if "style=" in f.read_text()]
    client = client_for(tmp_path)
    r = client.get("/healthz")
    assert r.json() == {"ok": True} and r.headers["cross-origin-resource-policy"] == "same-origin"
    assert "version" in logged_in(tmp_path / "other").get("/healthz").json()


def test_the_units_are_hardened_and_the_run_has_a_time_limit():
    from pathlib import Path

    units = Path(__file__).parent.parent / "deploy" / "systemd"
    for name in ("wealthfolio-broker-sync.service", "wealthfolio-broker-sync-run.service"):
        text = (units / name).read_text()
        for option in ("SystemCallFilter=@system-service", "SystemCallErrorNumber=EPERM", "ProtectProc=invisible",
                       "RemoveIPC=true", "NoNewPrivileges=true", "CapabilityBoundingSet=\n"):
            assert option in text, (name, option)
    assert "TimeoutStartSec=45min" in (units / "wealthfolio-broker-sync-run.service").read_text()


def test_a_cookie_of_an_earlier_password_signs_in_nobody(tmp_path):
    """brokersync-reset-password while the web UI runs: a copied cookie of before is not valid again once a
    new password is set - the session is bound to the password."""
    client = logged_in(tmp_path)
    stolen = dict(client.cookies)
    import os

    from brokersync import cli
    os.environ["BROKERSYNC_DATA"] = str(tmp_path)
    try:
        cli.main(["reset-ui-password"])
    finally:
        os.environ.pop("BROKERSYNC_DATA")
    first_password(client, "ein-ganz-neues-pw")
    thief = TestClient(client.app, base_url="http://sync.local", cookies=stolen)
    assert thief.get("/", follow_redirects=False).headers["location"] == "/login"
    assert client.get("/", follow_redirects=False).status_code == 200


def test_the_access_log_stays_off_the_setup_code_can_be_in_a_url():
    from pathlib import Path

    serve = (Path(__file__).parent.parent / "src" / "brokersync" / "web" / "serve.py").read_text()
    assert serve.count("access_log=False") == 2
