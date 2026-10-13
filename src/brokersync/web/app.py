"""Web UI: setup assistant, status, "Jetzt abrufen", broker login with TAN.

Everything a user configures happens here; nobody edits files. The UI is
protected by its own password, chosen on the first visit. Texts are German,
the audience are users of German brokers.

This module builds the app: templates, session, login check and the routers
of ``web/routes/`` (one per area, #76), which share an ``AppContext``
(``web/context.py``).
"""

from __future__ import annotations

import secrets
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .. import crypto, redact
from ..adapters import ADAPTERS, BrokerAdapter
from ..notify import Notifier
from ..state import State
from ..sync import UNMATCHED_SECURITIES, Syncer
from ..vault import Vault
from ..wealthfolio import WealthfolioClient
from . import views
from .context import PUBLIC, SETTINGS_PAGES, STATUS_LABEL, AppContext, accounts_of, csrf_guard
from .gate import Gate, Throttle
from .routes import ROUTERS

__all__ = ["PUBLIC", "SETTINGS_PAGES", "STATUS_LABEL", "accounts_of", "create_app"]

HERE = Path(__file__).parent


def create_app(data_dir: Path, *, wealthfolio=None, adapters: dict[str, type[BrokerAdapter]] | None = None,
               notifier: Notifier | None = None, run_in_thread: bool = True, secure: bool = False,
               logo_source=None) -> Gate:
    """The web UI behind its gate: security headers, and the unlock page while a master passphrase is set
    and not entered (the app itself is built once the data can be read)."""
    data_dir = Path(data_dir)
    throttle = Throttle()
    gate = Gate(data_dir, secure=secure, throttle=throttle, templates_dir=HERE / "templates",
                static_dir=HERE / "static")
    gate.build = lambda: _create_app(data_dir, wealthfolio=wealthfolio, adapters=adapters, notifier=notifier,
                                     run_in_thread=run_in_thread, secure=secure, gate=gate, throttle=throttle,
                                     logo_source=logo_source)
    if not crypto.is_locked(data_dir):
        gate.app()  # build it now: start-up work (retired brokers) and errors show at start, not on a first visit
    return gate


def _create_app(data_dir: Path, *, wealthfolio, adapters, notifier, run_in_thread: bool, secure: bool, gate: Gate,
                throttle: Throttle, logo_source=None) -> FastAPI:
    vault = Vault(data_dir)
    adapters = adapters or ADAPTERS
    make_wf = wealthfolio or (lambda url, pw: WealthfolioClient(url, pw))
    ctx = AppContext(data_dir=data_dir, vault=vault, state=State(data_dir), adapters=adapters, make_wf=make_wf,
                     syncer=Syncer(data_dir, wealthfolio=make_wf, notifier=notifier, adapters=adapters,
                                   logo_source=logo_source),
                     templates=_templates(), notifier=notifier, run_in_thread=run_in_thread, secure=secure,
                     gate=gate, throttle=throttle)

    session_key = vault.load().get("ui_session_key")
    if not session_key:
        session_key = secrets.token_urlsafe(32)
        vault.update(lambda d: d.__setitem__("ui_session_key", session_key))

    app = FastAPI(title="Wealthfolio Broker Sync", docs_url=None, redoc_url=None, openapi_url=None,
                  dependencies=[Depends(csrf_guard)])
    app.state.ctx = ctx

    @app.middleware("http")
    async def require_login(request: Request, call_next):
        path = request.url.path
        if path in PUBLIC or path.startswith("/static/"):
            return await call_next(request)
        if not vault.load().get("ui_password_hash"):
            return RedirectResponse("/setup-password", 303)
        if not request.session.get("user"):
            return RedirectResponse("/login", 303)
        return await call_next(request)

    # Added after the middleware above so it runs first and fills request.session.
    app.add_middleware(SessionMiddleware, secret_key=session_key, same_site="strict", https_only=secure,
                       max_age=24 * 3600)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    for router in ROUTERS:
        app.include_router(router)
    return app


def _templates() -> Jinja2Templates:
    templates = Jinja2Templates(directory=HERE / "templates")
    # One format for every date and time, German time (#73).
    templates.env.filters["local"] = views.local_time
    templates.env.filters["day"] = views.local_day
    templates.env.filters["utc"] = views.utc_iso
    templates.env.filters["money"] = views.money
    templates.env.globals["describe_unknown"] = views.describe_unknown
    templates.env.globals["report_url"] = views.report_url
    # Foreign error texts (banks, brokers, Wealthfolio) on a page: never a secret or an account number.
    templates.env.filters["redact"] = lambda text: redact.redact(str(text)) if text else text
    templates.env.globals["STATUS"] = STATUS_LABEL
    templates.env.globals["UNMATCHED_SECURITIES"] = UNMATCHED_SECURITIES
    return templates
