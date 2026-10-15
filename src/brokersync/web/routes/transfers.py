"""The page *Überträge*: patterns that make an outbound payment to an own account a transfer.

Listed under *Einstellungen* only while patterns are saved (waldonso2/wealthfolio-importer-addon#84), so a saved
pattern - it applies to every run - can always be seen and deleted.
"""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from .. import inputs
from ..context import Ctx, language, redirect, tr
from ..i18n import N_

router = APIRouter()


# ── transfer patterns ───────────────────────────────────────────────────────
@router.get("/transfers", response_class=HTMLResponse)
def transfers_page(request: Request, ctx: Ctx):
    c = ctx.cfg()
    accounts, error = ctx.wf_accounts(c)
    names = {a.id: f"{a.name} ({a.currency})" for a in accounts}
    return ctx.render(request, "transfers.html", patterns=c.transfer_patterns, accounts=accounts, names=names,
                      wf_error=error)


@router.post("/transfers")
def transfers_save(request: Request, ctx: Ctx, label: str = Form(""), iban: str = Form(""),
                   keyword: str = Form(""), destination: str = Form(""), delete: str = Form("")):
    if delete:
        i = int(delete) if delete.isdigit() else -1

        def drop(c) -> None:
            if 0 <= i < len(c.transfer_patterns):
                c.transfer_patterns.pop(i)

        ctx.update(drop)
    else:
        try:
            label = inputs.text(label, N_("Name"), max_len=100)
            iban = inputs.iban(iban)
            keyword = inputs.text(keyword, N_("Stichwort"), max_len=100)
            destination = inputs.text(destination, N_("Zielkonto"), max_len=100)
        except inputs.Invalid as e:
            return redirect(request, "/transfers", e.text(language(request)), "error")
        if not label or not (iban or keyword):
            return redirect(request, "/transfers",
                            tr(request, "Bitte einen Namen und eine IBAN oder ein Stichwort angeben."), "error")
        pattern = {"label": label, "iban": iban, "keyword": keyword, "destinationAccountId": destination}
        ctx.update(lambda c: c.transfer_patterns.append(pattern))
    return redirect(request, "/transfers", tr(request, "Gespeichert."))
