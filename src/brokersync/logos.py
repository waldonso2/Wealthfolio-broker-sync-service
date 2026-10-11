"""Logos for securities in Wealthfolio, from logo.dev by ISIN (optional, off by default).

Wealthfolio (3.8.0 and later) keeps a custom logo per asset (``PUT /assets/logo/{id}``,
stored in its database, synced and backed up). Its bundled logos are named by ticker
and exchange, so assets booked under an ISIN show none.

After every run, if the user turned it on and saved a logo.dev key: for each asset
whose ISIN is known - Wealthfolio's own (``metadata.identifiers.isin``, or an ISIN
as symbol) or learned by the sync (``brokersync.assets``) - and that has no custom
logo yet, ask logo.dev and set the PNG it returns. Only the ISIN leaves the
container.

- An asset with a custom logo is never touched (the user's own wins).
- A logo the sync set is set once (``set``): one the user removes stays removed.
- No logo at logo.dev (``none``): asked again after ``RETRY_DAYS``.
- At most ``PER_RUN`` questions per run; a rejected key or the rate limit ends the pass.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

import httpx

from .assets import valid_isin
from .state import State, now
from .wealthfolio import LogosUnsupported, WealthfolioClient, WealthfolioError

log = logging.getLogger(__name__)

URL = "https://img.logo.dev/isin/{isin}"
SIZE = 128
# Wealthfolio's limits for a logo.
MAX_BYTES = 150 * 1024
MAX_DIMENSION = 256
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
RETRY_DAYS = 30
PER_RUN = 50
ISIN = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}\d$")
LAST = "logos:last"


def status_key(asset_id: str) -> str:
    return f"logo:{asset_id}"


class LogoError(Exception):
    pass


class StopPass(LogoError):
    """The key was rejected or the rate limit reached: no more questions this run."""


class LogoDev:
    def __init__(self, token: str, *, transport: httpx.BaseTransport | None = None, timeout: float = 20.0):
        self.token = token
        self._http = httpx.Client(timeout=timeout, transport=transport, follow_redirects=True)

    def close(self) -> None:
        self._http.close()

    def fetch(self, isin: str) -> bytes | None:
        """The PNG logo of ``isin``, None when logo.dev has none."""
        try:
            r = self._http.get(URL.format(isin=isin),
                               params={"token": self.token, "format": "png", "size": SIZE, "fallback": "404"})
        except httpx.HTTPError as e:
            raise StopPass(f"logo.dev ist nicht erreichbar ({type(e).__name__}).") from e
        if r.status_code == 404:
            return None
        if r.status_code in (401, 403):
            raise StopPass("logo.dev hat den Schlüssel abgelehnt.")
        if r.status_code == 429:
            raise StopPass("logo.dev: Abruflimit erreicht, weiter beim nächsten Abruf.")
        if r.status_code >= 400:
            raise LogoError(f"logo.dev: HTTP {r.status_code}")
        return r.content


def check_png(data: bytes) -> None:
    """Raise LogoError unless ``data`` is a PNG Wealthfolio accepts."""
    if len(data) > MAX_BYTES:
        raise LogoError(f"Logo größer als {MAX_BYTES // 1024} KB")
    if len(data) < 24 or not data.startswith(PNG_SIGNATURE) or data[12:16] != b"IHDR":
        raise LogoError("kein PNG")
    width, height = int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if not (1 <= width <= MAX_DIMENSION and 1 <= height <= MAX_DIMENSION):
        raise LogoError(f"Logo {width}x{height} Pixel, höchstens {MAX_DIMENSION}")


def _isin(code) -> str | None:
    code = str(code or "").strip().upper()
    return code if ISIN.match(code) and valid_isin(code) else None


def isin_of(asset: dict) -> str | None:
    identifiers = (asset.get("metadata") or {}).get("identifiers") or {}
    return (_isin(identifiers.get("isin") if isinstance(identifiers, dict) else None)
            or _isin(asset.get("instrumentSymbol")) or _isin(asset.get("displayCode")))


def candidates(assets: list[dict], learned: dict[str, str]) -> dict[str, str]:
    """Asset id -> ISIN: Wealthfolio's own ISIN first, else the one the sync learned for the asset."""
    out = {a["id"]: isin for a in assets if a.get("id") and (isin := isin_of(a))}
    present = {a.get("id") for a in assets}
    for asset_id, isin in learned.items():
        if asset_id in present:
            out.setdefault(asset_id, isin)
    return out


@dataclass
class Result:
    at: str = ""
    set: int = 0
    none: int = 0
    failed: int = 0
    open: int = 0  # left for the next run (limit, stop)
    message: str = ""


def _due(state: State, asset_id: str, at: datetime) -> bool:
    raw = state.meta(status_key(asset_id))
    if not raw:
        return True
    entry = json.loads(raw)
    if entry.get("status") == "set":
        return False
    return datetime.fromisoformat(entry["at"]) <= at - timedelta(days=RETRY_DAYS)


def update(state: State, wf: WealthfolioClient, source: LogoDev, learned: dict[str, str], *,
           limit: int = PER_RUN, at: datetime | None = None) -> Result:
    """One pass; the result is also stored for the page *Logos*."""
    at = at or datetime.now(UTC)
    result = Result(at=now())
    try:
        own = wf.asset_logos()
        wanted = candidates(wf.list_assets(), learned)
    except LogosUnsupported:
        result.message = "Wealthfolio kann eigene Logos erst ab Version 3.8.0."
    except WealthfolioError as e:
        result.message = f"Wealthfolio: {e}"
    else:
        todo = sorted(((isin, asset_id) for asset_id, isin in wanted.items()
                       if asset_id not in own and _due(state, asset_id, at)))
        fetched: dict[str, bytes | None] = {}
        broken: set[str] = set()  # the question failed this run
        for n, (isin, asset_id) in enumerate(todo):
            if isin in broken:
                result.failed += 1
                continue
            if isin not in fetched and len(fetched) + len(broken) >= limit:
                result.open = len(todo) - n
                break
            try:
                if isin not in fetched:
                    try:
                        fetched[isin] = source.fetch(isin)
                    except StopPass:
                        raise
                    except LogoError:
                        broken.add(isin)
                        raise
                png = fetched[isin]
                if png is None:
                    state.set_meta(status_key(asset_id), json.dumps({"isin": isin, "status": "none", "at": now()}))
                    result.none += 1
                    continue
                check_png(png)
                wf.set_asset_logo(asset_id, png)
                state.set_meta(status_key(asset_id), json.dumps({"isin": isin, "status": "set", "at": now()}))
                result.set += 1
            except StopPass as e:
                result.message = str(e)
                result.open = len(todo) - n
                break
            except (LogoError, WealthfolioError) as e:
                result.failed += 1
                log.info("logo of %s not set: %s", isin, e)
    if result.set or result.none or result.failed:
        log.info("logos: %d set, %d not at logo.dev, %d failed, %d left", result.set, result.none, result.failed,
                 result.open)
    if result.message:
        log.info("logos: %s", result.message)
    state.set_meta(LAST, json.dumps(asdict(result)))
    return result


def last(state: State) -> Result | None:
    raw = state.meta(LAST)
    return Result(**json.loads(raw)) if raw else None
