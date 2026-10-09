import base64
import json
import stat

import httpx

from brokersync.notify import Notifier
from brokersync.vault import Vault, hash_password, verify_password


def test_secrets_are_encrypted_at_rest(tmp_path):
    v = Vault(tmp_path)
    v.update(lambda d: d.__setitem__("wealthfolio_password", "super-secret-pw"))
    v.set_broker_credentials("dummy", {"pin": "13579"})
    raw = (tmp_path / "secrets.enc").read_bytes()
    assert b"super-secret-pw" not in raw and b"13579" not in raw
    assert stat.S_IMODE((tmp_path / "secret.key").stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "secrets.enc").stat().st_mode) == 0o600
    assert Vault(tmp_path).load()["wealthfolio_password"] == "super-secret-pw"


def test_new_credentials_reset_the_session(tmp_path):
    v = Vault(tmp_path)
    v.set_broker_session("dummy", {"confirmed": True})
    v.set_broker_credentials("dummy", {"username": "x"})
    assert v.broker("dummy") == {"credentials": {"username": "x"}, "session": {}}


def test_ui_password_hash():
    h = hash_password("correct horse")
    assert verify_password("correct horse", h)
    assert not verify_password("wrong", h)
    assert not verify_password("x", None)
    assert not verify_password("x", "garbage")


def test_ntfy_message_with_link_and_utf8_title():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"id": "1"})

    n = Notifier("https://ntfy.example/", "topic-1", "tk", transport=httpx.MockTransport(handler))
    assert n.send("TAN für Dummy fällig", "Bitte anmelden", link="http://sync/brokers/dummy/login", tags="key")
    r = seen[0]
    assert str(r.url) == "https://ntfy.example/topic-1"
    assert r.headers["click"] == "http://sync/brokers/dummy/login"
    assert r.headers["authorization"] == "Bearer tk"
    title = r.headers["title"]
    assert title.startswith("=?UTF-8?B?")
    assert base64.b64decode(title[10:-2]).decode() == "TAN für Dummy fällig"
    assert r.content.decode() == "Bitte anmelden"


def test_ntfy_failure_or_missing_topic_never_raises():
    def down(request):
        raise httpx.ConnectError("no route")

    assert not Notifier("https://ntfy.example", "t", transport=httpx.MockTransport(down)).send("a", "b")
    assert not Notifier("https://ntfy.example", "").send("a", "b")


def test_nothing_secret_in_the_plain_config(tmp_path):
    from brokersync import config as config_mod

    config_mod.save(tmp_path, config_mod.Config(ntfy_topic="t"))
    assert "password" not in json.loads((tmp_path / "config.json").read_text())
