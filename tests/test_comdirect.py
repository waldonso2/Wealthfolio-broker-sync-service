"""comdirect: login with session TAN and the lock-out guard, against a fake API."""

import json
from decimal import Decimal as D

import httpx
import pytest

from brokersync.adapters import AdapterError, AuthRequired
from brokersync.adapters import comdirect as cd
from brokersync.adapters.comdirect import ComdirectAdapter

CREDS = {"username": "12345678", "pin": "TEST-PIN", "client_id": "test-client", "client_secret": "test-secret"}


class FakeComdirect:
    """The login calls of comdirect's API, with photoTAN-Push (status link) or mobileTAN."""

    def __init__(self, typ="P_TAN_PUSH", link=True, pin="TEST-PIN", tan="123456"):
        self.typ, self.link, self.pin, self.tan = typ, link, pin, tan
        self.approved = False
        self.challenges = 0
        self.calls: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(f"{request.method} {path}")
        if path == "/oauth/token":
            form = dict(x.split("=", 1) for x in request.content.decode().split("&"))
            grant = form["grant_type"]
            if grant == "password":
                if form["password"] != self.pin:
                    return httpx.Response(401, json={"error": "invalid_grant", "error_description": "Bad credentials"})
                return httpx.Response(200, json={"access_token": "first", "refresh_token": "r1", "expires_in": 599})
            if grant == "cd_secondary":
                return httpx.Response(200, json={"access_token": "full", "refresh_token": "r2", "expires_in": 599})
            if grant == "refresh_token":
                if form["refresh_token"] == "expired":
                    return httpx.Response(400, json={"error": "invalid_grant"})
                return httpx.Response(200, json={"access_token": "refreshed", "refresh_token": "r3", "expires_in": 599})
        assert json.loads(request.headers["x-http-request-info"])["clientRequestId"]["sessionId"]
        if path == "/api/session/clients/user/v1/sessions":
            return httpx.Response(200, json=[{"identifier": "SESSION-1", "sessionTanActive": False,
                                              "activated2FA": False}])
        if path.endswith("/validate"):
            self.challenges += 1
            info = {"id": f"CH-{self.challenges}", "typ": self.typ, "availableTypes": [self.typ]}
            if self.typ == "M_TAN":
                info["challenge"] = "+49-160-99XXXX"
            if self.typ == "P_TAN_PUSH" and self.link:
                info["link"] = {"href": f"/api/session/v1/authentications/CH-{self.challenges}", "rel": "self",
                                "method": "GET"}
            return httpx.Response(201, json={"identifier": "SESSION-1"},
                                  headers={"x-once-authentication-info": json.dumps(info)})
        if path.startswith("/api/session/v1/authentications/"):
            return httpx.Response(200, json={"status": "AUTHENTICATED" if self.approved else "PENDING"})
        if request.method == "PATCH":
            if self.typ == "M_TAN" and request.headers.get("x-once-authentication") != self.tan:
                return httpx.Response(422, json={"code": "TAN_UNGUELTIG",
                                                 "messages": [{"severity": "ERROR", "message": "TAN ungültig"}]})
            if self.typ == "P_TAN_PUSH" and not self.approved:
                return httpx.Response(422, json={"code": "expired"})
            return httpx.Response(200, json={"identifier": "SESSION-1", "sessionTanActive": True,
                                             "activated2FA": True})
        return httpx.Response(404, json={})


@pytest.fixture
def api(monkeypatch):
    fake = FakeComdirect()
    monkeypatch.setattr(ComdirectAdapter, "transport", httpx.MockTransport(fake.handle))
    monkeypatch.setattr(cd, "PUSH_POLL", 0)
    monkeypatch.setattr(cd, "CONFIRM_WAIT", 0)
    monkeypatch.setattr(cd, "PUSH_WAIT", 0)
    return fake


def test_web_login_with_photo_tan_push(api):
    a = ComdirectAdapter(CREDS)
    with pytest.raises(AuthRequired) as e:
        a.login()
    assert e.value.challenge.kind == "confirm" and "photoTAN-App" in e.value.challenge.message
    assert a.session_state()["open_challenges"] == 1
    # Not approved yet: asked again, no activation tried (it could count as a wrong TAN).
    with pytest.raises(AuthRequired, match="Noch nicht bestätigt"):
        a.complete_login("")
    assert not any(c.startswith("PATCH") for c in api.calls)
    api.approved = True
    a.complete_login("")
    s = a.session_state()
    assert (s["access_token"], s["refresh_token"], s["open_challenges"]) == ("full", "r2", 0)
    assert api.challenges == 1


def test_scheduled_run_asks_on_the_phone_and_waits(api):
    s = ComdirectAdapter(CREDS)
    notes: list[str] = []
    s.on_user_action = notes.append
    with pytest.raises(AuthRequired, match="nicht rechtzeitig"):
        s.login()
    assert notes and "photoTAN-App" in notes[0]
    # The open challenge is kept; once approved the next run gets through.
    api.approved = True
    again = ComdirectAdapter(CREDS, s.session_state())
    again.on_user_action = notes.append
    again.login()
    assert again.session_state()["access_token"] == "full"


def test_a_valid_or_refreshable_session_needs_no_tan(api):
    a = ComdirectAdapter(CREDS, {"access_token": "full", "refresh_token": "r2", "expires_at": 0})
    a.login()
    assert a.session_state()["access_token"] == "refreshed" and api.challenges == 0
    b = ComdirectAdapter(CREDS, {"access_token": "full", "refresh_token": "expired", "expires_at": 0})
    with pytest.raises(AuthRequired):
        b.login()
    assert api.challenges == 1


def test_too_many_open_challenges_stop_before_comdirect_locks(api):
    session: dict = {}
    for _ in range(cd.MAX_OPEN_CHALLENGES):
        a = ComdirectAdapter(CREDS, session)
        with pytest.raises(AuthRequired):
            a.login()
        session = a.session_state()
    with pytest.raises(AdapterError, match="nicht bestätigt"):
        ComdirectAdapter(CREDS, session).login()
    assert api.challenges == cd.MAX_OPEN_CHALLENGES < 5


def test_a_rejected_pin_is_never_retried(api):
    api.pin = "OTHER"
    a = ComdirectAdapter(CREDS)
    with pytest.raises(AdapterError, match="abgelehnt"):
        a.login()
    calls = len(api.calls)
    with pytest.raises(AdapterError, match="neu speichern"):
        ComdirectAdapter(CREDS, a.session_state()).login()
    assert len(api.calls) == calls


def test_mobile_tan_typed_in_and_a_second_wrong_tan_blocks(monkeypatch):
    fake = FakeComdirect(typ="M_TAN")
    monkeypatch.setattr(ComdirectAdapter, "transport", httpx.MockTransport(fake.handle))
    a = ComdirectAdapter(CREDS)
    with pytest.raises(AuthRequired) as e:
        a.login()
    assert e.value.challenge.kind == "code" and "+49-160-99XXXX" in e.value.challenge.message
    with pytest.raises(AuthRequired, match="abgelehnt"):
        a.complete_login("000000")
    with pytest.raises(AdapterError, match="zweimal abgelehnt"):
        a.complete_login("111111")
    assert a.session_state()["pin_rejected"]

    ok = ComdirectAdapter(CREDS)
    with pytest.raises(AuthRequired):
        ok.login()
    ok.complete_login("123456")
    assert ok.session_state()["access_token"] == "full"
    # A scheduled run doesn't open a challenge it can't answer (TAN to type).
    sched = ComdirectAdapter(CREDS, {"tan_type": "M_TAN"})
    sched.on_user_action = lambda m: None
    before = fake.challenges
    with pytest.raises(AuthRequired, match="Weboberfläche"):
        sched.login()
    assert fake.challenges == before


def test_photo_tan_graphic_is_shown():
    a = ComdirectAdapter(CREDS)
    a._pending = {"typ": "P_TAN", "challenge": "iVBORw0KGgo="}
    c = a._challenge()
    assert c.kind == "code" and c.image == "data:image/png;base64,iVBORw0KGgo="


def test_trade_settlement_and_unsettled_trades():
    def booking(ref, day, amount, text="WP-ABRECHNUNG"):
        return {"reference": ref, "bookingStatus": "BOOKED", "bookingDate": day, "amount": {"value": amount,
                "unit": "EUR"}, "transactionType": {"key": "SECURITIES", "text": "Wertpapier"},
                "remittanceInfo": f"01{text:<35}"}

    def trade(tid, day, kind, value, isin):
        return {"transactionId": tid, "bookingStatus": "BOOKED", "businessDate": day,
                "quantity": {"value": "2", "unit": "XXX"}, "instrument": {"isin": isin, "wkn": "", "name": "TEST"},
                "transactionValue": {"value": value, "unit": "EUR"}, "transactionType": {"key": kind}}

    from datetime import date
    # Two buys of the same amount: each takes the booking that names its ISIN.
    bookings = [booking("B1", "2026-03-05", "-101.00", "KAUF DE000TEST00B"),
                booking("B2", "2026-03-05", "-101.00", "KAUF DE000TEST00A")]
    trades = [trade("T1", "2026-03-04", "BUY", "100", "DE000TEST00A"),
              trade("T2", "2026-03-04", "BUY", "100", "DE000TEST00B")]
    txs = {t.id: t for t in cd.to_transactions(bookings, trades, {}, date(2026, 3, 20))}
    assert set(txs) == {"dep-T1", "dep-T2"}
    assert (txs["dep-T1"].net, txs["dep-T1"].fee) == (D("101.00"), D("1.00"))
    # A booking far off in amount (costs over half the value) or date isn't the settlement.
    far = cd.to_transactions([booking("B3", "2026-03-20", "-101"), booking("B4", "2026-03-05", "-300")],
                             [trade("T3", "2026-03-04", "BUY", "100", "DE000TEST00A")], {}, date(2026, 3, 20))
    # Unused giro bookings aren't booked at all: only dividends land on the cash account.
    assert [(t.id, t.kind.value) for t in far] == [("dep-T3", "UNKNOWN")]
    assert all(t.external_cash for t in txs.values())


def test_a_push_without_status_link_is_left_to_the_web_ui(monkeypatch):
    fake = FakeComdirect(link=False)
    monkeypatch.setattr(ComdirectAdapter, "transport", httpx.MockTransport(fake.handle))
    s = ComdirectAdapter(CREDS)
    s.on_user_action = lambda m: None
    with pytest.raises(AuthRequired, match="Weboberfläche"):
        s.login()
    again = ComdirectAdapter(CREDS, s.session_state())
    again.on_user_action = lambda m: None
    with pytest.raises(AuthRequired, match="Weboberfläche"):
        again.login()
    assert fake.challenges == 1  # the second run didn't ask again
    # In the web UI the user confirms; then the activation is tried once.
    web = ComdirectAdapter(CREDS, again.session_state())
    with pytest.raises(AuthRequired):
        web.login()
    fake.approved = True
    web.complete_login("")
    assert web.session_state()["access_token"] == "full"
