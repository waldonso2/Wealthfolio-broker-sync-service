"""N26 over its app's interface (waldonso2/wealthfolio-importer-addon#135): login with the app confirmation,
token refresh, the lock-out guard, and that a scheduled run never pushes a login into the app."""

import json
import time

import httpx
import pytest

from brokersync.adapters import n26
from brokersync.adapters.base import AdapterError, AuthRequired

CREDS = {"username": "max@example.invalid", "password": "geheim"}


class App:
    """A fake N26: password → mfa_required, challenge, confirmation after ``confirm_after`` polls."""

    def __init__(self, *, password_ok=True, confirm_after=1, refresh_ok=True):
        self.password_ok, self.confirm_after, self.refresh_ok = password_ok, confirm_after, refresh_ok
        self.calls: list[tuple[str, dict]] = []
        self.polls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/oauth2/token":
            form = dict(httpx.QueryParams(request.content.decode()))
            self.calls.append((form["grant_type"], form))
            assert request.headers["authorization"].startswith("Basic ") and request.headers["device-token"]
            if form["grant_type"] == "password":
                if not self.password_ok:
                    return httpx.Response(400, json={"error": "invalid_grant"})
                return httpx.Response(403, json={"error": "mfa_required", "mfaToken": "mfa-1"})
            if form["grant_type"] == "mfa_oob":
                self.polls += 1
                if self.polls < self.confirm_after:
                    return httpx.Response(400, json={"error": "authorization_pending"})
                return httpx.Response(200, json={"access_token": "a-1", "refresh_token": "r-1", "expires_in": 900})
            if form["grant_type"] == "refresh_token":
                if not self.refresh_ok:
                    return httpx.Response(401, json={"error": "invalid_grant"})
                return httpx.Response(200, json={"access_token": "a-2", "refresh_token": "r-2", "expires_in": 900})
        if path == "/api/mfa/challenge":
            self.calls.append(("challenge", json.loads(request.content)))
            return httpx.Response(201, json={})
        return httpx.Response(404, json={})


@pytest.fixture
def app(monkeypatch):
    fake = App()
    monkeypatch.setattr(n26.N26Adapter, "transport", httpx.MockTransport(lambda r: fake(r)))
    monkeypatch.setattr(n26, "POLL", 0)
    return fake


def test_login_with_the_confirmation_in_the_app(app):
    a = n26.N26Adapter(CREDS)
    with pytest.raises(AuthRequired, match="N26-App"):
        a.login()
    assert app.calls[1] == ("challenge", {"challengeType": "oob", "mfaToken": "mfa-1"})
    app.confirm_after = 3
    a.complete_login("")
    assert a.session["access_token"] == "a-1" and "mfa_token" not in a.session
    device = a.session["device_token"]
    again = n26.N26Adapter(CREDS, a.session_state())
    again.login()  # a valid token: nothing asked
    assert again.session["device_token"] == device and len(app.calls) == 2 + 3


def test_a_scheduled_run_refreshes_and_never_pushes_a_login(app):
    expired = {"access_token": "a-1", "refresh_token": "r-1", "expires_at": time.time() - 1, "device_token": "d"}
    a = n26.N26Adapter(CREDS, expired)
    a.on_user_action = lambda message: None
    a.login()
    assert a.session["access_token"] == "a-2" and app.calls[-1][0] == "refresh_token"
    app.refresh_ok = False
    b = n26.N26Adapter(CREDS, expired)
    b.on_user_action = lambda message: None
    with pytest.raises(AuthRequired, match="in der Oberfläche"):
        b.login()
    assert [c[0] for c in app.calls] == ["refresh_token", "refresh_token"]  # no password, no push


def test_a_rejected_password_is_never_tried_again(app):
    app.password_ok = False
    a = n26.N26Adapter(CREDS)
    with pytest.raises(AdapterError, match="abgelehnt"):
        a.login()
    calls = len(app.calls)
    with pytest.raises(AdapterError, match="nicht noch einmal"):
        n26.N26Adapter(CREDS, a.session_state()).login()
    assert len(app.calls) == calls


def test_unofficial_interface_needs_the_users_yes():
    assert "keine offizielle Schnittstelle" in n26.N26Adapter.consent


def test_a_page_that_comes_again_stops_the_fetch(monkeypatch):
    """#134 M6: an answer that ignores lastId would else be fetched for ever."""
    adapter = n26.N26Adapter(CREDS, None)
    page = [{"id": f"t{i}", "visibleTS": 0, "amount": -1.0} for i in range(n26.PAGE)]
    calls = []
    monkeypatch.setattr(adapter, "_get", lambda path, **params: calls.append(params) or page)
    with pytest.raises(AdapterError, match="dieselbe Seite"):
        adapter.get_transactions(None)
    assert len(calls) == 2
