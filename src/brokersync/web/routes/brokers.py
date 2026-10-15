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
from ..context import Ctx, accounts_of, language, redirect, tr
from ..i18n import N_

log = logging.getLogger(__name__)
router = APIRouter()

MAX_STATEMENT = 5 * 1024 * 1024
STATEMENT_KINDS = [(Kind.DIVIDEND, N_("Dividenden")), (Kind.BUY, N_("Käufe/Einbuchungen")), (Kind.SELL, N_("Verkäufe")),
                   (Kind.UNKNOWN, N_("nicht automatisch buchbar"))]


@router.get("/brokers", response_class=HTMLResponse)
def brokers_page(request: Request, ctx: Ctx):
    c = ctx.cfg()
    return ctx.render(request, "brokers.html", cfg=c)


@router.get("/brokers/{key}", response_class=HTMLResponse)
def broker_page(request: Request, ctx: Ctx, key: str):
    adapters = ctx.adapters
    if key not in adapters:
        return redirect(request, "/brokers", tr(request, "Unbekannter Broker."), "error")
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
        return redirect(request, "/brokers", tr(request, "Unbekannter Broker."), "error")
    form = await request.form()
    c = ctx.cfg()
    b = c.broker(key)
    b.enabled = form.get("enabled") == "on"
    try:
        b.cash_account_id = inputs.text(str(form.get("cash_account_id", "")), N_("Konto"), max_len=100)
        b.portfolio_account_id = inputs.text(str(form.get("portfolio_account_id", "")), N_("Konto"), max_len=100)
        b.savings_account_id = (inputs.text(str(form.get("savings_account_id", "")), N_("Konto"), max_len=100)
                                if adapters[key].savings_label else "")
        b.start_date = inputs.iso_date(str(form.get("start_date", "")), N_("Startdatum"))
    except inputs.Invalid as e:
        return redirect(request, f"/brokers/{key}", e.text(language(request)), "error")
    if b.enabled and (not b.cash_account_id or not b.portfolio_account_id):
        return redirect(request, f"/brokers/{key}", tr(request, "Bitte ein Verrechnungs- und ein Depotkonto wählen."),
                        "error")
    if b.enabled and b.cash_account_id == b.portfolio_account_id:
        return redirect(request, f"/brokers/{key}",
                        tr(request, "Verrechnungs- und Depotkonto müssen verschiedene Konten sein."), "error")
    if b.savings_account_id and b.savings_account_id in (b.cash_account_id, b.portfolio_account_id):
        return redirect(request, f"/brokers/{key}", tr(request, "Das {label}-Konto muss ein eigenes Wealthfolio-Konto "
                                                       "sein, nicht das Verrechnungs- oder Depotkonto.",
                                                       label=adapters[key].savings_label), "error")
    old = ctx.vault.broker(key).get("credentials", {})
    creds = {}
    for f in adapters[key].credential_fields:
        v = str(form.get(f"cred_{f.name}", ""))
        if len(v) > inputs.MAX_TEXT:  # checked before anything is saved: no half-saved form
            return redirect(request, f"/brokers/{key}", tr(request, "{label}: höchstens {max_len} Zeichen.",
                                                           label=f.label, max_len=inputs.MAX_TEXT), "error")
        creds[f.name] = v if v or not f.secret else old.get(f.name, "")
        if f.ask and form.get(f"forget_{f.name}"):
            creds[f.name] = ""  # typed at every login from now on, never stored (#85)
    ctx.save(c)
    if creds != old:
        ctx.vault.set_broker_credentials(key, creds)
        from .broker_login import start_login  # this POST starts the login with the new credentials (#99)

        request.session["flash"] = {"text": tr(request, "Gespeichert. Jetzt beim Broker anmelden."), "level": "ok"}
        return start_login(request, ctx, key, False)
    return redirect(request, "/", tr(request, "Gespeichert."))


@router.post("/brokers/{key}/refetch")
def broker_refetch(request: Request, ctx: Ctx, key: str):
    if key not in ctx.adapters:
        return redirect(request, "/brokers", tr(request, "Unbekannter Broker."), "error")
    ctx.state.set_flag(refetch_flag(key))
    start = ctx.cfg().broker(key).start_date
    if start:
        text = tr(request, "Der nächste Abruf holt noch einmal alles ab {start}. Schon übernommene Buchungen werden "
                           "nicht doppelt angelegt.", start=start)
    else:
        text = tr(request, "Der nächste Abruf holt noch einmal alles, was der Broker liefert. Schon übernommene "
                           "Buchungen werden nicht doppelt angelegt.")
    return redirect(request, f"/brokers/{key}", text)


@router.post("/brokers/{key}/statement")
async def broker_statement(request: Request, ctx: Ctx, key: str):
    """A statement the user exported from the broker (Deutsche Bank depot CSV): keep its rows for the runs."""
    adapters = ctx.adapters
    if key not in adapters or not adapters[key].statement_label:
        return redirect(request, "/brokers", tr(request, "Für diesen Broker gibt es keinen Datei-Import."), "error")
    file = (await request.form()).get("file")
    if not isinstance(file, StarletteUpload):
        return redirect(request, f"/brokers/{key}", tr(request, "Bitte eine Datei auswählen."), "error")
    data = await file.read(MAX_STATEMENT + 1)
    if len(data) > MAX_STATEMENT:
        return redirect(request, f"/brokers/{key}", tr(request, "Die Datei ist zu groß (höchstens 5 MB)."), "error")
    try:
        rows = adapters[key].parse_statement(data)
        # Read it as the runs will, now: a row they can't read must not reach the state, or every run of
        # this broker fails from then on.
        adapters[key].statement_transactions(rows, None)
    except ValueError as e:
        return redirect(request, f"/brokers/{key}", str(e), "error")
    new = ctx.state.add_statement_rows(key, rows)
    kinds = Counter(t.kind for t in adapters[key].statement_transactions(rows, None))
    summary = ", ".join(f"{n} {tr(request, label)}" for kind, label in STATEMENT_KINDS if (n := kinds.get(kind)))
    log.info("%s: statement uploaded, %d rows (%d new)", key, len(rows), new)
    return redirect(request, f"/brokers/{key}", tr(request, "{rows} Zeilen gelesen ({summary}), {new} davon neu. Der "
                                                           "nächste Abruf bucht sie; nicht Buchbares steht dann unter "
                                                           "„Unbekannte Buchungen“.",
                                                   rows=len(rows), summary=summary, new=new))


@router.post("/brokers/{key}/reset")
def broker_reset(request: Request, ctx: Ctx, key: str, confirm: str = Form("")):
    if key not in ctx.adapters:
        return redirect(request, "/brokers", tr(request, "Unbekannter Broker."), "error")
    if not confirm:
        return redirect(request, f"/brokers/{key}", tr(request, "Bitte bestätige das Neu-Aufsetzen mit dem Häkchen."),
                        "error")
    b = ctx.cfg().broker(key)
    if not b.cash_account_id or not b.portfolio_account_id:
        return redirect(request, f"/brokers/{key}", tr(request, "Erst die beiden Wealthfolio-Konten wählen."), "error")
    try:
        with run_lock(ctx.data_dir, wait=5), ctx.wf_client(ctx.cfg()) as wf:
            deleted, errors = reset_mod.reset(wf, ctx.state, key, accounts_of(b))
    except AlreadyRunning:
        return redirect(request, f"/brokers/{key}", tr(request, "Gerade läuft ein Abruf - bitte gleich noch einmal."),
                        "warn")
    except WealthfolioError as e:
        return redirect(request, f"/brokers/{key}", tr(request, "Wealthfolio nicht erreichbar: {error}", error=e),
                        "error")
    if errors:
        text = tr(request, "{deleted} Buchungen gelöscht, {failed} nicht: {errors} - bitte noch einmal versuchen.",
                  deleted=deleted, failed=len(errors), errors="; ".join(errors[:3]))
        return redirect(request, f"/brokers/{key}", text, "error")
    return redirect(request, f"/brokers/{key}", tr(request, "{deleted} Buchungen des Dienstes gelöscht. Der nächste "
                                                           "Abruf holt alles ab dem Startdatum neu.", deleted=deleted))
