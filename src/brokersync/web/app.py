"""Web UI: setup assistant, status, "Jetzt abrufen", broker login with TAN.

Everything a user configures happens here; nobody edits files. The UI is
protected by its own password, chosen on the first visit. Texts are German,
the audience are users of German brokers.
"""

from __future__ import annotations

import logging
import os
import secrets
import subprocess
import threading
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .. import __version__
from .. import config as config_mod
from ..adapters import ADAPTERS, AdapterError, AuthRequired, BrokerAdapter
from ..notify import Notifier
from ..state import State
from ..sync import UNMATCHED_SECURITIES, AlreadyRunning, Syncer, is_running
from ..vault import Vault, hash_password, verify_password
from ..wealthfolio import WealthfolioClient, WealthfolioError

log = logging.getLogger(__name__)
HERE = Path(__file__).parent
TIMER = "wealthfolio-broker-sync-run.timer"
PUBLIC = {"/login", "/setup-password", "/healthz"}
# Times are shown in German time; the container usually runs in UTC.
TZ = ZoneInfo(os.environ.get("BROKERSYNC_TZ", "Europe/Berlin"))
STATUS_LABEL = {"ok": "OK", "needs_auth": "Anmeldung nötig", "error": "Fehler", "running": "läuft",
                "aborted": "abgebrochen"}


def create_app(data_dir: Path, *, wealthfolio=None, adapters: dict[str, type[BrokerAdapter]] | None = None,
               notifier: Notifier | None = None, run_in_thread: bool = True) -> FastAPI:
    data_dir = Path(data_dir)
    vault = Vault(data_dir)
    state = State(data_dir)
    adapters = adapters or ADAPTERS
    make_wf = wealthfolio or (lambda url, pw: WealthfolioClient(url, pw))
    syncer = Syncer(data_dir, wealthfolio=make_wf, notifier=notifier, adapters=adapters)
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.filters["local"] = _local_time
    templates.env.filters["money"] = _money
    templates.env.globals["STATUS"] = STATUS_LABEL
    templates.env.globals["UNMATCHED_SECURITIES"] = UNMATCHED_SECURITIES
    # Adapters waiting for a TAN, between the two requests of a login.
    pending: dict[str, BrokerAdapter] = {}
    running = threading.Event()

    session_key = vault.load().get("ui_session_key")
    if not session_key:
        session_key = secrets.token_urlsafe(32)
        vault.update(lambda d: d.__setitem__("ui_session_key", session_key))

    async def csrf_guard(request: Request) -> None:
        # Starlette caches the parsed form, so the endpoints can still read it.
        if request.method == "POST" and request.url.path not in PUBLIC:
            form = await request.form()
            if not request.session.get("csrf") or form.get("csrf") != request.session.get("csrf"):
                raise HTTPException(400, "Formular abgelaufen - bitte die Seite neu laden.")

    app = FastAPI(title="Wealthfolio Broker Sync", docs_url=None, redoc_url=None, openapi_url=None,
                  dependencies=[Depends(csrf_guard)])

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
    app.add_middleware(SessionMiddleware, secret_key=session_key, same_site="strict", https_only=False,
                       max_age=7 * 24 * 3600)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    def render(request: Request, name: str, **ctx) -> HTMLResponse:
        if "csrf" not in request.session:
            request.session["csrf"] = secrets.token_urlsafe(16)
        flash = request.session.pop("flash", None)
        return templates.TemplateResponse(request, name, {
            "csrf": request.session["csrf"], "flash": flash, "version": __version__, "adapters": adapters, **ctx,
        })

    def redirect(request: Request, url: str, flash: str | None = None, level: str = "ok") -> RedirectResponse:
        if flash:
            request.session["flash"] = {"text": flash, "level": level}
        return RedirectResponse(url, 303)

    def cfg() -> config_mod.Config:
        return config_mod.load(data_dir)

    def wf_client(c: config_mod.Config) -> WealthfolioClient:
        return make_wf(c.wealthfolio_url, vault.load().get("wealthfolio_password"))

    def wf_accounts(c: config_mod.Config):
        try:
            with wf_client(c) as wf:
                return [a for a in wf.list_accounts() if a.is_active], None
        except WealthfolioError as e:
            return [], str(e)

    # ── first visit / login ─────────────────────────────────────────────────
    @app.get("/healthz")
    def healthz():
        return {"ok": True, "version": __version__}

    @app.get("/setup-password", response_class=HTMLResponse)
    def setup_password_page(request: Request):
        if vault.load().get("ui_password_hash"):
            return RedirectResponse("/login", 303)
        return render(request, "setup_password.html")

    @app.post("/setup-password")
    def setup_password(request: Request, password: str = Form(...), password2: str = Form(...)):
        if vault.load().get("ui_password_hash"):
            return RedirectResponse("/login", 303)
        if len(password) < 8 or password != password2:
            return redirect(request, "/setup-password",
                            "Das Passwort braucht mindestens 8 Zeichen, beide Eingaben müssen gleich sein.", "error")
        vault.update(lambda d: d.__setitem__("ui_password_hash", hash_password(password)))
        request.session["user"] = "admin"
        if not cfg().public_url:
            c = cfg()
            c.public_url = str(request.base_url).rstrip("/")
            config_mod.save(data_dir, c)
        return redirect(request, "/", "Passwort gespeichert. Jetzt Schritt für Schritt einrichten.")

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request):
        return render(request, "login.html")

    @app.post("/login")
    def login(request: Request, password: str = Form(...)):
        if verify_password(password, vault.load().get("ui_password_hash")):
            request.session["user"] = "admin"
            return redirect(request, "/")
        return redirect(request, "/login", "Falsches Passwort.", "error")

    @app.post("/logout")
    def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/login", 303)

    # ── dashboard ───────────────────────────────────────────────────────────
    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        # A run whose process died stays at 'running' in the database; close it
        # unless a sync (here or the timer's) really is in progress.
        if not running.is_set():
            syncer.cleanup()
        busy = running.is_set() or is_running(data_dir)
        c = cfg()
        secrets_ = vault.load()
        brokers = []
        for key, adapter in adapters.items():
            b = c.brokers.get(key, config_mod.BrokerConfig())
            brokers.append({
                "key": key,
                "label": adapter.label,
                "config": b,
                "has_credentials": bool(secrets_.get("brokers", {}).get(key, {}).get("credentials")),
                "last": state.last_run(key),
                "balances": state.balances(key),
            })
        steps = [
            ("Wealthfolio verbinden", "/setup/wealthfolio", bool(c.wealthfolio_url and _wf_done(secrets_, c))),
            ("Broker einrichten und Konten zuordnen", "/brokers", any(b["config"].enabled for b in brokers)),
            ("Benachrichtigungen (ntfy)", "/notifications", bool(c.ntfy_topic)),
            ("Erster Abruf", "#run", any(b["last"] for b in brokers)),
        ]
        return render(request, "dashboard.html", brokers=brokers, steps=steps, all_done=all(s[2] for s in steps),
                      running=busy,
                      next_run=_next_timer_run(), unknown=len(state.unknown_events()), runs=state.runs(limit=10))

    @app.post("/run")
    def run_now(request: Request, broker: str = Form("")):
        if running.is_set():
            return redirect(request, "/", "Ein Abruf läuft bereits.", "warn")
        only = [broker] if broker else None

        def work():
            try:
                syncer.run(only)
            except AlreadyRunning:
                log.info("sync skipped: already running (timer)")
            except Exception:
                log.exception("sync failed")
            finally:
                running.clear()

        running.set()  # before the thread starts, so the next page view already shows it
        if run_in_thread:
            threading.Thread(target=work, daemon=True).start()
            return redirect(request, "/", "Abruf gestartet. Die Seite aktualisiert sich, bis er fertig ist.")
        work()
        return redirect(request, "/", "Abruf beendet.")

    # ── Wealthfolio ─────────────────────────────────────────────────────────
    @app.get("/setup/wealthfolio", response_class=HTMLResponse)
    def wealthfolio_page(request: Request):
        c = cfg()
        has_pw = bool(vault.load().get("wealthfolio_password"))
        return render(request, "wealthfolio.html", cfg=c, has_password=has_pw)

    @app.post("/setup/wealthfolio")
    def wealthfolio_save(request: Request, url: str = Form(...), password: str = Form(""),
                         no_password: str = Form("")):
        c = cfg()
        c.wealthfolio_url = url.strip().rstrip("/")
        config_mod.save(data_dir, c)
        if no_password:
            vault.update(lambda d: d.pop("wealthfolio_password", None))
        elif password:
            vault.update(lambda d: d.__setitem__("wealthfolio_password", password))
        vault.update(lambda d: d.__setitem__("wealthfolio_checked", False))
        accounts, error = wf_accounts(c)
        if error:
            return redirect(request, "/setup/wealthfolio", f"Verbindung fehlgeschlagen: {error}", "error")
        vault.update(lambda d: d.__setitem__("wealthfolio_checked", True))
        return redirect(request, "/brokers", f"Verbunden - {len(accounts)} Konten in Wealthfolio gefunden.")

    # ── brokers ─────────────────────────────────────────────────────────────
    @app.get("/brokers", response_class=HTMLResponse)
    def brokers_page(request: Request):
        c = cfg()
        return render(request, "brokers.html", cfg=c)

    @app.get("/brokers/{key}", response_class=HTMLResponse)
    def broker_page(request: Request, key: str):
        if key not in adapters:
            return redirect(request, "/brokers", "Unbekannter Broker.", "error")
        c = cfg()
        accounts, error = wf_accounts(c)
        stored = vault.broker(key).get("credentials", {})
        return render(request, "broker.html", key=key, adapter=adapters[key], b=c.broker(key), accounts=accounts,
                      wf_error=error, stored={k: bool(v) for k, v in stored.items()},
                      plain={f.name: stored.get(f.name, "") for f in adapters[key].credential_fields if not f.secret})

    @app.post("/brokers/{key}")
    async def broker_save(request: Request, key: str):
        if key not in adapters:
            return redirect(request, "/brokers", "Unbekannter Broker.", "error")
        form = await request.form()
        c = cfg()
        b = c.broker(key)
        b.enabled = form.get("enabled") == "on"
        b.cash_account_id = str(form.get("cash_account_id", ""))
        b.portfolio_account_id = str(form.get("portfolio_account_id", ""))
        b.start_date = str(form.get("start_date", ""))
        if b.enabled and (not b.cash_account_id or not b.portfolio_account_id):
            return redirect(request, f"/brokers/{key}", "Bitte ein Verrechnungs- und ein Depotkonto wählen.", "error")
        if b.enabled and b.cash_account_id == b.portfolio_account_id:
            return redirect(request, f"/brokers/{key}",
                            "Verrechnungs- und Depotkonto müssen verschiedene Konten sein.", "error")
        config_mod.save(data_dir, c)
        old = vault.broker(key).get("credentials", {})
        creds = {}
        for f in adapters[key].credential_fields:
            v = str(form.get(f"cred_{f.name}", ""))
            creds[f.name] = v if v or not f.secret else old.get(f.name, "")
        if creds != old:
            vault.set_broker_credentials(key, creds)
            return redirect(request, f"/brokers/{key}/login", "Gespeichert. Jetzt beim Broker anmelden.")
        return redirect(request, "/", "Gespeichert.")

    @app.get("/brokers/{key}/login", response_class=HTMLResponse)
    def broker_login_start(request: Request, key: str):
        if key not in adapters:
            return redirect(request, "/brokers", "Unbekannter Broker.", "error")
        old = pending.pop(key, None)
        if old is not None:
            old.close()
        stored = vault.broker(key)
        adapter = adapters[key](stored.get("credentials", {}), stored.get("session"))
        try:
            adapter.login()
        except AuthRequired as e:
            pending[key] = adapter
            vault.set_broker_session(key, adapter.session_state())
            return render(request, "broker_login.html", key=key, adapter=adapters[key], challenge=e.challenge)
        except AdapterError as e:
            finish_login(key, adapter)  # keeps e.g. a rejected PIN, so nothing retries it
            return redirect(request, f"/brokers/{key}", f"Anmeldung fehlgeschlagen: {e}", "error")
        finish_login(key, adapter)
        return redirect(request, "/", f"{adapters[key].label}: angemeldet.")

    def finish_login(key: str, adapter: BrokerAdapter) -> None:
        adapter.close()
        vault.set_broker_session(key, adapter.session_state())

    @app.post("/brokers/{key}/login")
    def broker_login_finish(request: Request, key: str, code: str = Form("")):
        adapter = pending.get(key)
        if adapter is None:
            return redirect(request, f"/brokers/{key}/login", "Die Anmeldung ist abgelaufen, bitte neu starten.",
                            "warn")
        try:
            adapter.complete_login(code)
        except AuthRequired as e:
            return render(request, "broker_login.html", key=key, adapter=adapters[key], challenge=e.challenge)
        except AdapterError as e:
            pending.pop(key, None)
            finish_login(key, adapter)
            return redirect(request, f"/brokers/{key}", f"Anmeldung fehlgeschlagen: {e}", "error")
        pending.pop(key, None)
        finish_login(key, adapter)
        return redirect(request, "/", f"{adapters[key].label}: angemeldet. Du kannst jetzt abrufen.")

    # ── transfer patterns ───────────────────────────────────────────────────
    @app.get("/transfers", response_class=HTMLResponse)
    def transfers_page(request: Request):
        c = cfg()
        accounts, error = wf_accounts(c)
        names = {a.id: f"{a.name} ({a.currency})" for a in accounts}
        return render(request, "transfers.html", patterns=c.transfer_patterns, accounts=accounts, names=names,
                      wf_error=error)

    @app.post("/transfers")
    def transfers_save(request: Request, label: str = Form(""), iban: str = Form(""), keyword: str = Form(""),
                       destination: str = Form(""), delete: str = Form("")):
        c = cfg()
        if delete:
            i = int(delete)
            if 0 <= i < len(c.transfer_patterns):
                c.transfer_patterns.pop(i)
        else:
            iban = iban.replace(" ", "").upper()
            if not label.strip() or not (iban or keyword.strip()):
                return redirect(request, "/transfers", "Bitte einen Namen und eine IBAN oder ein Stichwort angeben.",
                                "error")
            c.transfer_patterns.append({"label": label.strip(), "iban": iban, "keyword": keyword.strip(),
                                        "destinationAccountId": destination})
        config_mod.save(data_dir, c)
        return redirect(request, "/transfers", "Gespeichert.")

    # ── notifications ───────────────────────────────────────────────────────
    @app.get("/notifications", response_class=HTMLResponse)
    def notifications_page(request: Request):
        c = cfg()
        if not c.ntfy_topic:
            c.ntfy_topic = f"wealthfolio-sync-{secrets.token_hex(6)}"
        return render(request, "notifications.html", cfg=c, has_token=bool(vault.load().get("ntfy_token")))

    @app.post("/notifications")
    def notifications_save(request: Request, server: str = Form(...), topic: str = Form(""), token: str = Form(""),
                           public_url: str = Form(""), test: str = Form("")):
        c = cfg()
        c.ntfy_server = server.strip().rstrip("/")
        c.ntfy_topic = topic.strip()
        c.public_url = public_url.strip().rstrip("/")
        config_mod.save(data_dir, c)
        if token:
            vault.update(lambda d: d.__setitem__("ntfy_token", token))
        if test:
            n = notifier or Notifier(c.ntfy_server, c.ntfy_topic, vault.load().get("ntfy_token"))
            ok = n.send("Wealthfolio Broker Sync", "Testnachricht - Benachrichtigungen funktionieren.",
                        link=c.public_url or None, tags="white_check_mark")
            if not ok:
                return redirect(request, "/notifications", "Testnachricht konnte nicht gesendet werden.", "error")
            return redirect(request, "/notifications", "Testnachricht gesendet.")
        return redirect(request, "/", "Benachrichtigungen gespeichert.")

    # ── unknown events, securities ──────────────────────────────────────────
    @app.get("/unknown", response_class=HTMLResponse)
    def unknown_page(request: Request):
        return render(request, "unknown.html", events=state.unknown_events())

    @app.get("/securities", response_class=HTMLResponse)
    def securities_page(request: Request):
        return render(request, "securities.html", mappings=cfg().security_mappings)

    @app.post("/securities")
    def securities_save(request: Request, isin: str = Form(...), symbol: str = Form(""), exchange_mic: str = Form(""),
                        delete: str = Form("")):
        c = cfg()
        isin = isin.strip().upper()
        if delete:
            c.security_mappings.pop(isin, None)
        elif isin and symbol.strip():
            c.security_mappings[isin] = {"symbol": symbol.strip(), "exchangeMic": exchange_mic.strip() or None}
        config_mod.save(data_dir, c)
        return redirect(request, "/securities", "Gespeichert.")

    return app


def _wf_done(secrets_: dict, c: config_mod.Config) -> bool:
    return bool(secrets_.get("wealthfolio_checked"))


def _money(value) -> str:
    """German number format: 1.234,56."""
    return f"{Decimal(str(value)):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _local_time(iso: str | None) -> str:
    if not iso:
        return ""
    return datetime.fromisoformat(iso).astimezone(TZ).strftime("%d.%m.%Y %H:%M")


def _next_timer_run() -> str | None:
    """Next run of the systemd timer, if it is installed."""
    try:
        out = subprocess.run(["systemctl", "show", TIMER, "-p", "NextElapseUSecRealtime", "--value"],
                             capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    if not out or out == "n/a":
        return None
    # "Fri 2026-10-09 06:12:00 CEST" - already local time of the host.
    parts = out.split()
    try:
        return datetime.strptime(f"{parts[1]} {parts[2]}", "%Y-%m-%d %H:%M:%S").strftime("%d.%m.%Y %H:%M")
    except (IndexError, ValueError):
        return out
