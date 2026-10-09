"""Contract tests for the Wealthfolio REST client against recorded answers
(tests/fixtures/wealthfolio/, shaped like Wealthfolio 3.9's responses)."""

import json
from datetime import date
from pathlib import Path

import httpx
import pytest
import respx

from brokersync.wealthfolio import AuthFailed, Duplicate, WealthfolioClient, WealthfolioError

FIX = Path(__file__).parent / "fixtures" / "wealthfolio"
BASE = "http://wf.local:8080"


def rec(name: str):
    return json.loads((FIX / name).read_text())


def login_route(mock):
    return mock.post(f"{BASE}/api/v1/auth/login").mock(return_value=httpx.Response(
        200, json=rec("login.json"),
        headers={"set-cookie": "wf_session=abc.def.ghi; HttpOnly; SameSite=Lax; Path=/api; Max-Age=3600; Secure"}))


@respx.mock
def test_login_sends_the_session_token_as_bearer():
    login = login_route(respx)
    accounts = respx.get(f"{BASE}/api/v1/accounts").mock(return_value=httpx.Response(200, json=rec("accounts.json")))
    with WealthfolioClient(BASE, "pw") as wf:
        result = wf.list_accounts()
    assert json.loads(login.calls[0].request.content) == {"password": "pw"}
    assert accounts.calls[0].request.headers["authorization"] == "Bearer abc.def.ghi"
    assert [(a.name, a.currency, a.is_active) for a in result] == [("TR Cash", "EUR", True), ("TR Depot", "EUR", True)]


@respx.mock
def test_wrong_password():
    respx.post(f"{BASE}/api/v1/auth/login").mock(return_value=httpx.Response(401, json={"code": 401, "message": "x"}))
    with pytest.raises(AuthFailed):
        WealthfolioClient(BASE, "wrong").list_accounts()


@respx.mock
def test_no_password_means_no_login():
    respx.get(f"{BASE}/api/v1/accounts").mock(return_value=httpx.Response(200, json=[]))
    assert WealthfolioClient(BASE, None).list_accounts() == []


@respx.mock
def test_expired_session_logs_in_again():
    login = login_route(respx)
    respx.get(f"{BASE}/api/v1/accounts").mock(side_effect=[
        httpx.Response(401, json={"code": 401, "message": "expired"}),
        httpx.Response(200, json=rec("accounts.json")),
    ])
    WealthfolioClient(BASE, "pw").list_accounts()
    assert login.call_count == 2


@respx.mock
def test_create_reports_duplicates_and_errors():
    login_route(respx)
    respx.post(f"{BASE}/api/v1/activities").mock(side_effect=[
        httpx.Response(200, json=rec("activity_created.json")),
        httpx.Response(409, json=rec("duplicate.json")),
        httpx.Response(400, json={"code": 400, "message": "Invalid data: Account not found"}),
    ])
    wf = WealthfolioClient(BASE, "pw")
    assert wf.create_activity({"activityType": "DEPOSIT"})["id"] == "8f1c2d3e-0000-4000-8000-000000000001"
    assert isinstance(wf.create_activity({"activityType": "DEPOSIT"}), Duplicate)
    with pytest.raises(WealthfolioError, match="Account not found"):
        wf.create_activity({"activityType": "DEPOSIT"})


@respx.mock
def test_search_pages_through_all_results():
    login_route(respx)
    page = rec("search_page.json")
    route = respx.post(f"{BASE}/api/v1/activities/search").mock(side_effect=[
        httpx.Response(200, json={"data": page["data"][:1], "meta": {"totalRowCount": 2}}),
        httpx.Response(200, json={"data": page["data"][1:], "meta": {"totalRowCount": 2}}),
    ])
    rows = WealthfolioClient(BASE, "pw").search_activities(["a", "b"], date(2026, 1, 1), date(2026, 1, 31),
                                                           page_size=1)
    assert [r["activityType"] for r in rows] == ["BUY", "TRANSFER_IN"]
    first = json.loads(route.calls[0].request.content)
    assert first == {"page": 0, "pageSize": 1, "accountIdFilter": ["a", "b"], "dateFrom": "2026-01-01",
                     "dateTo": "2026-01-31", "sort": {"id": "date", "desc": False}}
    assert json.loads(route.calls[1].request.content)["page"] == 1
