"""The page *Prüfung*: account check (read only), gaps to book again or ignore, opening positions."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ... import audit, meta_keys
from ...sync import opening_id, refetch_flag
from ...wealthfolio import WealthfolioError
from .. import inputs
from ..context import AppContext, Ctx, accounts_of, redirect

router = APIRouter()


@router.get("/check", response_class=HTMLResponse)
def check_page(request: Request, ctx: Ctx):
    c = ctx.cfg()
    adapters, state = ctx.adapters, ctx.state
    report, error = [], None
    try:
        with ctx.wf_client(c) as wf:
            names = {a.id: a.name for a in wf.list_accounts()}
            for key, b in c.brokers.items():
                if key in adapters and b.cash_account_id and b.portfolio_account_id:
                    accounts = accounts_of(b)
                    report.append({"key": key, "label": adapters[key].label, "result": audit.run(wf, accounts),
                                   "cash_name": names.get(b.cash_account_id, "Verrechnungskonto"),
                                   "depot_name": names.get(b.portfolio_account_id, "Depotkonto"),
                                   "depot_id": b.portfolio_account_id})
    except WealthfolioError as e:
        error = str(e)
    brokers = [{"key": k, "label": adapters[k].label, "full": adapters[k].full_history, "gaps": state.gaps(k),
                "checked": bool(state.last_run(k)), "missing": missing_positions(ctx, k),
                "openings": [{**o, "booked": opening_id(o["isin"], o["day"]) in synced}
                             for synced in [state.synced(k)] for o in state.openings(k)]}
               for k, b in c.brokers.items() if k in adapters and b.cash_account_id and b.portfolio_account_id]
    return ctx.render(request, "check.html", report=report, wf_error=error, source=audit.source,
                      effect=audit.cash_effect, brokers=brokers)


def missing_positions(ctx: AppContext, key: str) -> list[dict]:
    """Positions the broker holds more of than Wealthfolio (last check), pre-filled for an opening position."""
    state = ctx.state
    if not ctx.adapters[key].reports_positions:
        return []
    known = {p["isin"]: p for p in json.loads(state.meta(meta_keys.POSITIONS(key)) or "[]")}
    entered = {o["isin"] for o in state.openings(key)}
    out = []
    for d in (state.reconcile(key) or {}).get("deviations", []):
        if d["kind"] != "position" or ":" in d["key"] or d["key"] in entered:
            continue
        try:
            missing = Decimal(d["broker"]) - Decimal(d["wealthfolio"])
        except InvalidOperation:
            continue
        if missing > 0:
            p = known.get(d["key"], {})
            out.append({"isin": d["key"], "name": d["name"], "shares": format(missing.normalize(), "f"),
                        "price": p.get("cost") or "", "currency": p.get("currency") or "EUR"})
    return out


@router.post("/check/{key}/opening")
async def check_opening(request: Request, ctx: Ctx, key: str):
    """An opening position: bought on a day before the start date; the next run books it."""
    state = ctx.state
    if key not in ctx.adapters:
        return redirect(request, "/check", "Unbekannter Broker.", "error")
    form = await request.form()
    isin = str(form.get("isin", "")).strip().upper()
    day = str(form.get("day", "")).strip()
    if form.get("action") != "delete":
        try:
            isin = inputs.security_id(isin)
        except inputs.Invalid as e:
            return redirect(request, "/check", str(e), "error")
    if form.get("action") == "delete":
        if opening_id(isin, day) not in state.synced(key):
            state.delete_opening(key, isin, day)
        return redirect(request, "/check", "Anfangsbestand entfernt.")
    try:
        shares = Decimal(str(form.get("shares", "")).replace(",", "."))
        price = Decimal(str(form.get("price", "")).replace(",", "."))
        fee = Decimal(str(form.get("fee") or "0").replace(",", "."))
        bought = date.fromisoformat(day)
    except (InvalidOperation, ValueError):
        return redirect(request, "/check", "Bitte Kaufdatum, Stückzahl, Kurs und Gebühr als Zahlen angeben.",
                        "error")
    if shares <= 0 or price <= 0 or fee < 0 or bought > date.today() or not isin:
        return redirect(request, "/check", "Stückzahl und Kurs müssen größer 0 sein, die Gebühr mindestens 0, "
                                           "das Kaufdatum darf nicht in der Zukunft liegen.", "error")
    known = {p["isin"]: p for p in json.loads(state.meta(meta_keys.POSITIONS(key)) or "[]")}
    p = known.get(isin, {})
    name = str(form.get("name") or p.get("name") or isin)[:200]
    state.add_opening(key, isin, bought.isoformat(), name,
                      str(shares), str(price), str(fee), p.get("currency") or "EUR")
    return redirect(request, "/check", f"Anfangsbestand {isin} gespeichert - der nächste Abruf bucht ihn als Kauf "
                                       f"am {bought:%d.%m.%Y} samt Einzahlung des Kaufbetrags.")


@router.post("/check/{key}")
async def check_decide(request: Request, ctx: Ctx, key: str):
    """The user's decision on transactions missing in Wealthfolio: book them again, or ignore them."""
    state = ctx.state
    if key not in ctx.adapters:
        return redirect(request, "/check", "Unbekannter Broker.", "error")
    form = await request.form()
    action = str(form.get("action", ""))
    open_refs = {g["ref"] for g in state.gaps(key) if g["kind"] != "orphan"}
    tx_ids = [str(t) for t in form.getlist("tx") if str(t) in open_refs]
    if not tx_ids:
        return redirect(request, "/check", "Keine Buchung ausgewählt.", "warn")
    if action == "ignore":
        state.ignore(key, tx_ids)
        return redirect(request, "/check", f"{len(tx_ids)} Buchung(en) werden nicht mehr gebucht.")
    if action != "rebook":
        return redirect(request, "/check", "Unbekannte Aktion.", "error")
    state.forget(key, tx_ids)
    if not ctx.adapters[key].full_history:
        state.set_flag(refetch_flag(key))  # they may lie before the next run's window
    return redirect(request, "/check", f"{len(tx_ids)} Buchung(en) legt der nächste Abruf wieder an – außer "
                                       "sie sind inzwischen in Wealthfolio (z. B. aus einem Import).")
