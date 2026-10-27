"""The page *Durchschau* (waldonso2/wealthfolio-importer-addon#139): every stock across the depot's funds,
how the funds overlap, and the cluster risks over the user's limits.

The positions of all Wealthfolio accounts are read in the background (#104) and kept for ``READ_SECONDS``.
A fund counts as one once its holdings list is there - fetched from its issuer (``lookthrough.fetch``, opt-in)
or uploaded; until then it is one line of its own, never guessed.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import re
import time
from datetime import UTC, datetime
from decimal import Decimal
from itertools import combinations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile as StarletteUpload

from ... import fire, lookthrough, meta_keys, xray
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
                ("cash", N_("Bargeld je Konto über (EUR)"), False),
                ("cost", N_("Gesamtkostenquote über (% im Jahr)"), True)]
KIND = {"stock": N_("Einzelaktie"), "country": N_("Land"), "sector": N_("Sektor"), "currency": N_("Fremdwährung"),
        "overlap": N_("Überschneidung"), "effective": N_("Wenige Titel"),
        "cash": N_("Bargeld über der Einlagensicherung"), "cost": N_("Gesamtkostenquote")}
# A broker's fee model (costs.Model): (field, label, a share in percent?, maximum)
COST_FIELDS = [("fixed", N_("Ordergebühr je Verkauf (EUR)"), False, Decimal(1000)),
               ("percent", N_("Ordergebühr je Verkauf (% des Werts)"), True, Decimal(10)),
               ("spread", N_("Spread je Kauf und Verkauf (%)"), True, Decimal(10)),
               ("custody", N_("Depotgebühr im Jahr (EUR)"), False, Decimal(100000))]


def _read(ctx: AppContext, c) -> None:
    error = None
    try:
        with ctx.wf_client(c) as wf:
            depot = lookthrough.read_depot(wf)
    except WealthfolioError as e:
        depot, error = lookthrough.Depot(), str(e)
    ctx.depot, ctx.depot_error, ctx.depot_at = depot, error, time.time()


def _typed(raw: str | None, percent: bool, lang: str) -> str:
    """A saved number as the user typed it, without rounding (a TER of 0,075 stays 0,075)."""
    if not raw:
        return ""
    text = format((Decimal(raw) * (100 if percent else 1)).normalize(), "f")
    return text if lang == "en" else text.replace(".", ",")


def _position(ctx: AppContext, asset_id: str) -> dict | None:
    return next((p for p in ctx.depot.positions if p["id"] == asset_id), None)


FUND_TOP = 50  # a fund's largest positions shown under it; all of them with ?full=<asset id>


@router.get("/lookthrough", response_class=HTMLResponse)
def lookthrough_page(request: Request, ctx: Ctx, top: str = "50", full: str = ""):
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
    cost = lookthrough.cost(state, depot, c)
    found = xray.findings(holdings, limits, depot.base, depot.cash, cost.rate)
    return ctx.render(
        request, "lookthrough.html", wf_error=ctx.depot_error, positions=depot.positions, funds=funds,
        status={p["id"]: lookthrough.status(state, p["id"]) for p in depot.positions},
        total=sum((p["value"] for p in depot.positions), Decimal(0)), top=top, tops=list(TOPS),
        stocks=stocks[:TOPS[top]] if TOPS[top] else stocks, count=len(stocks), full=full, fund_top=FUND_TOP,
        largest={k: sorted(f["constituents"], key=lambda c: -Decimal(c[2])) for k, f in funds.items()},
        unassigned=next((r for r in rows if r.key == xray.UNASSIGNED), None),
        names={h.id: h.name for h in holdings}, effective=xray.effective_count(rows), fund_list=with_list,
        overlap={(a.id, b.id): xray.overlap(a, b) for a, b in combinations(with_list, 2)},
        own={h.id: xray.own_share(h, holdings) for h in with_list},
        risks=[f for f in found if f.key not in accepted], accepted=[f for f in found if f.key in accepted],
        kind=KIND, cfg=c, limit_fields=LIMIT_FIELDS, limits=limits, base=depot.base, cost=cost,
        ters={p["id"]: lookthrough.ter(lookthrough.status(state, p["id"])) for p in depot.positions},
        cost_fields=COST_FIELDS, brokers=[k for k in c.brokers if k in ctx.adapters],
        typed=lambda raw, percent: _typed(raw, percent, language(request)),
        written=json.loads(state.meta(meta_keys.TAXONOMIES_LAST()) or "null"),
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
        c.lookthrough_write = bool(form.get("write"))
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


@router.post("/lookthrough/costs")
async def lookthrough_costs(request: Request, ctx: Ctx):
    """The brokers' fee models, the whole form checked before anything is saved; an empty field: unknown."""
    form = await request.form()
    c = ctx.cfg()
    models: dict[str, dict[str, str]] = {}
    try:
        for key in (k for k in c.brokers if k in ctx.adapters):
            for name, label, percent, high in COST_FIELDS:
                value = _plain(str(form.get(f"{key}:{name}") or "").strip())
                if value:
                    number = inputs.decimal(value, label, minimum=Decimal(0), maximum=high)
                    models.setdefault(key, {})[name] = str(number / 100 if percent else number)
    except inputs.Invalid as e:
        return redirect(request, "/lookthrough", e.text(language(request)), "error")
    ctx.update(lambda c: setattr(c, "cost_models", models))
    return redirect(request, "/lookthrough", tr(request, "Gespeichert."))


@router.post("/lookthrough/ter")
def lookthrough_ter(request: Request, ctx: Ctx, asset_id: str = Form(...), ter: str = Form("")):
    """A position's TER typed by the user, used before the issuer's; empty: the issuer's again."""
    if _position(ctx, asset_id) is None:
        return redirect(request, "/lookthrough", tr(request, "Unbekannte Position - bitte die Seite neu laden."),
                        "error")
    try:
        value = inputs.decimal(ter.strip(), N_("Laufende Kosten (TER, % im Jahr)"), minimum=Decimal(0),
                               maximum=Decimal(5)) if ter.strip() else None
    except inputs.Invalid as e:
        return redirect(request, "/lookthrough", e.text(language(request)), "error")
    lookthrough.set_status(ctx.state, asset_id, ter_manual=str(value / 100) if value is not None else "")
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
def lookthrough_fetch(request: Request, ctx: Ctx, asset_id: str = Form("")):
    """*Jetzt abrufen*: every fund's holdings list now, or one position's (``asset_id``), in the background."""
    c = ctx.cfg()
    if not c.lookthrough_enabled and not c.lookthrough_write:
        return redirect(request, "/lookthrough", tr(request, "Erst den automatischen Abruf einschalten."), "warn")
    if asset_id and _position(ctx, asset_id) is None:
        return redirect(request, "/lookthrough", tr(request, "Unbekannte Position - bitte die Seite neu laden."),
                        "error")
    from .overview import start_job

    def work(lang: str) -> tuple[str, str]:
        try:
            result = ctx.syncer.update_funds(only=asset_id)
        except AlreadyRunning:
            return translate("Ein Abruf läuft gerade - bitte gleich noch einmal.", lang), "warn"
        ctx.depot_at = 0.0
        if result is None:
            return translate("Die Positionslisten konnten nicht geholt werden - siehe Protokoll.", lang), "error"
        text = translate("{fetched} Positionslisten geholt, {none} ohne Quelle (hochladen), {failed} fehlgeschlagen.",
                         lang, fetched=result.fetched, none=result.none, failed=len(result.errors))
        return text, "warn" if result.errors else "ok"

    return start_job(request, ctx, N_("Positionslisten abrufen"), work, "/lookthrough")


@router.post("/lookthrough/taxonomies/remove")
def lookthrough_remove(request: Request, ctx: Ctx):
    """Take the service's countries and sectors out of Wealthfolio again, and stop writing them."""
    ctx.update(lambda c: setattr(c, "lookthrough_write", False))
    from .overview import start_job

    def work(lang: str) -> tuple[str, str]:
        try:
            n = ctx.syncer.remove_taxonomies()
        except AlreadyRunning:
            return translate("Ein Abruf läuft gerade - bitte gleich noch einmal.", lang), "warn"
        except WealthfolioError as e:
            return translate("Wealthfolio: {error}", lang, error=str(e)), "error"
        ctx.state.delete_meta(meta_keys.TAXONOMIES_LAST())
        return translate("{n} Zuordnungen aus Wealthfolio entfernt.", lang, n=n), "ok"

    return start_job(request, ctx, N_("Zuordnungen entfernen"), work, "/lookthrough")


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
        lines = await run_in_threadpool(lookthrough.parse_file, data)  # never on the event loop: the UI stays
    except (ValueError, lookthrough.SourceError) as e:
        return redirect(request, "/lookthrough", tr(request, str(e)), "error")
    ctx.state.set_fund(asset_id, position["name"], lookthrough.rows(lines))
    lookthrough.set_status(ctx.state, asset_id, status="upload", source="Upload", error="", lines=len(lines))
    total = sum((c.weight for c in lines), Decimal(0)) * 100
    log.info("look-through: holdings file of %s, %d lines", asset_id, len(lines))
    text = tr(request, "{name}: {n} Positionen gelesen, zusammen {sum} % des Fonds.", name=position["name"],
              n=len(lines), sum=money(total, language(request)))
    return redirect(request, "/lookthrough", text)


# ── FIRE (fire.py) ───────────────────────────────────────────────────────────
# (field of fire.Inputs, label, kind, minimum, maximum, default as typed); percent: typed in %, stored as share.
FIRE_FIELDS = [
    ("expenses", N_("Ausgaben im Jahr (EUR, ohne Krankenversicherung)"), "money", 0, 10_000_000, ""),
    ("savings", N_("Sparrate im Jahr (EUR)"), "money", 0, 10_000_000, "0"),
    ("capital", N_("Vermögen (EUR, leer: aus Wealthfolio)"), "money", 0, 1e11, ""),
    ("age", N_("Alter heute"), "int", 16, 100, "40"),
    ("retire_age", N_("Alter bei Rückzug aus dem Beruf"), "int", 16, 100, "55"),
    ("pension_age", N_("Alter bei Beginn der gesetzlichen Rente"), "int", 50, 100, "67"),
    ("pension", N_("Rente netto im Jahr (EUR, heutiger Wert)"), "money", 0, 1_000_000, "0"),
    ("part_time", N_("Teilzeit-Einkommen netto im Jahr bis zur Rente (EUR, Barista FIRE)"), "money", 0, 1_000_000,
     "0"),
    ("real_return", N_("Rendite nach Inflation (% im Jahr)"), "percent", -10, 20, "5"),
    ("costs", N_("Kosten (% im Jahr, leer: aus der Durchschau)"), "percent", 0, 5, ""),
    ("safe_rate", N_("Entnahmerate (% im Jahr)"), "percent", 1, 10, "3,5"),
    ("gain_share", N_("Gewinnanteil eines Verkaufs (%, leer: aus Wealthfolio)"), "percent", 0, 100, ""),
    ("church", N_("Kirchensteuer (%: 0, 8 oder 9)"), "percent", 0, 9, "0"),
    ("private", N_("Private Krankenversicherung im Jahr (EUR)"), "money", 0, 200_000, "0"),
]
FIRE_FLAGS = [("joint", N_("Zusammenveranlagt (2.000 EUR Sparerpauschbetrag)")),
              ("gkv", N_("Freiwillig gesetzlich versichert (sonst privat)")),
              ("kvdr", N_("In der Rente pflichtversichert (KVdR): kein Beitrag auf Kapitalerträge")),
              ("childless", N_("Kinderlos (höherer Pflegebeitrag)")),
              ("equity_fund", N_("Aktienfonds (30 % Teilfreistellung)"))]
FIRE_TARGETS = {"lean": N_("Lean FIRE (75 % der Ausgaben)"), "regular": N_("FIRE"),
                "fat": N_("Fat FIRE (150 % der Ausgaben)"), "barista": N_("Barista FIRE (mit Teilzeit bis zur Rente)")}


def _plain(value: str) -> str:
    """Thousands separators out ("30.000", "30,000.50"): ``inputs.decimal`` takes "," and "." as decimal mark."""
    value = value.replace(" ", "").replace("\xa0", "")
    if re.fullmatch(r"-?\d{1,3}(\.\d{3})+(,\d+)?", value):
        return value.replace(".", "")
    if re.fullmatch(r"-?\d{1,3}(,\d{3})+(\.\d+)?", value):
        return value.replace(",", "")
    return value


def _fire_inputs(saved: dict[str, str], depot: lookthrough.Depot, cost: Decimal | None) -> fire.Inputs | None:
    if not saved.get("expenses"):
        return None
    values: dict = {}
    for key, _, kind, *_rest in FIRE_FIELDS:
        raw = saved.get(key, "")
        if raw == "":
            continue
        number = Decimal(raw)
        values[key] = int(number) if kind == "int" else float(number)
    values.setdefault("capital", float(depot.total))
    values.setdefault("costs", float(cost or 0))
    if "gain_share" not in values and depot.gain_share is not None:
        values["gain_share"] = float(depot.gain_share)
    for key, _ in FIRE_FLAGS:
        values[key] = saved.get(key, "1" if key in ("gkv", "equity_fund") else "") == "1"
    return fire.Inputs(**values)


@router.get("/lookthrough/fire", response_class=HTMLResponse)
def fire_page(request: Request, ctx: Ctx):
    if time.time() - ctx.depot_at > READ_SECONDS:
        from .overview import launch

        launch(ctx, N_("Durchschau"), lambda lang: _read(ctx, ctx.cfg()), language(request))
    saved = ctx.cfg().fire
    lang = language(request)

    def shown(key: str, kind: str, default: str) -> str:
        raw = saved.get(key)
        if raw is None or raw == "":
            return default if key not in ("capital", "gain_share", "costs") else ""
        number = Decimal(raw) * (100 if kind == "percent" else 1)
        return str(int(number)) if kind == "int" else re.sub(r"[,.]00$", "", money(number, lang))

    cost = lookthrough.cost(ctx.state, ctx.depot, ctx.cfg()).rate
    inputs_ = _fire_inputs(saved, ctx.depot, cost)
    return ctx.render(request, "fire.html", fields=[(k, label, kind, shown(k, kind, d)) for k, label, kind, _, _, d
                                                    in FIRE_FIELDS],
                      flags=[(k, label, saved.get(k, "1" if k in ("gkv", "equity_fund") else "") == "1")
                             for k, label in FIRE_FLAGS],
                      result=fire.plan(inputs_) if inputs_ else None, inputs=inputs_, labels=FIRE_TARGETS,
                      free=fire.plan(dataclasses.replace(inputs_, costs=0)) if inputs_ and inputs_.costs else None,
                      depot_cost=cost,
                      depot_total=ctx.depot.total, gain_share=ctx.depot.gain_share, wf_error=ctx.depot_error,
                      rules=fire)


@router.post("/lookthrough/fire")
async def fire_save(request: Request, ctx: Ctx):
    form = await request.form()
    saved: dict[str, str] = {}
    try:
        for key, label, kind, low, high, _ in FIRE_FIELDS:
            value = _plain(str(form.get(key) or "").strip())
            if not value:
                continue
            number = inputs.decimal(value, label, minimum=Decimal(str(low)), maximum=Decimal(str(high)))
            if kind == "int" and number != int(number):
                raise inputs.Invalid(N_("{label}: bitte eine ganze Zahl."), label=label)
            saved[key] = str(number / 100 if kind == "percent" else number)
        if not saved.get("expenses"):
            raise inputs.Invalid(N_("{label}: bitte eintragen."), label=FIRE_FIELDS[0][1])
    except inputs.Invalid as e:
        return redirect(request, "/lookthrough/fire", e.text(language(request)), "error")
    for key, _ in FIRE_FLAGS:
        saved[key] = "1" if form.get(key) else "0"
    ctx.update(lambda c: setattr(c, "fire", saved))
    return redirect(request, "/lookthrough/fire", tr(request, "Gespeichert."))
