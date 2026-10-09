"""Client for the REST API of a self-hosted Wealthfolio (``/api/v1``).

Authentication: ``POST /api/v1/auth/login {"password": ...}`` answers with a
``wf_session`` cookie holding a JWT. The server also accepts that JWT as
``Authorization: Bearer``, which works over plain HTTP too (the cookie may be
marked ``Secure``), so the client sends it as a header. A server started with
``WF_AUTH_REQUIRED=false`` needs no password.

Activities are created one at a time (``POST /api/v1/activities``), like the
addon does, so ``tax``, ``subtype`` and ``sourceGroupId`` arrive unchanged.
Wealthfolio rejects an exact duplicate (same idempotency fingerprint) with
"Duplicate activity detected"; ``create_activity`` reports that as
``Duplicate`` instead of an error.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

import httpx

API = "/api/v1"


class WealthfolioError(Exception):
    pass


class AuthFailed(WealthfolioError):
    pass


@dataclass(frozen=True)
class Duplicate:
    message: str


@dataclass(frozen=True)
class Account:
    id: str
    name: str
    currency: str
    account_type: str
    is_active: bool


class WealthfolioClient:
    def __init__(self, base_url: str, password: str | None, *, timeout: float = 30.0,
                 transport: httpx.BaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        self.password = password or None
        self._token: str | None = None
        self._http = httpx.Client(base_url=self.base_url, timeout=timeout, transport=transport,
                                  follow_redirects=False)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> WealthfolioClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ── auth ────────────────────────────────────────────────────────────────
    def login(self) -> None:
        if not self.password:
            return
        try:
            r = self._http.post(f"{API}/auth/login", json={"password": self.password})
        except httpx.HTTPError as e:
            raise WealthfolioError(f"Wealthfolio is not reachable at {self.base_url}: {e}") from e
        if r.status_code in (401, 403):
            raise AuthFailed("Wealthfolio rejected the password.")
        if r.status_code >= 400:
            raise WealthfolioError(f"Login failed: HTTP {r.status_code} {_detail(r)}")
        token = _session_token(r)
        if not token:
            raise WealthfolioError("Login succeeded but Wealthfolio sent no session token.")
        self._token = token

    def _request(self, method: str, path: str, **kw) -> httpx.Response:
        if self.password and not self._token:
            self.login()
        for attempt in range(2):
            headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
            try:
                r = self._http.request(method, f"{API}{path}", headers=headers, **kw)
            except httpx.HTTPError as e:
                raise WealthfolioError(f"Wealthfolio is not reachable at {self.base_url}: {e}") from e
            if r.status_code == 401 and self.password and attempt == 0:
                self.login()  # session expired
                continue
            if r.status_code == 401:
                raise AuthFailed("Wealthfolio asks for a login - enter the Wealthfolio password in the setup.")
            return r
        raise AssertionError("unreachable")

    # ── API ─────────────────────────────────────────────────────────────────
    def list_accounts(self) -> list[Account]:
        r = self._request("GET", "/accounts")
        _raise(r)
        return [
            Account(a["id"], a.get("name", ""), a.get("currency", ""), a.get("accountType", ""),
                    a.get("isActive", True))
            for a in r.json()
        ]

    def create_activity(self, payload: dict) -> dict | Duplicate:
        r = self._request("POST", "/activities", json=payload)
        if r.status_code >= 400:
            detail = _detail(r)
            if "Duplicate activity" in detail:
                return Duplicate(detail)
            raise WealthfolioError(f"Wealthfolio rejected the activity ({payload.get('activityType')}): {detail}")
        return r.json()

    def delete_activity(self, activity_id: str) -> None:
        r = self._request("DELETE", f"/activities/{activity_id}")
        _raise(r)

    def holdings(self, account_id: str) -> list[dict]:
        """Current holdings of an account, cash included (``holdingType`` "cash"/"security")."""
        r = self._request("GET", "/holdings", params={"accountId": account_id})
        _raise(r)
        return r.json()

    def search_activities(self, account_ids: list[str], date_from: date, date_to: date,
                          page_size: int = 500) -> list[dict]:
        """All activities of the accounts between the two dates (inclusive)."""
        out: list[dict] = []
        page = 0
        while True:
            r = self._request("POST", "/activities/search", json={
                "page": page,
                "pageSize": page_size,
                "accountIdFilter": account_ids,
                "dateFrom": date_from.isoformat(),
                "dateTo": date_to.isoformat(),
                "sort": {"id": "date", "desc": False},
            })
            _raise(r)
            body = r.json()
            data = body.get("data", [])
            out.extend(data)
            total = body.get("meta", {}).get("totalRowCount", len(out))
            if not data or len(out) >= total:
                return out
            page += 1


def _session_token(r: httpx.Response) -> str | None:
    for header in r.headers.get_list("set-cookie"):
        m = re.match(r"\s*wf_session=([^;]+)", header)
        if m:
            return m.group(1)
    return None


def _detail(r: httpx.Response) -> str:
    try:
        body = r.json()
    except ValueError:
        return r.text[:500]
    if isinstance(body, dict):
        for key in ("message", "error", "detail"):
            if isinstance(body.get(key), str):
                return body[key]
    return str(body)[:500]


def _raise(r: httpx.Response) -> None:
    if r.status_code >= 400:
        raise WealthfolioError(f"HTTP {r.status_code}: {_detail(r)}")
