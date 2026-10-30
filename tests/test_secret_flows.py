"""Where secrets go (SEC-06, #152): never into a page, a header or the session cookie, never readable in a backup,
masked in tracebacks; the encryption fails closed on a wrong key or a changed byte; the real argon2id cost."""

import base64
import gzip
import io
import json
import logging
import tarfile

import pytest
from fastapi.testclient import TestClient

from brokersync import backup, crypto, redact, vault
from brokersync import config as config_mod
from brokersync.adapters.base import CredentialField
from brokersync.vault import Vault, hash_password
from brokersync.wealthfolio import WealthfolioClient

from .fake_broker import FakeBroker
from .fakes import FakeWealthfolio
from .helpers import PASSWORD, first_password
from .test_attack_surface import routes, session_of
from .test_sync import RecordingNotifier, setup

PIN = "pin-secret-4711"
SESSION_TOKEN = "session-token-" + "s" * 30
LOGO = "pk_logo_secret_0815"
NTFY = "tk_ntfy_secret_0042"


class SecretBroker(FakeBroker):
    credential_fields = [*FakeBroker.credential_fields, CredentialField("pin", "PIN", secret=True)]


def test_no_page_header_or_cookie_carries_a_stored_secret(tmp_path, hidden_pages):
    from brokersync.web.app import create_app

    wf = FakeWealthfolio()
    client = TestClient(create_app(tmp_path, wealthfolio=lambda u, p: WealthfolioClient(u, p, transport=wf.transport()),
                                   adapters={"fake": SecretBroker}, notifier=RecordingNotifier(), run_in_thread=False),
                        base_url="http://sync.local:8090")
    first_password(client)
    v = Vault(tmp_path)
    v.update(lambda d: d.update(wealthfolio_password=wf.password, logo_dev_token=LOGO))
    v.set_broker_credentials("fake", {"username": "max", "pin": PIN})
    v.set_broker_session("fake", {"token": SESSION_TOKEN})

    def change(c):
        c.wealthfolio_url, c.ntfy_token = "http://wf.local:8080", NTFY
        c.brokers["fake"] = config_mod.BrokerConfig(cash_account_id="acc-cash", portfolio_account_id="acc-depot")

    config_mod.update(tmp_path, change)
    stored = v.load()
    secrets = [PASSWORD, wf.password, PIN, SESSION_TOKEN, LOGO, NTFY, stored["ui_session_key"],
               stored["ui_password_hash"]]
    answers = [client.request(m, p) for m, p in routes() if m == "GET"]
    # Forms that come back with what was typed: a typed secret never.
    answers.append(client.post("/brokers/fake", data={"csrf": session_of(client)["csrf"], "cred_pin": "typed-" + PIN,
                                                      "start_date": "kein Datum"}))
    answers.append(client.post("/setup/wealthfolio", data={"csrf": session_of(client)["csrf"],
                                                           "url": "http://wf.local:8080", "test": "1"}))
    assert answers[-2].status_code == 422
    assert sum(r.status_code == 200 and "<html" in r.text for r in answers) >= 25  # real pages, not redirects
    for r in answers:
        seen = r.text + json.dumps(dict(r.headers)) + json.dumps(session_of(client))
        for s in secrets:
            assert s not in seen, (str(r.url), s[:8])


def _logged(record_exc: BaseException, *, json_format: bool) -> str:
    from brokersync.cli import JsonFormatter

    out = io.StringIO()
    handler = logging.StreamHandler(out)
    if json_format:
        handler.setFormatter(JsonFormatter())
    handler.addFilter(redact.RedactFilter())
    log = logging.getLogger("brokersync.test-redact")
    log.addHandler(handler)
    try:
        log.error("run failed", exc_info=record_exc)
    finally:
        log.removeHandler(handler)
    return out.getvalue()


@pytest.mark.parametrize("json_format", [False, True])
def test_tracebacks_go_through_the_redaction(tmp_path, json_format):
    Vault(tmp_path).set_broker_credentials("fake", {"pin": PIN})
    Vault(tmp_path).load()
    try:
        raise RuntimeError(f"bank said no to {PIN} for DE02 5001 0517 0137 0750 30")
    except RuntimeError as e:
        text = _logged(e, json_format=json_format)
    assert "RuntimeError" in text and "Traceback" in text
    assert PIN not in text and "0137 0750" not in text and redact.MASK in text


def test_the_ntfy_token_is_masked_although_it_lives_outside_the_vault(tmp_path):
    config_mod.update(tmp_path, lambda c: setattr(c, "ntfy_token", NTFY))
    config_mod.load_notify(tmp_path)
    assert NTFY not in redact.redact(f"POST failed with {NTFY}")


def test_a_backup_holds_no_secret_in_plain_text(tmp_path):
    syncer, _ = setup(tmp_path, FakeWealthfolio())
    Vault(tmp_path).set_broker_credentials("fake", {"username": "max-mustermann", "pin": PIN})
    Vault(tmp_path).set_broker_session("fake", {"confirmed": True, "token": SESSION_TOKEN})
    config_mod.update(tmp_path, lambda c: setattr(c, "ntfy_token", NTFY))
    syncer.run()
    syncer.state.close()
    out = io.BytesIO()
    backup.write(tmp_path, out)
    raw = gzip.decompress(out.getvalue())
    with tarfile.open(fileobj=io.BytesIO(out.getvalue())) as tar:
        names = tar.getnames()
    assert "data/secret.key" not in names
    for s in (PIN, "max-mustermann", SESSION_TOKEN, NTFY, "wf-test-password", "acc-cash", "SQLite format 3"):
        assert s.encode() not in raw, s


def test_encryption_fails_closed(tmp_path, monkeypatch):
    key = crypto.data_key(tmp_path)
    a, b = crypto.seal(key, b"same", "secrets"), crypto.seal(key, b"same", "secrets")
    assert a[4:16] != b[4:16] and a != b  # a fresh nonce per seal
    for blob, purpose in ((a[:-1] + bytes([a[-1] ^ 1]), "secrets"),   # a changed byte of the tag
                          (a[:20] + bytes([a[20] ^ 1]) + a[21:], "secrets"),  # of the ciphertext
                          (a[:4] + bytes(12) + a[16:], "secrets"),  # another nonce
                          (a, "config"),                           # sealed for another purpose
                          (a[:10], "secrets"), (b"plain text", "secrets")):
        with pytest.raises(crypto.DecryptError):
            crypto.unseal(key, blob, purpose)
    other = crypto._data_key(tmp_path, None, base64.urlsafe_b64encode(b"o" * 32))
    with pytest.raises(crypto.DecryptError):
        crypto.unseal(other, a, "secrets")
    # A vault under another host key: an error, never empty data that a save would then overwrite.
    Vault(tmp_path).update(lambda d: d.__setitem__("wealthfolio_password", "pw"))
    key_file = tmp_path / "other.key"
    key_file.write_bytes(base64.urlsafe_b64encode(b"o" * 32))
    monkeypatch.setenv("COMPANION_KEY_FILE", str(key_file))
    with pytest.raises(vault.VaultError):
        Vault(tmp_path).load()
    with pytest.raises(vault.VaultError):
        Vault(tmp_path).update(lambda d: d.clear())
    monkeypatch.delenv("COMPANION_KEY_FILE")
    assert Vault(tmp_path).load()["wealthfolio_password"] == "pw"
    (tmp_path / "secrets.enc").write_bytes((tmp_path / "secrets.enc").read_bytes()[:-3])  # damaged on disk
    with pytest.raises(vault.VaultError):
        Vault(tmp_path).load()


def test_the_host_and_data_keys_are_separate(tmp_path):
    host, data = crypto.host_key(tmp_path), crypto.data_key(tmp_path)
    assert host != data and crypto.subkey(data, "secrets") != crypto.subkey(data, "config")
    blob = crypto.seal(host, b"x", "notify")
    with pytest.raises(crypto.DecryptError):
        crypto.unseal(data, blob, "notify")


def test_the_real_argon2id_cost(monkeypatch):
    monkeypatch.undo()  # the suite lowers the cost (conftest); these are the shipped values
    for params in (vault.PASSWORD_HASH, crypto.ARGON2):  # RFC 9106, second recommendation
        assert params["memory_cost"] >= 64 * 1024 and params["time_cost"] >= 3 and params["parallelism"] >= 1
    assert hash_password("ein Passwort zum Messen").startswith("$argon2id$v=19$m=65536,t=3,p=4$")
