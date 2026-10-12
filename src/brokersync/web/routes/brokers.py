"""Broker settings: list, credentials and accounts, fetch again, statement upload, *Neu aufsetzen*."""

from __future__ import annotations

import logging
from collections import Counter

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from starlette.datastructures import UploadFile as StarletteUpload

from ... import reset as reset_mod
from ...model import Kind
from ...sync import AlreadyRunning, refetch_flag, run_lock
from ...wealthfolio import WealthfolioError
from .. import inputs
from ..context import Ctx, accounts_of, redirect

log = logging.getLogger(__name__)
router = APIRouter()

MAX_STATEMENT = 5 * 1024 * 1024
STATEMENT_KINDS = [(Kind.DIVIDEND, "Dividenden"), (Kind.BUY, "Käufe/Einbuchungen"), (Kind.SELL, "Verkäufe"),
                   (Kind.UNKNOWN, "nicht automatisch buchbar")]


@router.get("/brokers", response_class=HTMLResponse)
def brokers_page(request: Request, ctx: Ctx):
    c = ctx.cfg()
    return ctx.render(request, "brokers.html", cfg=c)


@router.get("/brokers/{key}", response_class=HTMLResponse)
def broker_page(request: Request, ctx: Ctx, key: str):
    adapters = ctx.adapters
    if key not in adapters:
        return redirect(request, "/brokers", "Unbekannter Broker.", "error")
    c = ctx.cfg()
    accounts, error = ctx.wf_accounts(c)
    stored = ctx.vault.broker(key).get("credentials", {})
    return ctx.render(request, "broker.html", key=key, adapter=adapters[key], b=c.broker(key), accounts=accounts,
                      wf_error=error, stored={k: bool(v) for k, v in stored.items()},
                      plain={f.name: stored.get(f.name, "") for f in adapters[key].credential_fields if not f.secret},
                      statement_rows=len(ctx.state.statement_rows(key)) if adapters[key].statement_label else 0)


@router.post("/brokers/{key}")
async def broker_save(request: Request, ctx: Ctx, key: str):
    adapters = ctx.adapters
    if key not in adapters:
        return redirect(request, "/brokers", "Unbekannter Broker.", "error")
    form = await request.form()
    c = ctx.cfg()
    b = c.broker(key)
    b.enabled = form.get("enabled") == "on"
    try:
        b.cash_account_id = inputs.text(str(form.get("cash_account_id", "")), "Konto", max_len=100)
        b.portfolio_account_id = inputs.text(str(form.get("portfolio_account_id", "")), "Konto", max_len=100)
        b.savings_account_id = (inputs.text(str(form.get("savings_account_id", "")), "Konto", max_len=100)
                                if adapters[key].savings_label else "")
        b.start_date = inputs.iso_date(str(form.get("start_date", "")), "Startdatum")
    except inputs.Invalid as e:
        return redirect(request, f"/brokers/{key}", str(e), "error")
    if b.enabled and (not b.cash_account_id or not b.portfolio_account_id):
        return redirect(request, f"/brokers/{key}", "Bitte ein Verrechnungs- und ein Depotkonto wählen.", "error")
    if b.enabled and b.cash_account_id == b.portfolio_account_id:
        return redirect(request, f"/brokers/{key}",
                        "Verrechnungs- und Depotkonto müssen verschiedene Konten sein.", "error")
    if b.savings_account_id and b.savings_account_id in (b.cash_account_id, b.portfolio_account_id):
        return redirect(request, f"/brokers/{key}", f"Das {adapters[key].savings_label}-Konto muss ein eigenes "
                        "Wealthfolio-Konto sein, nicht das Verrechnungs- oder Depotkonto.", "error")
    ctx.save(c)
    old = ctx.vault.broker(key).get("credentials", {})
    creds = {}
    for f in adapters[key].credential_fields:
        v = str(form.get(f"cred_{f.name}", ""))
        if len(v) > inputs.MAX_TEXT:
            return redirect(request, f"/brokers/{key}", f"{f.label}: höchstens {inputs.MAX_TEXT} Zeichen.", "error")
        creds[f.name] = v if v or not f.secret else old.get(f.name, "")
    if creds != old:
        ctx.vault.set_broker_credentials(key, creds)
        return redirect(request, f"/brokers/{key}/login", "Gespeichert. Jetzt beim Broker anmelden.")
    return redirect(request, "/", "Gespeichert.")


@router.post("/brokers/{key}/refetch")
def broker_refetch(request: Request, ctx: Ctx, key: str):
    if key not in ctx.adapters:
        return redirect(request, "/brokers", "Unbekannter Broker.", "error")
    ctx.state.set_flag(refetch_flag(key))
    start = ctx.cfg().broker(key).start_date
    since = f"alles ab {start}" if start else "alles, was der Broker liefert"
    return redirect(request, f"/brokers/{key}", f"Der nächste Abruf holt noch einmal {since}. Schon "
                                                "übernommene Buchungen werden nicht doppelt angelegt.")


@router.post("/brokers/{key}/statement")
async def broker_statement(request: Request, ctx: Ctx, key: str):
    """A statement the user exported from the broker (Deutsche Bank depot CSV): keep its rows for the runs."""
    adapters = ctx.adapters
    if key not in adapters or not adapters[key].statement_label:
        return redirect(request, "/brokers", "Für diesen Broker gibt es keinen Datei-Import.", "error")
    file = (await request.form()).get("file")
    if not isinstance(file, StarletteUpload):
        return redirect(request, f"/brokers/{key}", "Bitte eine Datei auswählen.", "error")
    data = await file.read(MAX_STATEMENT + 1)
    if len(data) > MAX_STATEMENT:
        return redirect(request, f"/brokers/{key}", "Die Datei ist zu groß (höchstens 5 MB).", "error")
    try:
        rows = adapters[key].parse_statement(data)
    except ValueError as e:
        return redirect(request, f"/brokers/{key}", str(e), "error")
    new = ctx.state.add_statement_rows(key, rows)
    kinds = Counter(t.kind for t in adapters[key].statement_transactions(rows, None))
    summary = ", ".join(f"{n} {label}" for kind, label in STATEMENT_KINDS if (n := kinds.get(kind)))
    log.info("%s: statement uploaded, %d rows (%d new)", key, len(rows), new)
    return redirect(request, f"/brokers/{key}", f"{len(rows)} Zeilen gelesen ({summary}), {new} davon neu. "
                    "Der nächste Abruf bucht sie; nicht Buchbares steht dann unter „Unbekannte Buchungen“.")


@router.post("/brokers/{key}/reset")
def broker_reset(request: Request, ctx: Ctx, key: str, confirm: str = Form("")):
    if key not in ctx.adapters:
        return redirect(request, "/brokers", "Unbekannter Broker.", "error")
    if not confirm:
        return redirect(request, f"/brokers/{key}", "Bitte bestätige das Neu-Aufsetzen mit dem Häkchen.", "error")
    b = ctx.cfg().broker(key)
    if not b.cash_account_id or not b.portfolio_account_id:
        return redirect(request, f"/brokers/{key}", "Erst die beiden Wealthfolio-Konten wählen.", "error")
    try:
        with run_lock(ctx.data_dir, wait=5), ctx.wf_client(ctx.cfg()) as wf:
            deleted, errors = reset_mod.reset(wf, ctx.state, key, accounts_of(b))
    except AlreadyRunning:
        return redirect(request, f"/brokers/{key}", "Gerade läuft ein Abruf - bitte gleich noch einmal.", "warn")
    except WealthfolioError as e:
        return redirect(request, f"/brokers/{key}", f"Wealthfolio nicht erreichbar: {e}", "error")
    if errors:
        return redirect(request, f"/brokers/{key}", f"{deleted} Buchungen gelöscht, {len(errors)} nicht: "
                                                    + "; ".join(errors[:3]) + " - bitte noch einmal versuchen.",
                        "error")
    return redirect(request, f"/brokers/{key}", f"{deleted} Buchungen des Dienstes gelöscht. Der nächste Abruf "
                                                "holt alles ab dem Startdatum neu.")
