"""Banks over PSD2 through Enable Banking (waldonso2/wealthfolio-importer-addon#137): JWT, login with the
pasted redirect address, bookings and balance. The key is made here; no key is in the repository."""

import base64
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from brokersync.adapters import ADAPTERS
from brokersync.adapters import enablebanking as eb
from brokersync.adapters.base import AdapterError, AuthRequired
from brokersync.model import Kind

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PEM = KEY.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                        serialization.NoEncryption()).decode()
CREDS = {"bank": "n26", "app_id": "app-123", "private_key": PEM.replace("\n", " ")}  # as a one-line field keeps it


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def test_the_token_is_signed_with_the_apps_key():
    head, body, sig = eb.jwt("app-123", CREDS["private_key"], now=1000).split(".")
    assert json.loads(_unb64(head)) == {"typ": "JWT", "alg": "RS256", "kid": "app-123"}
    assert json.loads(_unb64(body)) == {"iss": "enablebanking.com", "aud": "api.enablebanking.com", "iat": 1000,
                                        "exp": 4600}
    KEY.public_key().verify(_unb64(sig), f"{head}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256())
    with pytest.raises(AdapterError, match="kein PEM"):
        eb.jwt("app-123", "not a key")


class Bank:
    """A fake Enable Banking: records the calls, answers them."""

    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []
        self.status = 200

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        self.calls.append((request.method, request.url.path, body))
        assert request.headers["authorization"].startswith("Bearer ey")
        path = request.url.path
        if path == "/aspsps":
            return httpx.Response(200, json={"aspsps": [{"name": "N26", "country": "DE",
                                                         "maximum_consent_validity": 180 * 86400},
                                                        {"name": "N26 Business", "country": "DE"}]})
        if path == "/application":
            return httpx.Response(200, json={"redirect_urls": ["https://sync.local:8443/psd2"]})
        if path == "/auth":
            return httpx.Response(200, json={"url": "https://bank.example.invalid/consent", "authorization_id": "a"})
        if path == "/sessions":
            return httpx.Response(200, json={
                "session_id": "s-1", "access": {"valid_until": "2099-01-01T00:00:00+00:00"},
                "accounts": [{"uid": "u-savings", "account_id": {"iban": "DE00SAV"}, "cash_account_type": "SVGS"},
                             {"uid": "u-giro", "account_id": {"iban": "DE00GIRO"}, "cash_account_type": "CACC"}]})
        if path.endswith("/balances"):
            return httpx.Response(self.status, json={"balances": [
                {"balance_type": "ITAV", "balance_amount": {"amount": "999.00", "currency": "EUR"}},
                {"balance_type": "CLBD", "balance_amount": {"amount": "120.50", "currency": "EUR"}}]})
        return httpx.Response(404, json={"message": "not found"})


@pytest.fixture
def bank(monkeypatch):
    fake = Bank()
    monkeypatch.setattr(eb.EnableBankingAdapter, "transport", httpx.MockTransport(fake))
    return fake


def test_login_through_the_bank_and_the_pasted_address(bank):
    a = ADAPTERS["psd2-1"](CREDS)
    with pytest.raises(AuthRequired) as e:
        a.login()
    assert e.value.challenge.kind == "url" and e.value.challenge.url == "https://bank.example.invalid/consent"
    auth = next(body for method, path, body in bank.calls if path == "/auth")
    assert auth["aspsp"] == {"name": "N26", "country": "DE"} and auth["psu_type"] == "personal"
    assert auth["redirect_url"] == "https://sync.local:8443/psd2"
    until = datetime.fromisoformat(auth["access"]["valid_until"])
    assert timedelta(days=179) < until - datetime.now(UTC) < timedelta(days=180)
    state = a.session["state"]
    with pytest.raises(AdapterError, match="anderen Anmeldung"):
        a.complete_login(f"https://sync.local:8443/psd2?code=abc&state={state}x")
    a.complete_login(f"https://sync.local:8443/psd2?state={state}&code=abc")
    assert bank.calls[-1] == ("POST", "/sessions", {"code": "abc"})
    assert a.session["session_id"] == "s-1" and "state" not in a.session
    assert a.get_cash()[0].amount == 120.50  # the booked balance, not the available one
    assert bank.calls[-1][1] == "/accounts/u-giro/balances"  # the giro account, not the savings one

    again = ADAPTERS["psd2-1"](CREDS, a.session_state())
    again.login()  # the stored session reads without the user
    with pytest.raises(AdapterError, match="nicht freigegeben"):
        a.complete_login("https://sync.local:8443/psd2?error=access_denied")


def test_an_unknown_bank_names_the_close_ones(bank):
    with pytest.raises(AdapterError, match="Meintest du: N26, N26 Business"):
        ADAPTERS["psd2-1"]({**CREDS, "bank": "N2"}).login()


def test_a_reminder_before_the_consent_ends_and_a_login_after(bank):
    told: list[str] = []
    soon = (datetime.now(UTC) + timedelta(days=7, hours=1)).isoformat()
    a = ADAPTERS["psd2-1"](CREDS, {"session_id": "s", "valid_until": soon, "accounts": []})
    a.on_user_action = told.append
    a.login()
    assert told and "7 Tag" in told[0]
    gone = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    calls = len(bank.calls)
    b = ADAPTERS["psd2-1"](CREDS, {"session_id": "s", "valid_until": gone})
    b.on_user_action = told.append
    with pytest.raises(AuthRequired, match="in der Oberfläche"):
        b.login()
    assert len(bank.calls) == calls  # a scheduled run starts no login at the bank


def test_a_withdrawn_consent_ends_the_session(bank):
    bank.status = 401
    a = ADAPTERS["psd2-1"](CREDS, {"session_id": "s", "valid_until": "2099-01-01T00:00:00+00:00",
                                   "accounts": [{"uid": "u", "iban": "", "type": "CACC"}]})
    with pytest.raises(AdapterError, match="neu anmelden"):
        a.get_cash()
    assert "session_id" not in a.session_state()


def test_pending_bookings_wait_and_debits_are_negative():
    recording = {"today": "2026-09-30", "pages": [
        [{"transaction_amount": {"amount": "12.00", "currency": "EUR"}, "credit_debit_indicator": "DBIT",
          "status": "BOOK", "booking_date": "2026-09-02", "creditor": {"name": "Bäckerei Muster"},
          "remittance_information": ["Kartenzahlung"]}],
        [{"transaction_amount": {"amount": "5.00", "currency": "EUR"}, "credit_debit_indicator": "DBIT",
          "status": "PDNG", "booking_date": "2026-09-29"}]]}
    txs = eb.BANKS[0].replay(recording).get_transactions(None)
    assert [(t.kind, t.signed, t.counterparty) for t in txs] == [(Kind.WITHDRAWAL, -12, "Bäckerei Muster")]


def test_a_continuation_key_that_comes_again_stops_the_fetch(monkeypatch):
    """#134 M6: a bank whose continuation key never moves on would else be asked for ever."""
    adapter = eb.BANKS[0](CREDS, None)
    calls = []
    monkeypatch.setattr(adapter, "_account", lambda: "uid")
    monkeypatch.setattr(adapter, "_call", lambda *a, **k: calls.append(k) or {"transactions": [],
                                                                               "continuation_key": "same"})
    with pytest.raises(AdapterError, match="dieselbe Seite"):
        adapter.get_transactions(None)
    assert len(calls) == 2
