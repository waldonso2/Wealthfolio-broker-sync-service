"""The page *Dienste* (#145), the start page: what the service writes into Wealthfolio, each switched on and off.

A service that still lacks something (Wealthfolio, a broker, a key) is not switched on: the user is sent to the
settings it needs, and it turns on by itself once they are saved (``settle``, called on every page view).
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ... import config as config_mod
from ... import meta_keys
from ..context import AppContext, Ctx, redirect, tr
from ..i18n import N_

router = APIRouter()

# (key, name, what it does in Wealthfolio, its settings)
SERVICES = [
    ("sync", N_("Broker-Sync"), N_("bucht die Umsätze deiner Broker und Banken in Wealthfolio"), "/brokers"),
    ("taxonomies", N_("Länder & Sektoren"),
     N_("trägt Länder und Sektoren der ETFs in Wealthfolio ein; deine eigenen Zuordnungen bleiben"),
     "/lookthrough?settings=1#xray-settings"),
    ("isin", N_("ISIN ergänzen"), N_("trägt bei Wertpapieren ohne ISIN die ISIN in Wealthfolio ein"), "/securities"),
    ("logos", N_("Logos"), N_("setzt die Logos der Wertpapiere von logo.dev in Wealthfolio"), "/logos"),
    ("spending", N_("Ausgaben-Tracker"),
     N_("nimmt die Verrechnungskonten deiner Banken in Wealthfolios Ausgaben-Auswertung auf"), "/brokers"),
    ("xray", N_("X-Ray in Wealthfolio"),
     N_("legt die Fondslisten für das X-Ray-Addon in Wealthfolio ab; Beträge bleiben in Wealthfolio"),
     "/lookthrough?settings=1#xray-settings"),
]
NAMES = {key: name for key, name, _, _ in SERVICES}


def missing(ctx: AppContext, key: str, c: config_mod.Config) -> tuple[str, str] | None:
    """What service ``key`` still needs before it can run: (message, page), else None."""
    secrets_ = ctx.vault.load()
    if not secrets_.get("wealthfolio_checked"):
        return N_("Erst Wealthfolio verbinden."), "/setup/wealthfolio"
    brokers = secrets_.get("brokers", {})
    if key == "sync" and not any(b.get("credentials") for b in brokers.values()):
        return N_("Erst einen Broker einrichten."), "/brokers"
    if key == "spending" and not any(b.spending and b.cash_account_id for b in c.brokers.values()):
        return N_("Erst bei einem Broker „Ausgaben auswerten“ einschalten."), "/brokers"
    if key == "logos" and not secrets_.get("logo_dev_token"):
        return N_("Erst einen logo.dev-Schlüssel eintragen."), "/logos"
    return None


def settle(request: Request, ctx: AppContext) -> None:
    """A service the user switched on while it lacked something: on now, if the settings were saved since."""
    key = request.session.get("enable_service")
    if key not in NAMES:
        return
    c = ctx.cfg()
    if missing(ctx, key, c):
        return
    request.session.pop("enable_service")
    ctx.update(lambda c: c.set_service(key, True))
    request.session["flash"] = {"text": tr(request, "{name} ist eingeschaltet.", name=tr(request, NAMES[key])),
                                "level": "ok"}


@router.get("/", response_class=HTMLResponse)
def services_page(request: Request, ctx: Ctx):
    c = ctx.cfg()
    services = [{"key": key, "name": name, "text": text, "href": href, "on": c.service(key)}
                for key, name, text, href in SERVICES]
    xray = json.loads(ctx.state.meta(meta_keys.XRAY_ADDON_STATUS()) or "{}")  # the last write for the addon
    return ctx.render(request, "services.html", services=services,
                      pending=request.session.get("enable_service"), xray=xray)


@router.post("/services/{key}")
def services_switch(request: Request, ctx: Ctx, key: str):
    if key not in NAMES:
        return redirect(request, "/", tr(request, "Unbekannter Dienst."), "error")
    c = ctx.cfg()
    name = tr(request, NAMES[key])
    if c.service(key):
        ctx.update(lambda c: c.set_service(key, False))
        if request.session.get("enable_service") == key:
            request.session.pop("enable_service")
        return redirect(request, "/", tr(request, "{name} ist ausgeschaltet.", name=name))
    lacking = missing(ctx, key, c)
    if lacking:
        request.session["enable_service"] = key
        return redirect(request, lacking[1], tr(request, "{what} Danach schaltet sich {name} ein.",
                                                what=tr(request, lacking[0]), name=name), "warn")
    ctx.update(lambda c: c.set_service(key, True))
    request.session.pop("enable_service", None)
    return redirect(request, "/", tr(request, "{name} ist eingeschaltet.", name=name))
