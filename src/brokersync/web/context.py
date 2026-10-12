"""What every page of the UI shares (#76): the data, the syncer, the templates and the helpers around them.

``app._create_app`` builds one ``AppContext`` per built app and keeps it on ``app.state.ctx``; the routes
(``web/routes/``) get it through the dependency ``Ctx``.
"""

from __future__ import annotations

import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import __version__, redact
from .. import config as config_mod
from ..adapters import BrokerAdapter
from ..mapping import Accounts
from ..notify import Notifier
from ..state import State
from ..sync import Syncer
from ..vault import Vault
from ..wealthfolio import WealthfolioClient, WealthfolioError
from .gate import Gate, Throttle

# Pages anyone can open, without a login and without a CSRF token.
PUBLIC = {"/login", "/setup-password", "/healthz", "/ca.crt"}
STATUS_LABEL = {"ok": "OK", "needs_auth": "Anmeldung nötig", "error": "Fehler", "running": "läuft",
                "aborted": "abgebrochen"}

# The pages under *Einstellungen*: (path, title, what it is for). The header shows the daily pages only (#71).
SETTINGS_PAGES = [
    ("/setup/wealthfolio", "Wealthfolio", "Adresse und Passwort deines Wealthfolio"),
    ("/brokers", "Broker", "Zugangsdaten, Konten, Startdatum, Anmeldung"),
    ("/notifications", "Benachrichtigungen", "ntfy für TAN-Anfragen und Fehler"),
    ("/securities", "Wertpapiere", "ISIN unter eigenem Symbol führen"),
    ("/transfers", "Überträge", "Auszahlungen auf eigene Konten erkennen"),
    ("/logos", "Logos", "Logos der Wertpapiere von logo.dev"),
    ("/security", "Sicherheit", "Verschlüsselung, Master-Passphrase, Zertifikat"),
    ("/duplicates", "Duplikate", "Doppelte Buchungen finden und entfernen"),
]


@dataclass
class AppContext:
    data_dir: Path
    vault: Vault
    state: State
    adapters: dict[str, type[BrokerAdapter]]
    make_wf: Callable[[str, Any], WealthfolioClient]
    syncer: Syncer
    templates: Jinja2Templates
    notifier: Notifier | None
    run_in_thread: bool
    secure: bool
    gate: Gate
    throttle: Throttle
    # Adapters waiting for a TAN, between the two requests of a login.
    pending: dict[str, BrokerAdapter] = field(default_factory=dict)
    # Set while a sync started from the UI runs.
    running: threading.Event = field(default_factory=threading.Event)

    def render(self, request: Request, name: str, **ctx) -> HTMLResponse:
        if "csrf" not in request.session:
            request.session["csrf"] = secrets.token_urlsafe(16)
        flash = request.session.pop("flash", None)
        nav_unknown = self.state.unknown_count() if request.session.get("user") else 0
        return self.templates.TemplateResponse(request, name, {
            "csrf": request.session["csrf"], "flash": flash, "version": __version__, "adapters": self.adapters,
            "nav_unknown": nav_unknown, "settings_pages": SETTINGS_PAGES, **ctx,
        })

    def cfg(self) -> config_mod.Config:
        return config_mod.load(self.data_dir)

    def save(self, c: config_mod.Config) -> None:
        config_mod.save(self.data_dir, c)

    def wf_client(self, c: config_mod.Config) -> WealthfolioClient:
        return self.make_wf(c.wealthfolio_url, self.vault.load().get("wealthfolio_password"))

    def wf_accounts(self, c: config_mod.Config):
        try:
            with self.wf_client(c) as wf:
                return [a for a in wf.list_accounts() if a.is_active], None
        except WealthfolioError as e:
            return [], str(e)

    def finish_login(self, key: str, adapter: BrokerAdapter) -> None:
        adapter.close()
        self.vault.set_broker_session(key, adapter.session_state())


def get_ctx(request: Request) -> AppContext:
    return request.app.state.ctx


Ctx = Annotated[AppContext, Depends(get_ctx)]


def redirect(request: Request, url: str, flash: str | None = None, level: str = "ok") -> RedirectResponse:
    if flash:
        # Messages can carry a broker's error text: never a secret or an account number on screen.
        request.session["flash"] = {"text": redact.redact(flash), "level": level}
    return RedirectResponse(url, 303)


async def csrf_guard(request: Request) -> None:
    # Starlette caches the parsed form, so the endpoints can still read it.
    if request.method == "POST" and request.url.path not in PUBLIC:
        form = await request.form()
        if not request.session.get("csrf") or form.get("csrf") != request.session.get("csrf"):
            raise HTTPException(400, "Formular abgelaufen - bitte die Seite neu laden.")


def accounts_of(b: config_mod.BrokerConfig) -> Accounts:
    return Accounts(b.cash_account_id, b.portfolio_account_id, b.savings_account_id)
