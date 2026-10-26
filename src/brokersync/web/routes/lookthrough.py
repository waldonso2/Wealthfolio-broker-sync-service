"""The page *Durchschau* (waldonso2/wealthfolio-importer-addon#139): every stock across the depot's funds.

The positions of all Wealthfolio accounts are read in the background (#104) and kept for ``READ_SECONDS``;
a fund counts as one once its issuer's holdings file is uploaded - until then it is one line of its own,
marked as without a holdings list, never guessed.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from itertools import combinations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from starlette.datastructures import UploadFile as StarletteUpload

from ... import lookthrough, xray
from ...logos import isin_of
from ...wealthfolio import WealthfolioError
from ..context import AppContext, Ctx, language, redirect, tr
from ..i18n import N_
from ..views import money

log = logging.getLogger(__name__)
router = APIRouter()

READ_SECONDS = 10 * 60
MAX_FILE = 5 * 1024 * 1024
TOPS = {"50": 50, "100": 100, "all": 0}


def _dec(value) -> Decimal:
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return Decimal(0)
    return number if number.is_finite() else Decimal(0)


def _read(ctx: AppContext, c) -> None:
    """Every open position of every Wealthfolio account, added up per asset, value in the base currency."""
    positions: dict[str, dict] = {}
    error = None
    try:
        with ctx.wf_client(c) as wf:
            isins = {a.get("id"): isin_of(a) or "" for a in wf.list_assets()}
            for account in wf.list_accounts():
                for h in wf.holdings(account.id):
                    inst = h.get("instrument") or {}
                    if (h.get("holdingType") or "").lower() != "security" or h.get("isClosed") or not inst.get("id"):
                        continue
                    p = positions.setdefault(inst["id"], {"id": inst["id"], "isin": isins.get(inst["id"], ""),
                                                          "name": inst.get("name") or inst.get("symbol") or inst["id"],
                                                          "value": Decimal(0)})
                    p["value"] += _dec((h.get("marketValue") or {}).get("base"))
    except WealthfolioError as e:
        error = str(e)
    ctx.positions = sorted((p for p in positions.values() if p["value"] > 0), key=lambda p: -p["value"])
    ctx.positions_error, ctx.positions_at = error, time.time()


def _holdings(ctx: AppContext) -> list[xray.Holding]:
    funds = ctx.state.funds()
    out = []
    for p in ctx.positions:
        f = funds.get(p["id"])
        if f:
            lines = tuple(xray.Constituent(k, n, Decimal(w), co, se, cu) for k, n, w, co, se, cu in f["constituents"])
            out.append(xray.Holding(p["id"], p["name"], p["value"], lines))
        else:
            out.append(xray.stock(p["id"], p["isin"] or p["id"], p["name"], p["value"]))
    return out


@router.get("/lookthrough", response_class=HTMLResponse)
def lookthrough_page(request: Request, ctx: Ctx, top: str = "50"):
    if time.time() - ctx.positions_at > READ_SECONDS:
        from .overview import launch

        launch(ctx, N_("Durchschau"), lambda lang: _read(ctx, ctx.cfg()), language(request))
    top = top if top in TOPS else "50"
    funds = ctx.state.funds()
    holdings = _holdings(ctx)
    rows = xray.exposures(holdings)
    stocks = [r for r in rows if r.key != xray.UNASSIGNED]
    unassigned = next((r for r in rows if r.key == xray.UNASSIGNED), None)
    with_list = [h for h in holdings if h.id in funds]
    names = {h.id: h.name for h in holdings}
    return ctx.render(
        request, "lookthrough.html", wf_error=ctx.positions_error, positions=ctx.positions, funds=funds,
        total=sum((p["value"] for p in ctx.positions), Decimal(0)), top=top, tops=list(TOPS),
        stocks=stocks[:TOPS[top]] if TOPS[top] else stocks, count=len(stocks), unassigned=unassigned, names=names,
        effective=xray.effective_count(rows), fund_list=with_list,
        overlap={(a.id, b.id): xray.overlap(a, b) for a, b in combinations(with_list, 2)},
        own={h.id: xray.own_share(h, holdings) for h in with_list},
        reading=ctx.running.is_set() and ctx.job == N_("Durchschau"),
        read_at=datetime.fromtimestamp(ctx.positions_at, UTC) if ctx.positions_at else None)


@router.post("/lookthrough/refresh")
def lookthrough_refresh(request: Request, ctx: Ctx):
    from .overview import start_job

    ctx.positions_at = 0.0
    return start_job(request, ctx, N_("Durchschau"), lambda lang: _read(ctx, ctx.cfg()), "/lookthrough")


@router.post("/lookthrough/upload")
async def lookthrough_upload(request: Request, ctx: Ctx):
    """A fund's holdings file from its issuer: its lines replace what was kept for the fund before."""
    form = await request.form()
    asset_id = str(form.get("asset_id") or "")
    position = next((p for p in ctx.positions if p["id"] == asset_id), None)
    if position is None:
        return redirect(request, "/lookthrough", tr(request, "Unbekannte Position - bitte die Seite neu laden."),
                        "error")
    file = form.get("file")
    if not isinstance(file, StarletteUpload):
        return redirect(request, "/lookthrough", tr(request, "Bitte eine Datei auswählen."), "error")
    data = await file.read(MAX_FILE + 1)
    if len(data) > MAX_FILE:
        return redirect(request, "/lookthrough", tr(request, "Die Datei ist zu groß (höchstens 5 MB)."), "error")
    try:
        lines = lookthrough.parse(data)
    except ValueError as e:
        return redirect(request, "/lookthrough", str(e), "error")
    ctx.state.set_fund(asset_id, position["name"],
                       [[c.key, c.name, str(c.weight), c.country, c.sector, c.currency] for c in lines])
    total = sum((c.weight for c in lines), Decimal(0)) * 100
    log.info("look-through: holdings file of %s, %d lines", asset_id, len(lines))
    text = tr(request, "{name}: {n} Positionen gelesen, zusammen {sum} % des Fonds.", name=position["name"],
              n=len(lines), sum=money(total, language(request)))
    return redirect(request, "/lookthrough", text)
