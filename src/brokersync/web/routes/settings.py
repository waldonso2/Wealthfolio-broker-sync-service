"""Settings pages without a broker of their own: Wealthfolio, notifications, securities, logos."""

from __future__ import annotations

import re
import secrets

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from ... import logos as logos_mod
from ...notify import Notifier
from ...sync import AlreadyRunning
from .. import inputs
from ..context import Ctx, language, redirect, tr
from ..i18n import N_

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
        c.wealthfolio_url = inputs.http_url(url, N_("Wealthfolio-Adresse"))
    except inputs.Invalid as e:
        return redirect(request, "/setup/wealthfolio", e.text(language(request)), "error")
    ctx.save(c)
    if no_password:
        ctx.vault.update(lambda d: d.pop("wealthfolio_password", None))
    elif password:
        ctx.vault.update(lambda d: d.__setitem__("wealthfolio_password", password))
    ctx.vault.update(lambda d: d.__setitem__("wealthfolio_checked", False))
    accounts, error = ctx.wf_accounts(c)
    if error:
        return redirect(request, "/setup/wealthfolio", tr(request, "Verbindung fehlgeschlagen: {error}", error=error),
                        "error")
    ctx.vault.update(lambda d: d.__setitem__("wealthfolio_checked", True))
    return redirect(request, "/brokers", tr(request, "Verbunden - {n} Konten in Wealthfolio gefunden.",
                                            n=len(accounts)))


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
        c.ntfy_server = inputs.http_url(server, N_("ntfy-Server"))
        c.ntfy_topic = inputs.text(topic, N_("Thema"), max_len=64)
        c.public_url = inputs.http_url(public_url, N_("Adresse dieser Oberfläche"), required=False)
        token = inputs.text(token, N_("Zugangstoken"))
    except inputs.Invalid as e:
        return redirect(request, "/notifications", e.text(language(request)), "error")
    if c.ntfy_topic and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", c.ntfy_topic):
        return redirect(request, "/notifications", tr(request, "Thema: nur Buchstaben, Ziffern, - und _."), "error")
    c.ntfy_details = bool(details)
    if token:
        c.ntfy_token = token
    ctx.save(c)
    if test:
        n = ctx.notifier or Notifier(c.ntfy_server, c.ntfy_topic, c.ntfy_token)
        ok = n.send("Wealthfolio Broker Sync", "Testnachricht - Benachrichtigungen funktionieren.",
                    link=c.public_url or None, tags="white_check_mark")
        if not ok:
            return redirect(request, "/notifications", tr(request, "Testnachricht konnte nicht gesendet werden."),
                            "error")
        return redirect(request, "/notifications", tr(request, "Testnachricht gesendet."))
    return redirect(request, "/", tr(request, "Benachrichtigungen gespeichert."))


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
            return redirect(request, "/securities", e.text(language(request)), "error")
    ctx.save(c)
    return redirect(request, "/securities", tr(request, "Gespeichert."))


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
            return redirect(request, "/logos", tr(request, "Der Schlüssel sieht nicht wie ein logo.dev-Schlüssel aus."),
                            "error")
        ctx.vault.update(lambda d: d.__setitem__("logo_dev_token", token.strip()))
    if not fetch:
        return redirect(request, "/logos", tr(request, "Gespeichert."))
    if not c.logos_enabled or not ctx.vault.load().get("logo_dev_token"):
        return redirect(request, "/logos", tr(request, "Erst einschalten und einen logo.dev-Schlüssel eintragen."),
                        "warn")
    try:
        result = ctx.syncer.update_logos()
    except AlreadyRunning:
        return redirect(request, "/logos", tr(request, "Ein Abruf läuft gerade - bitte gleich noch einmal."), "warn")
    if result is None:
        return redirect(request, "/logos", tr(request, "Logos konnten nicht geholt werden - siehe Protokoll."),
                        "error")
    text = tr(request, "{set} Logos gesetzt, {none} ohne Logo bei logo.dev, {failed} fehlgeschlagen.",
              set=result.set, none=result.none, failed=result.failed)
    if result.message:
        text += f" {result.message}"
    return redirect(request, "/logos", text, "warn" if result.message or result.failed else "ok")


# ── appearance ─────────────────────────────────────────────────────────────
THEMES = {"system": N_("Wie das Gerät"), "light": N_("Hell"), "dark": N_("Dunkel")}
# Each language under its own name, in every language.
LANGUAGES = {"de": "Deutsch", "en": "English"}


@router.get("/appearance", response_class=HTMLResponse)
def appearance_page(request: Request, ctx: Ctx):
    return ctx.render(request, "appearance.html", cfg=ctx.cfg(), themes=THEMES, languages=LANGUAGES)


@router.post("/appearance")
def appearance_save(request: Request, ctx: Ctx, theme: str = Form("system"), language: str = Form("de")):
    if theme not in THEMES or language not in LANGUAGES:
        return redirect(request, "/appearance", tr(request, "Unbekannte Auswahl."), "error")
    c = ctx.cfg()
    c.ui_theme, c.ui_language = theme, language
    ctx.save(c)
    return redirect(request, "/appearance", tr(request, "Gespeichert."))  # already in the language just chosen
