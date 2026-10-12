"""The page *Duplikate*: activities the sync created on top of CSV/PDF imports, removed after confirmation."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from ... import config as config_mod
from ... import duplicates as duplicates_mod
from ...sync import AlreadyRunning, run_lock
from ...wealthfolio import WealthfolioError
from ..context import AppContext, Ctx, accounts_of, redirect

router = APIRouter()


def duplicate_report(ctx: AppContext, c: config_mod.Config):
    report, error = [], None
    try:
        with ctx.wf_client(c) as wf:
            for key, b in c.brokers.items():
                if key in ctx.adapters and b.cash_account_id and b.portfolio_account_id:
                    found = duplicates_mod.find(wf, key, accounts_of(b))
                    report.append({"key": key, "label": ctx.adapters[key].label, "found": found})
    except WealthfolioError as e:
        error = str(e)
    return report, error


@router.get("/duplicates", response_class=HTMLResponse)
def duplicates_page(request: Request, ctx: Ctx):
    report, error = duplicate_report(ctx, ctx.cfg())
    return ctx.render(request, "duplicates.html", report=report, wf_error=error)


@router.post("/duplicates")
def duplicates_remove(request: Request, ctx: Ctx, broker: str = Form(...), confirm: str = Form("")):
    if not confirm:
        return redirect(request, "/duplicates", "Bitte bestätige das Löschen mit dem Häkchen.", "error")
    c = ctx.cfg()
    b = c.brokers.get(broker)
    if broker not in ctx.adapters or b is None:
        return redirect(request, "/duplicates", "Unbekannter Broker.", "error")
    try:
        # Not while a sync runs: it could create what is being removed.
        with run_lock(ctx.data_dir, wait=5), ctx.wf_client(c) as wf:
            found = duplicates_mod.find(wf, broker, accounts_of(b))
            deleted, done, errors = duplicates_mod.remove(wf, found)
    except AlreadyRunning:
        return redirect(request, "/duplicates", "Gerade läuft ein Abruf - bitte gleich noch einmal.", "warn")
    except WealthfolioError as e:
        return redirect(request, "/duplicates", f"Wealthfolio: {e}", "error")
    for d in found:
        if d.tx_id in done:
            ctx.state.mark(broker, d.tx_id, "existing", [d.original["id"]])
    message = f"{len(done)} doppelte Vorgänge entfernt ({deleted} Buchungen). Die CSV-/PDF-Buchungen bleiben."
    if done:
        # The check on the status page was computed with the duplicates in it.
        ctx.state.clear_reconcile(broker)
        message += (f" Der Abgleich wird beim nächsten Abruf neu berechnet - auf der Startseite bei "
                    f"{ctx.adapters[broker].label} „Jetzt abrufen“ klicken.")
    if errors:
        message += f" {len(errors)} nicht entfernt: " + "; ".join(errors[:3])
        return redirect(request, "/duplicates", message, "warn")
    return redirect(request, "/duplicates", message)
