"""The shared FinTS adapter: what every bank profile gets (login with app
confirmation or TAN, PIN safety, bank code, patterns, messages), checked for
DKB and for a second, fabricated profile that exists only here."""

import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from brokersync.adapters import ADAPTERS
from brokersync.adapters import fints as fints_mod
from brokersync.adapters.base import AdapterError, AuthRequired
from brokersync.adapters.dkb import DkbAdapter
from brokersync.adapters.fints import FintsAdapter, credential_fields
from brokersync.model import Kind

from .fake_fints import FakeFinTS

RECORDING = json.loads((Path(__file__).parent / "contract" / "dkb" / "giro.json").read_text())["recording"]


class ExampleBank(FintsAdapter):
    """A bank whose code differs per branch and whose posting texts DKB's patterns don't know."""

    key = "example"
    label = "Beispielbank"
    credential_fields = credential_fields("der Beispielbank", "Beispiel-Banking", blz=True)
    server = "https://fints.example.invalid/"
    bank = "die Beispielbank"
    app = "Beispiel-App"
    banking = "Beispiel-Banking"
    securities_pattern = re.compile(r"wp-kauf|wp-verkauf|ertrag", re.IGNORECASE)


PROFILES = {
    DkbAdapter: {"username": "max", "pin": "1234", "product_id": "TESTPRODUCT"},
    ExampleBank: {"username": "max", "pin": "1234", "product_id": "TESTPRODUCT", "blz": "100 700 00"},
}


@pytest.fixture
def opts(monkeypatch):
    """Options for the next FakeFinTS, and the log of (bank code, server, product id) it was opened with."""
    FakeFinTS.instances = []
    client_opts: dict = {}
    connected: list = []

    def factory(blz, user, pin, server, product_id, data):
        connected.append((blz, server, product_id))
        return FakeFinTS(RECORDING, **client_opts)

    monkeypatch.setattr(FintsAdapter, "client_factory", staticmethod(factory))
    monkeypatch.setattr(fints_mod, "DECOUPLED_POLL", 0)
    return client_opts, connected


def test_the_example_profile_is_not_a_bank_users_can_pick():
    assert ExampleBank.key not in ADAPTERS and FintsAdapter not in ADAPTERS.values()
    assert ADAPTERS["dkb"] is DkbAdapter


def test_each_profile_connects_to_its_bank(opts):
    _, connected = opts
    for cls, creds in PROFILES.items():
        with pytest.raises(AuthRequired):
            cls(creds).login()
    assert connected == [("12030000", "https://fints.dkb.de/fints", "TESTPRODUCT"),
                         ("10070000", "https://fints.example.invalid/", "TESTPRODUCT")]


def test_a_bank_code_per_branch_is_a_credential(opts):
    assert [f.name for f in ExampleBank.credential_fields] == ["username", "pin", "blz", "iban", "product_id"]
    assert "blz" not in [f.name for f in DkbAdapter.credential_fields]
    with pytest.raises(AdapterError, match="Bankleitzahl fehlt"):
        ExampleBank({**PROFILES[ExampleBank], "blz": ""}).login()


@pytest.mark.parametrize("cls", PROFILES, ids=lambda c: c.key)
def test_app_confirmation_uses_the_banks_app(opts, cls):
    a = cls(PROFILES[cls])
    told = []
    a.on_user_action = told.append
    a.login()
    assert told == [f"Bitte bestätige den Zugriff des Wealthfolio Broker Sync in der {cls.app}."]


@pytest.mark.parametrize("cls", PROFILES, ids=lambda c: c.key)
def test_a_tan_to_type_is_asked_in_the_web_ui_and_checked(opts, cls):
    client_opts, _ = opts
    client_opts["decoupled"] = False
    a = cls(PROFILES[cls])
    with pytest.raises(AuthRequired) as e:
        a.login()
    assert e.value.challenge.kind == "code"
    with pytest.raises(AuthRequired) as again:
        a.complete_login("000000")
    assert again.value.challenge.kind == "code"
    a.complete_login("123456")
    a.get_cash()  # the dialog is open


@pytest.mark.parametrize("cls", PROFILES, ids=lambda c: c.key)
def test_a_rejected_pin_is_never_tried_again(opts, cls):
    client_opts, connected = opts
    client_opts["pin_ok"] = False
    a = cls(PROFILES[cls])
    with pytest.raises(AdapterError, match=f"{cls.bank[4:]} hat Anmeldename oder PIN abgelehnt"):
        a.login()
    count = len(connected)
    with pytest.raises(AdapterError, match="nicht noch einmal"):
        cls(PROFILES[cls], a.session_state()).login()
    assert len(connected) == count


def test_messages_name_the_bank(opts, monkeypatch):
    client_opts, _ = opts
    client_opts["sca"] = False
    a = ExampleBank({**PROFILES[ExampleBank], "iban": "DE00 0000 0000 0000 0000 00"})
    a.login()
    with pytest.raises(AdapterError, match="gehört nicht zu diesem Beispielbank-Zugang"):
        a.get_cash()
    monkeypatch.setattr(FakeFinTS, "get_sepa_accounts", lambda self: [])
    with pytest.raises(AdapterError, match="^Die Beispielbank meldet kein Konto"):
        a.get_cash()

    def down(self):
        raise ConnectionError("server unreachable")

    monkeypatch.setattr(FakeFinTS, "__enter__", down)
    with pytest.raises(AdapterError, match=r"^Beispielbank \(FinTS\): server unreachable"):
        ExampleBank(PROFILES[ExampleBank]).login()


def test_each_profile_classifies_with_its_own_patterns():
    records = [
        {"amount": fints_mod._Amount("-1000.00", "EUR"), "date": datetime(2026, 9, 1).date(),
         "posting_text": "WP-KAUF", "purpose": "Depot 123 Stk 10"},
        {"amount": fints_mod._Amount("-1000.00", "EUR"), "date": datetime(2026, 9, 2).date(),
         "posting_text": "Wertpapiere", "purpose": "Kauf Depot 123"},
    ]
    dkb = fints_mod.to_transactions(records, DkbAdapter.securities_pattern)
    example = fints_mod.to_transactions(records, ExampleBank.securities_pattern)
    assert [t.kind for t in dkb] == [Kind.SECURITIES_CASH, Kind.SECURITIES_CASH]  # "depot" in both
    assert [t.kind for t in example] == [Kind.SECURITIES_CASH, Kind.WITHDRAWAL]
    # Same booking, same id, whatever the profile: the id depends on the booking only.
    assert [t.id for t in dkb] == [t.id for t in example]


def test_replay_works_for_every_profile():
    since = datetime(2026, 8, 1, tzinfo=UTC)
    assert [t.id for t in ExampleBank.replay(RECORDING).get_transactions(since)] == \
        [t.id for t in DkbAdapter.replay(RECORDING).get_transactions(since)]
