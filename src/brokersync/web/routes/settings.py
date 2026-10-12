"""Settings pages without a broker of their own: Wealthfolio, notifications, securities, transfers, logos."""

from __future__ import annotations

import re
import secrets

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from ... import logos as logos_mod
from ...notify import Notifier
from ...sync import AlreadyRunning
from .. import inputs
from ..context import Ctx, redirect

router = APIRouter()


# ── Wealthfolio ─────────────────────────────────────────────────────────────
@router.get("/setup/wealthfolio", response_class=HTMLResponse)
def wealthfolio_page(request: Request, ctx: Ctx):
    c = ctx.cfg()
    has_pw = bool(ctx.vault.load().get("wealthfolio_password"))
    return ctx.render(request, "wealthfolio.html", cfg=c, has_password=has_pw)


@router.post("/setup/wealthfolio")
def wealthfolio_save(request: Request, ctx: Ctx, url: str = Form(...), password: str = Form(""),
                     no_password: str = Form("")):
    c = ctx.cfg()
    try:
        c.wealthfolio_url = inputs.http_url(url, "Wealthfolio-Adresse")
    except inputs.Invalid as e:
        return redirect(request, "/setup/wealthfolio", str(e), "error")
    ctx.save(c)
    if no_password:
        ctx.vault.update(lambda d: d.pop("wealthfolio_password", None))
    elif password:
        ctx.vault.update(lambda d: d.__setitem__("wealthfolio_password", password))
    ctx.vault.update(lambda d: d.__setitem__("wealthfolio_checked", False))
    accounts, error = ctx.wf_accounts(c)
    if error:
        return redirect(request, "/setup/wealthfolio", f"Verbindung fehlgeschlagen: {error}", "error")
    ctx.vault.update(lambda d: d.__setitem__("wealthfolio_checked", True))
    return redirect(request, "/brokers", f"Verbunden - {len(accounts)} Konten in Wealthfolio gefunden.")


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
    c = ctx.cfg()
    if delete:
        i = int(delete) if delete.isdigit() else -1
        if 0 <= i < len(c.transfer_patterns):
            c.transfer_patterns.pop(i)
    else:
        try:
            label = inputs.text(label, "Name", max_len=100)
            iban = inputs.iban(iban)
            keyword = inputs.text(keyword, "Stichwort", max_len=100)
            destination = inputs.text(destination, "Zielkonto", max_len=100)
        except inputs.Invalid as e:
            return redirect(request, "/transfers", str(e), "error")
        if not label or not (iban or keyword):
            return redirect(request, "/transfers", "Bitte einen Namen und eine IBAN oder ein Stichwort angeben.",
                            "error")
        c.transfer_patterns.append({"label": label, "iban": iban, "keyword": keyword,
                                    "destinationAccountId": destination})
    ctx.save(c)
    return redirect(request, "/transfers", "Gespeichert.")


# ── notifications ───────────────────────────────────────────────────────────
@router.get("/notifications", response_class=HTMLResponse)
def notifications_page(request: Request, ctx: Ctx):
    c = ctx.cfg()
    if not c.ntfy_topic:
        c.ntfy_topic = f"wealthfolio-sync-{secrets.token_hex(6)}"
    return ctx.render(request, "notifications.html", cfg=c, has_token=bool(c.ntfy_token))


@router.post("/notifications")
def notifications_save(request: Request, ctx: Ctx, server: str = Form(...), topic: str = Form(""),
                       token: str = Form(""), public_url: str = Form(""), details: str = Form(""),
                       test: str = Form("")):
    c = ctx.cfg()
    try:
        c.ntfy_server = inputs.http_url(server, "ntfy-Server")
        c.ntfy_topic = inputs.text(topic, "Thema", max_len=64)
        c.public_url = inputs.http_url(public_url, "Adresse dieser Oberfläche", required=False)
        token = inputs.text(token, "Zugangstoken")
    except inputs.Invalid as e:
        return redirect(request, "/notifications", str(e), "error")
    if c.ntfy_topic and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", c.ntfy_topic):
        return redirect(request, "/notifications", "Thema: nur Buchstaben, Ziffern, - und _.", "error")
    c.ntfy_details = bool(details)
    if token:
        c.ntfy_token = token
    ctx.save(c)
    if test:
        n = ctx.notifier or Notifier(c.ntfy_server, c.ntfy_topic, c.ntfy_token)
        ok = n.send("Wealthfolio Broker Sync", "Testnachricht - Benachrichtigungen funktionieren.",
                    link=c.public_url or None, tags="white_check_mark")
        if not ok:
            return redirect(request, "/notifications", "Testnachricht konnte nicht gesendet werden.", "error")
        return redirect(request, "/notifications", "Testnachricht gesendet.")
    return redirect(request, "/", "Benachrichtigungen gespeichert.")


# ── securities ──────────────────────────────────────────────────────────────
@router.get("/securities", response_class=HTMLResponse)
def securities_page(request: Request, ctx: Ctx):
    return ctx.render(request, "securities.html", mappings=ctx.cfg().security_mappings)


@router.post("/securities")
def securities_save(request: Request, ctx: Ctx, isin: str = Form(...), symbol: str = Form(""),
                    exchange_mic: str = Form(""), delete: str = Form("")):
    c = ctx.cfg()
    isin = isin.strip().upper()
    if delete:
        c.security_mappings.pop(isin, None)
    else:
        try:
            isin = inputs.isin(isin)
            c.security_mappings[isin] = {"symbol": inputs.symbol(symbol), "exchangeMic": inputs.mic(exchange_mic)}
        except inputs.Invalid as e:
            return redirect(request, "/securities", str(e), "error")
    ctx.save(c)
    return redirect(request, "/securities", "Gespeichert.")


# ── logos ───────────────────────────────────────────────────────────────────
@router.get("/logos", response_class=HTMLResponse)
def logos_page(request: Request, ctx: Ctx):
    return ctx.render(request, "logos.html", cfg=ctx.cfg(), has_token=bool(ctx.vault.load().get("logo_dev_token")),
                      last=logos_mod.last(ctx.state))


@router.post("/logos")
def logos_save(request: Request, ctx: Ctx, enabled: str = Form(""), token: str = Form(""),
               remove_token: str = Form(""), fetch: str = Form("")):
    c = ctx.cfg()
    c.logos_enabled = bool(enabled)
    ctx.save(c)
    if remove_token:
        ctx.vault.update(lambda d: d.pop("logo_dev_token", None))
    elif token.strip():
        if len(token.strip()) > 200 or not re.fullmatch(r"[A-Za-z0-9_-]+", token.strip()):
            return redirect(request, "/logos", "Der Schlüssel sieht nicht wie ein logo.dev-Schlüssel aus.", "error")
        ctx.vault.update(lambda d: d.__setitem__("logo_dev_token", token.strip()))
    if not fetch:
        return redirect(request, "/logos", "Gespeichert.")
    if not c.logos_enabled or not ctx.vault.load().get("logo_dev_token"):
        return redirect(request, "/logos", "Erst einschalten und einen logo.dev-Schlüssel eintragen.", "warn")
    try:
        result = ctx.syncer.update_logos()
    except AlreadyRunning:
        return redirect(request, "/logos", "Ein Abruf läuft gerade - bitte gleich noch einmal.", "warn")
    if result is None:
        return redirect(request, "/logos", "Logos konnten nicht geholt werden - siehe Protokoll.", "error")
    text = f"{result.set} Logos gesetzt, {result.none} ohne Logo bei logo.dev, {result.failed} fehlgeschlagen."
    if result.message:
        text += f" {result.message}"
    return redirect(request, "/logos", text, "warn" if result.message or result.failed else "ok")
