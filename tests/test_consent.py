"""Agreeing to an unofficial interface before the first login (#112), and the FinTS product id's note."""

from fastapi.testclient import TestClient

from brokersync import config as config_mod
from brokersync.adapters.dkb import DkbAdapter
from brokersync.adapters.tr import TradeRepublicAdapter
from brokersync.sync import Syncer
from brokersync.vault import Vault

from .helpers import first_password
from .test_web import csrf


def client_for(tmp_path):
    from brokersync.web.app import create_app

    app = create_app(tmp_path, adapters={"tr": TradeRepublicAdapter}, run_in_thread=False)
    return TestClient(app, base_url="http://sync.local:8090")


def test_no_trade_republic_login_without_agreeing(tmp_path):
    client = client_for(tmp_path)
    first_password(client)
    page = client.get("/brokers/tr").text
    assert "keine offizielle Schnittstelle" in page and 'name="consent"' in page
    token = csrf(client, "/brokers/tr")
    form = {"csrf": token, "cred_phone": "+4917600000000", "cred_pin": "1234"}
    r = client.post("/brokers/tr", data=form)
    assert r.status_code == 422 and 'id="err-consent"' in r.text and not Vault(tmp_path).broker("tr")
    # Saved without credentials, a login can't be started either.
    config_mod.update(tmp_path, lambda c: c.broker("tr"))
    r = client.post("/brokers/tr/login/start", data={"csrf": token})
    assert "Bitte bestätige, dass du den Hinweis zur Schnittstelle gelesen hast" in r.text
    assert not config_mod.load(tmp_path).broker("tr").consented

    r = client.post("/brokers/tr", data={**form, "consent": "1"}, follow_redirects=False)
    assert config_mod.load(tmp_path).broker("tr").consented and Vault(tmp_path).broker("tr")["credentials"]
    assert "Hinweis zur Schnittstelle bestätigt" in client.get("/brokers/tr").text


def test_a_broker_set_up_before_counts_as_agreed(tmp_path):
    Vault(tmp_path).set_broker_credentials("tr", {"phone": "+4917600000000", "pin": "1234"})
    assert not config_mod.load(tmp_path).broker("tr").consented
    Syncer(tmp_path, adapters={"tr": TradeRepublicAdapter})
    assert config_mod.load(tmp_path).broker("tr").consented


def test_the_product_id_field_says_whose_id_belongs_there():
    field = next(f for f in DkbAdapter.credential_fields if f.name == "product_id")
    assert "eigene, bei der Deutschen Kreditwirtschaft registrierte" in field.help
