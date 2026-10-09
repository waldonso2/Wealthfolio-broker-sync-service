"""An in-memory Wealthfolio REST API (httpx.MockTransport) for the tests.

It behaves like the real server where the sync depends on it: password login
with a ``wf_session`` cookie, Bearer auth, accounts, one-by-one creation with
duplicate rejection by fingerprint (account, type, date, asset, quantity, unit
price, amount, fee, currency, comment), and search by account and date.
"""

from __future__ import annotations

import json
import uuid

import httpx

PASSWORD = "wf-test-password"
TOKEN = "jwt-test-token"

ACCOUNTS = [
    {"id": "acc-cash", "name": "Dummy Cash", "currency": "EUR", "accountType": "CASH", "isActive": True},
    {"id": "acc-depot", "name": "Dummy Depot", "currency": "EUR", "accountType": "SECURITIES", "isActive": True},
    {"id": "acc-old", "name": "Altes Konto", "currency": "EUR", "accountType": "CASH", "isActive": False},
]


class FakeWealthfolio:
    def __init__(self, password: str | None = PASSWORD):
        self.password = password
        self.activities: list[dict] = []
        self.created: list[dict] = []  # payloads as received
        self.fail_types: set[str] = set()
        self.logins = 0
        self.expire_next = False

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def add_existing(self, **a) -> dict:
        a.setdefault("id", str(uuid.uuid4()))
        self.activities.append(a)
        return a

    @staticmethod
    def fingerprint(p: dict) -> tuple:
        return (p["accountId"], p["activityType"], p["activityDate"], p.get("asset", {}).get("symbol"),
                p.get("quantity"), p.get("unitPrice"), p.get("amount"), p.get("fee"), p["currency"],
                p.get("comment"))

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/auth/login":
            body = json.loads(request.content)
            if body.get("password") != self.password:
                return httpx.Response(401, json={"code": 401, "message": "Invalid password"})
            self.logins += 1
            return httpx.Response(200, json={"authenticated": True, "expiresIn": 3600},
                                  headers={"set-cookie": f"wf_session={TOKEN}; HttpOnly; SameSite=Lax; Path=/api"})
        if self.password:
            if self.expire_next:
                self.expire_next = False
                return httpx.Response(401, json={"code": 401, "message": "expired"})
            if request.headers.get("authorization") != f"Bearer {TOKEN}":
                return httpx.Response(401, json={"code": 401, "message": "Unauthorized"})
        if path == "/api/v1/accounts" and request.method == "GET":
            return httpx.Response(200, json=ACCOUNTS)
        if path == "/api/v1/activities" and request.method == "POST":
            p = json.loads(request.content)
            self.created.append(p)
            if p["activityType"] in self.fail_types:
                return httpx.Response(400, json={"code": 400, "message": "Invalid data: simulated failure"})
            fp = self.fingerprint(p)
            if any(a.get("_fp") == fp for a in self.activities):
                message = "Invalid data: Duplicate activity detected. A matching activity already exists."
                return httpx.Response(409, json={"code": 409, "message": message})
            a = {
                "id": str(uuid.uuid4()),
                "accountId": p["accountId"],
                "activityType": p["activityType"],
                "subtype": p.get("subtype"),
                "date": p["activityDate"],
                "quantity": p.get("quantity"),
                "unitPrice": p.get("unitPrice"),
                "amount": p.get("amount"),
                "fee": p.get("fee"),
                "tax": p.get("tax"),
                "currency": p["currency"],
                "comment": p.get("comment"),
                "assetSymbol": p.get("asset", {}).get("symbol", ""),
                "assetId": p.get("asset", {}).get("symbol", ""),
                "sourceGroupId": p.get("sourceGroupId"),
                "_fp": fp,
            }
            self.activities.append(a)
            return httpx.Response(200, json=a)
        if path == "/api/v1/activities/search":
            body = json.loads(request.content)
            ids = body.get("accountIdFilter") or []
            rows = [a for a in self.activities
                    if a["accountId"] in ids and body["dateFrom"] <= a["date"][:10] <= body["dateTo"]]
            size = body["pageSize"]
            page = rows[body["page"] * size:(body["page"] + 1) * size]
            return httpx.Response(200, json={"data": page, "meta": {"totalRowCount": len(rows)}})
        return httpx.Response(404, json={"code": 404, "message": "Not found"})
