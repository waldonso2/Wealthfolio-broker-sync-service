"""The page *Durchschau* (waldonso2/wealthfolio-importer-addon#139): every stock across the depot's funds,
how the funds overlap, and the cluster risks over the user's limits.

The positions of all Wealthfolio accounts are read in the background (#104) and kept for ``READ_SECONDS``.
A fund counts as one once its holdings list is there - fetched from its issuer (``lookthrough.fetch``, opt-in)
or uploaded; until then it is one line of its own, never guessed.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime
from decimal import Decimal
from itertools import combinations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from starlette.datastructures import UploadFile as StarletteUpload

from ... import lookthrough, meta_keys, xray
from ...sync import AlreadyRunning
from ...wealthfolio import WealthfolioError
from .. import inputs
from ..context import AppContext, Ctx, language, redirect, tr
from ..i18n import N_, translate
from ..views import money

log = logging.getLogger(__name__)
router = APIRouter()

READ_SECONDS = 10 * 60
MAX_FILE = 5 * 1024 * 1024
TOPS = {"50": 50, "100": 100, "all": 0}
# The limits' form fields: (key of xray.LIMITS, label, a share in percent?)
LIMIT_FIELDS = [("stock", N_("Einzelaktie über (%)"), True), ("country", N_("Land über (%)"), True),
                ("sector", N_("Sektor über (%)"), True), ("currency", N_("Fremdwährung über (%)"), True),
                ("overlap", N_("Überschneidung zweier ETFs über (%)"), True),
                ("effective", N_("Effektive Titelzahl unter"), False),
                ("cash", N_("Bargeld je Konto über (EUR)"), False)]
KIND = {"stock": N_("Einzelaktie"), "country": N_("Land"), "sector": N_("Sektor"), "currency": N_("Fremdwährung"),
        "overlap": N_("Überschneidung"), "effective": N_("Wenige Titel"),
        "cash": N_("Bargeld über der Einlagensicherung")}


def _read(ctx: AppContext, c) -> None:
    error = None
    try:
        with ctx.wf_client(c) as wf:
            depot = lookthrough.read_depot(wf)
    except WealthfolioError as e:
        depot, error = lookthrough.Depot(), str(e)
    ctx.depot, ctx.depot_error, ctx.depot_at = depot, error, time.time()


def _position(ctx: AppContext, asset_id: str) -> dict | None:
    return next((p for p in ctx.depot.positions if p["id"] == asset_id), None)


@router.get("/lookthrough", response_class=HTMLResponse)
def lookthrough_page(request: Request, ctx: Ctx, top: str = "50"):
    if time.time() - ctx.depot_at > READ_SECONDS:
        from .overview import launch

        launch(ctx, N_("Durchschau"), lambda lang: _read(ctx, ctx.cfg()), language(request))
    c = ctx.cfg()
    top = top if top in TOPS else "50"
    depot, state = ctx.depot, ctx.state
    funds = state.funds()
    holdings = lookthrough.holdings(state, depot)
    rows = xray.exposures(holdings)
    stocks = [r for r in rows if r.key != xray.UNASSIGNED]
    with_list = [h for h in holdings if h.id in funds]
    limits = lookthrough.limits(c.xray_limits)
    accepted = lookthrough.accepted(state)
    found = xray.findings(holdings, limits, depot.base, depot.cash)
    return ctx.render(
        request, "lookthrough.html", wf_error=ctx.depot_error, positions=depot.positions, funds=funds,
        status={p["id"]: lookthrough.status(state, p["id"]) for p in depot.positions},
        total=sum((p["value"] for p in depot.positions), Decimal(0)), top=top, tops=list(TOPS),
        stocks=stocks[:TOPS[top]] if TOPS[top] else stocks, count=len(stocks),
        unassigned=next((r for r in rows if r.key == xray.UNASSIGNED), None),
        names={h.id: h.name for h in holdings}, effective=xray.effective_count(rows), fund_list=with_list,
        overlap={(a.id, b.id): xray.overlap(a, b) for a, b in combinations(with_list, 2)},
        own={h.id: xray.own_share(h, holdings) for h in with_list},
        risks=[f for f in found if f.key not in accepted], accepted=[f for f in found if f.key in accepted],
        kind=KIND, cfg=c, limit_fields=LIMIT_FIELDS, limits=limits, base=depot.base,
        reading=ctx.running.is_set() and ctx.job in (N_("Durchschau"), N_("Positionslisten abrufen")),
        read_at=datetime.fromtimestamp(ctx.depot_at, UTC) if ctx.depot_at else None)


@router.post("/lookthrough/refresh")
def lookthrough_refresh(request: Request, ctx: Ctx):
    from .overview import start_job

    ctx.depot_at = 0.0
    return start_job(request, ctx, N_("Durchschau"), lambda lang: _read(ctx, ctx.cfg()), "/lookthrough")


@router.post("/lookthrough/settings")
async def lookthrough_settings(request: Request, ctx: Ctx):
    """Fetching on or off, and the limits of the cluster risks - the whole form checked before anything is saved."""
    form = await request.form()
    saved: dict[str, str] = {}
    try:
        for key, label, percent in LIMIT_FIELDS:
            value = str(form.get(key) or "").strip()
            if not value:
                continue
            number = inputs.decimal(value, label, minimum=Decimal(0),
                                    maximum=Decimal(100) if percent else Decimal("1e9"))
            saved[key] = str(number / 100 if percent else number)
    except inputs.Invalid as e:
        return redirect(request, "/lookthrough", e.text(language(request)), "error")

    def change(c) -> None:
        c.lookthrough_enabled = bool(form.get("enabled"))
        c.xray_limits = saved

    ctx.update(change)
    return redirect(request, "/lookthrough", tr(request, "Gespeichert."))


@router.post("/lookthrough/accept")
def lookthrough_accept(request: Request, ctx: Ctx, key: str = Form(...), action: str = Form("accept")):
    """A cluster risk the user holds on purpose: no longer listed as one nor notified - or listed again."""
    try:
        key = inputs.text(key, N_("Meldung"), max_len=400)
    except inputs.Invalid as e:
        return redirect(request, "/lookthrough", e.text(language(request)), "error")
    keys = lookthrough.accepted(ctx.state)
    keys = keys - {key} if action == "undo" else keys | {key}
    ctx.state.set_meta(meta_keys.XRAY_ACCEPTED(), json.dumps(sorted(keys)))
    return redirect(request, "/lookthrough", tr(request, "Gespeichert."))


@router.post("/lookthrough/source")
def lookthrough_source(request: Request, ctx: Ctx, asset_id: str = Form(...), url: str = Form("")):
    """The iShares product page of a fund, so the fetch finds its holdings; empty: forget it."""
    if _position(ctx, asset_id) is None:
        return redirect(request, "/lookthrough", tr(request, "Unbekannte Position - bitte die Seite neu laden."),
                        "error")
    url = url.strip()
    if url and (len(url) > 300 or not lookthrough.ishares_page(url)):
        return redirect(request, "/lookthrough", tr(request, "Das ist keine Produktseite von iShares "
                                                             "(https://www.ishares.com/…/products/<Nummer>/<Name>)."),
                        "error")
    lookthrough.set_status(ctx.state, asset_id, url=url, status="")  # due at the next fetch
    return redirect(request, "/lookthrough", tr(request, "Gespeichert."))


@router.post("/lookthrough/fetch")
def lookthrough_fetch(request: Request, ctx: Ctx):
    """*Jetzt abrufen*: every fund's holdings list now, in the background."""
    if not ctx.cfg().lookthrough_enabled:
        return redirect(request, "/lookthrough", tr(request, "Erst den automatischen Abruf einschalten."), "warn")
    from .overview import start_job

    def work(lang: str) -> tuple[str, str]:
        try:
            result = ctx.syncer.update_funds()
        except AlreadyRunning:
            return translate("Ein Abruf läuft gerade - bitte gleich noch einmal.", lang), "warn"
        ctx.depot_at = 0.0
        if result is None:
            return translate("Die Positionslisten konnten nicht geholt werden - siehe Protokoll.", lang), "error"
        text = translate("{fetched} Positionslisten geholt, {none} ohne Quelle (hochladen), {failed} fehlgeschlagen.",
                         lang, fetched=result.fetched, none=result.none, failed=len(result.errors))
        return text, "warn" if result.errors else "ok"

    return start_job(request, ctx, N_("Positionslisten abrufen"), work, "/lookthrough")


@router.post("/lookthrough/upload")
async def lookthrough_upload(request: Request, ctx: Ctx):
    """A fund's holdings file from its issuer (CSV or XLSX): its lines replace what was kept before."""
    form = await request.form()
    asset_id = str(form.get("asset_id") or "")
    position = _position(ctx, asset_id)
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
        lines = lookthrough.parse_file(data)
    except (ValueError, lookthrough.SourceError) as e:
        return redirect(request, "/lookthrough", str(e), "error")
    ctx.state.set_fund(asset_id, position["name"], lookthrough.rows(lines))
    lookthrough.set_status(ctx.state, asset_id, status="upload", source="Upload", error="", lines=len(lines))
    total = sum((c.weight for c in lines), Decimal(0)) * 100
    log.info("look-through: holdings file of %s, %d lines", asset_id, len(lines))
    text = tr(request, "{name}: {n} Positionen gelesen, zusammen {sum} % des Fonds.", name=position["name"],
              n=len(lines), sum=money(total, language(request)))
    return redirect(request, "/lookthrough", text)
