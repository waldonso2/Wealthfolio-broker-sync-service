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
        self.deleted: list[str] = []
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
                # Like Wealthfolio: an asset id books onto that existing asset.
                "assetId": p.get("asset", {}).get("id") or p.get("asset", {}).get("symbol", ""),
                "assetName": p.get("asset", {}).get("name"),
                "sourceGroupId": p.get("sourceGroupId"),
                "_fp": fp,
            }
            self.activities.append(a)
            return httpx.Response(200, json=a)
        if path.startswith("/api/v1/activities/") and request.method == "DELETE":
            activity_id = path.rsplit("/", 1)[1]
            gone = [a for a in self.activities if a["id"] == activity_id]
            if not gone:
                # Wealthfolio answers 400 for a missing activity.
                return httpx.Response(400, json={"code": 400, "message": "Record not found"})
            # Like Wealthfolio: deleting one leg of a linked transfer pair deletes both.
            group = gone[0].get("sourceGroupId")
            if group and gone[0]["activityType"] in ("TRANSFER_IN", "TRANSFER_OUT"):
                gone = [a for a in self.activities if a.get("sourceGroupId") == group]
            for a in gone:
                self.activities.remove(a)
                self.deleted.append(a["id"])
            return httpx.Response(200, json=gone[0])
        if path == "/api/v1/holdings":
            return httpx.Response(200, json=self.holdings(request.url.params["accountId"]))
        if path == "/api/v1/activities/search":
            body = json.loads(request.content)
            ids = body.get("accountIdFilter") or []
            rows = [a for a in self.activities
                    if a["accountId"] in ids and body["dateFrom"] <= a["date"][:10] <= body["dateTo"]]
            size = body["pageSize"]
            page = rows[body["page"] * size:(body["page"] + 1) * size]
            return httpx.Response(200, json={"data": page, "meta": {"totalRowCount": len(rows)}})
        return httpx.Response(404, json={"code": 404, "message": "Not found"})

    # Cash effect of each type, as Wealthfolio computes it from ``amount``.
    CASH_SIGN = {"DEPOSIT": 1, "TRANSFER_IN": 1, "SELL": 1, "DIVIDEND": 1, "INTEREST": 1, "CREDIT": 1,
                 "WITHDRAWAL": -1, "TRANSFER_OUT": -1, "BUY": -1, "FEE": -1, "TAX": -1}

    def holdings(self, account_id: str) -> list[dict]:
        from decimal import Decimal

        cash: dict[str, Decimal] = {}
        shares: dict[str, Decimal] = {}
        symbols: dict[str, str] = {}
        for a in self.activities:
            if a["accountId"] != account_id:
                continue
            t = a["activityType"]
            symbol = a.get("assetSymbol") or ""
            if t in ("TRANSFER_IN", "TRANSFER_OUT") and not symbol.startswith("$CASH-"):
                continue
            cash[a["currency"]] = cash.get(a["currency"], Decimal(0)) + self.CASH_SIGN[t] * Decimal(a["amount"] or 0)
            if t in ("BUY", "SELL"):
                q = Decimal(a["quantity"]) * (1 if t == "BUY" else -1)
                asset = a.get("assetId") or symbol
                shares[asset] = shares.get(asset, Decimal(0)) + q
                symbols.setdefault(asset, symbol)
        out = [{"holdingType": "cash", "localCurrency": c, "quantity": str(v), "instrument": {"symbol": c}}
               for c, v in cash.items()]
        out += [{"holdingType": "security", "localCurrency": "EUR", "quantity": str(q),
                 "instrument": {"id": i, "symbol": symbols[i], "name": symbols[i]}} for i, q in shares.items() if q]
        return out
