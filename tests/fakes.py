"""An in-memory Wealthfolio REST API (httpx.MockTransport) for the tests.

It behaves like the real server where the sync depends on it: password login
with a ``wf_session`` cookie, Bearer auth, accounts, one-by-one creation with
duplicate rejection by fingerprint (account, type, date, asset, quantity, unit
price, amount, fee, currency, comment), update (``asset: {}`` removes the
asset), search by account and date, and holdings: a TRANSFER_IN/TRANSFER_OUT
*with* an asset is a securities transfer of that asset (moves no money, even
for a ``$CASH-EUR`` placeholder), one without an asset moves money. Assets and their
custom logos (``logos = None``: a Wealthfolio before 3.8.0).
"""

from __future__ import annotations

import base64
import json
import uuid
from urllib.parse import unquote

import httpx

PASSWORD = "wf-test-password"
TOKEN = "jwt-test-token"

ACCOUNTS = [
    {"id": "acc-cash", "name": "Test Cash", "currency": "EUR", "accountType": "CASH", "isActive": True},
    {"id": "acc-depot", "name": "Test Depot", "currency": "EUR", "accountType": "SECURITIES", "isActive": True},
    {"id": "acc-old", "name": "Altes Konto", "currency": "EUR", "accountType": "CASH", "isActive": False},
]


class FakeWealthfolio:
    def __init__(self, password: str | None = PASSWORD):
        self.password = password
        self.activities: list[dict] = []
        self.created: list[dict] = []  # payloads as received
        self.deleted: list[str] = []
        self.updated: list[dict] = []  # payloads as received
        self.fail_types: set[str] = set()
        self.logins = 0
        self.expire_next = False
        self.assets: list[dict] = []  # GET /assets
        self.logos: dict[str, str] | None = {}  # asset id -> base64 PNG; None: Wealthfolio before 3.8.0
        # Taxonomies: a few of Wealthfolio's seeded categories, and the assignments made.
        self.categories = {"regions": [{"id": "R20", "name": "Americas", "parentId": None},
                                       {"id": "country_US", "name": "United States", "parentId": "R2010"},
                                       {"id": "country_DE", "name": "Germany", "parentId": "R3010"}],
                           "industries_gics": [{"id": "45", "name": "Information Technology", "parentId": None},
                                               {"id": "4510", "name": "Software & Services", "parentId": "45"},
                                               {"id": "20", "name": "Industrials", "parentId": None}]}
        self.assignments: list[dict] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def add_existing(self, **a) -> dict:
        a.setdefault("id", str(uuid.uuid4()))
        self.activities.append(a)
        return a

    @staticmethod
    def fingerprint(p: dict) -> tuple:
        return (p["accountId"], p["activityType"], p["activityDate"], (p.get("asset") or {}).get("symbol"),
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
                # Like Wealthfolio: an asset id books onto that existing asset; no asset is "".
                "assetId": p.get("asset", {}).get("id") or p.get("asset", {}).get("symbol", ""),
                "assetName": p.get("asset", {}).get("name"),
                "sourceGroupId": p.get("sourceGroupId"),
                "_fp": fp,
            }
            self.activities.append(a)
            return httpx.Response(200, json=a)
        if path == "/api/v1/activities" and request.method == "PUT":
            p = json.loads(request.content)
            self.updated.append(p)
            found = [a for a in self.activities if a["id"] == p["id"]]
            if not found:
                return httpx.Response(400, json={"code": 400, "message": "Record not found"})
            a = found[0]
            fields = {"accountId": "accountId", "activityType": "activityType", "subtype": "subtype",
                      "activityDate": "date", "currency": "currency", "quantity": "quantity",
                      "unitPrice": "unitPrice", "amount": "amount", "fee": "fee", "tax": "tax", "comment": "comment"}
            a.update({old: p[new] for new, old in fields.items() if new in p})
            if p.get("asset") == {}:
                a.update(assetSymbol="", assetId="", assetName=None)
            elif "asset" in p:
                a.update(assetSymbol=p["asset"].get("symbol", ""),
                         assetId=p["asset"].get("id") or p["asset"].get("symbol", ""))
            a["_fp"] = (a["accountId"], a["activityType"], a["date"], a.get("assetSymbol") or None,
                        a.get("quantity"), a.get("unitPrice"), a.get("amount"), a.get("fee"), a["currency"],
                        a.get("comment"))
            return httpx.Response(200, json={k: v for k, v in a.items() if k != "_fp"})
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
        if path == "/api/v1/assets" and request.method == "GET":
            return httpx.Response(200, json=self.assets)
        if self.logos is not None and path == "/api/v1/assets/logos" and request.method == "GET":
            return httpx.Response(200, json=[{"assetId": i, "displayCode": None, "sha256": "x", "updatedAt": "t"}
                                             for i in self.logos])
        if self.logos is not None and path.startswith("/api/v1/assets/logo/") and request.method == "PUT":
            asset_id = unquote(path.rsplit("/", 1)[1])
            if not any(a["id"] == asset_id for a in self.assets):
                return httpx.Response(404, json={"code": 404, "message": "Asset not found"})
            data = json.loads(request.content)["dataBase64"]
            if not base64.b64decode(data).startswith(b"\x89PNG"):
                return httpx.Response(400, json={"code": 400, "message": "Logo image must be a PNG"})
            self.logos[asset_id] = data
            return httpx.Response(200, json={"assetId": asset_id})
        if path.startswith("/api/v1/taxonomies/assignments/asset/") and request.method == "PUT":
            _, asset_id, _, taxonomy_id = path.removeprefix("/api/v1/taxonomies/assignments/").split("/")
            new = json.loads(request.content)
            known = {c["id"] for c in self.categories[taxonomy_id]}
            if any(not 1 <= a["weight"] <= 10000 or a["categoryId"] not in known for a in new):
                return httpx.Response(400, json={"code": 400, "message": "invalid assignment"})
            self.assignments = [a for a in self.assignments
                                if not (a["assetId"] == asset_id and a["taxonomyId"] == taxonomy_id)]
            self.assignments += [{**a, "id": str(uuid.uuid4())} for a in new]
            return httpx.Response(200, json=new)
        if path.startswith("/api/v1/taxonomies/assignments/asset/"):
            asset_id = unquote(path.rsplit("/", 1)[1])
            return httpx.Response(200, json=[a for a in self.assignments if a["assetId"] == asset_id])
        if path.startswith("/api/v1/taxonomies/assignments/") and request.method == "DELETE":
            gone = path.rsplit("/", 1)[1]
            self.assignments = [a for a in self.assignments if a["id"] != gone]
            return httpx.Response(204)
        if path.startswith("/api/v1/taxonomies/"):
            taxonomy_id = path.rsplit("/", 1)[1]
            return httpx.Response(200, json={"taxonomy": {"id": taxonomy_id},
                                             "categories": self.categories.get(taxonomy_id, [])})
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
            asset = a.get("assetId") or symbol
            if t in ("TRANSFER_IN", "TRANSFER_OUT") and asset:
                # A securities transfer, also of a "$CASH-EUR" asset: shares, no money.
                q = Decimal(a["quantity"] or 0) * (1 if t == "TRANSFER_IN" else -1)
                shares[asset] = shares.get(asset, Decimal(0)) + q
                symbols.setdefault(asset, symbol or asset)
                continue
            cash[a["currency"]] = cash.get(a["currency"], Decimal(0)) + self.CASH_SIGN[t] * Decimal(a["amount"] or 0)
            if t in ("BUY", "SELL"):
                q = Decimal(a["quantity"]) * (1 if t == "BUY" else -1)
                shares[asset] = shares.get(asset, Decimal(0)) + q
                symbols.setdefault(asset, symbol)
        out = [{"holdingType": "cash", "localCurrency": c, "quantity": str(v), "instrument": {"symbol": c}}
               for c, v in cash.items()]
        # Every share is worth 1 EUR here.
        out += [{"holdingType": "security", "localCurrency": "EUR", "quantity": str(q),
                 "marketValue": {"local": str(q), "base": str(q)},
                 "instrument": {"id": i, "symbol": symbols[i], "name": symbols[i]}} for i, q in shares.items() if q]
        return out
