"""The daily pages: overview (a traffic light per broker), "Jetzt abrufen", settings index, unknown bookings."""

from __future__ import annotations

import logging
import subprocess
import threading
from dataclasses import asdict
from datetime import datetime

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ... import config as config_mod
from ... import retired
from ...sync import AlreadyRunning, is_running
from .. import views
from ..context import AppContext, Ctx, redirect, tr

log = logging.getLogger(__name__)
router = APIRouter()

TIMER = "wealthfolio-broker-sync-run.timer"


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, ctx: Ctx):
    # A run whose process died stays at 'running' in the database; close it
    # unless a sync (here or the timer's) really is in progress.
    if not ctx.running.is_set():
        ctx.syncer.cleanup()
    busy = ctx.running.is_set() or is_running(ctx.data_dir)
    c = ctx.cfg()
    secrets_ = ctx.vault.load()
    brokers = _brokers(ctx, c, secrets_)
    # No setup steps any more (#83): only a hint while Wealthfolio isn't connected.
    return ctx.render(request, "dashboard.html", brokers=brokers,
                      wf_missing=not (c.wealthfolio_url and _wf_done(secrets_)), running=busy,
                      next_run=_next_timer_run(), runs=ctx.state.runs(limit=20),
                      retired_notices=retired.notices(ctx.state), safe_mode=c.safe_mode)


def _brokers(ctx: AppContext, c: config_mod.Config, secrets_: dict) -> list[dict]:
    brokers = []
    lang = c.ui_language
    for key, adapter in ctx.adapters.items():
        b = c.brokers.get(key, config_mod.BrokerConfig())
        last = ctx.state.last_run(key)
        reconcile = ctx.state.reconcile(key)
        unknown = len(ctx.state.unknown_events(key))
        gaps = sum(1 for g in ctx.state.gaps(key) if g["kind"] in ("missing", "partial", "orphan"))
        balances = ctx.state.balances(key)
        # The overview shows no amounts, only whether the broker's balance matches Wealthfolio (#83).
        cash_off = [d for d in (reconcile or {}).get("deviations", []) if d.get("kind") == "cash"]
        saldo = None if not balances or reconcile is None else ("off" if cash_off else "ok")
        light, reasons = views.broker_light(asdict(last) if last else None, reconcile, unknown, gaps, b.enabled,
                                            lang=lang)
        brokers.append({
            "key": key,
            "label": adapter.label,
            "savings_label": adapter.savings_label,
            "config": b,
            "has_credentials": bool(secrets_.get("brokers", {}).get(key, {}).get("credentials")),
            "last": last,
            "saldo": saldo,
            "reconcile": reconcile,
            "unknown": unknown,
            "gaps": gaps,
            "light": light,
            "reasons": reasons,
        })
    return brokers


@router.post("/retired/dismiss")
def retired_dismiss(request: Request, ctx: Ctx, broker: str = Form(...)):
    retired.dismiss(ctx.state, broker)
    return redirect(request, "/")


@router.post("/run")
def run_now(request: Request, ctx: Ctx, broker: str = Form("")):
    if ctx.running.is_set():
        return redirect(request, "/", tr(request, "Ein Abruf läuft bereits."), "warn")
    c = ctx.cfg()
    if broker:
        if broker not in ctx.adapters:
            return redirect(request, "/", tr(request, "Unbekannter Broker."), "error")
        if ctx.needs_login(broker, c):
            from .broker_login import start_login  # this POST starts the login itself (#99)

            return start_login(request, ctx, broker, True)
        return start_run(request, ctx, [broker])
    # "Alle abrufen": the brokers that need a login first are logged in one after the other (#85), then all
    # are fetched in one run.
    keys = [k for k, b in c.brokers.items()
            if k in ctx.adapters and b.enabled and ctx.vault.broker(k).get("credentials")]
    login = [k for k in keys if ctx.needs_login(k, c)]
    if login:
        request.session["run_queue"] = {"todo": login, "run": keys, "login": list(login)}
        from .broker_login import start_login

        return start_login(request, ctx, login[0], True)
    return start_run(request, ctx, None)


def start_run(request: Request, ctx: AppContext, only: list[str] | None) -> RedirectResponse:
    """Start a run from the UI (``manual``: the safe mode fetches only so), with the PINs typed at the logins."""
    with ctx.start_lock:  # check and set as one: two quick clicks start one run
        if ctx.running.is_set():
            return redirect(request, "/", tr(request, "Ein Abruf läuft bereits."), "warn")
        ctx.running.set()  # before the thread starts, so the next page view already shows it
    pins = {k: ctx.pins.pop(k) for k in list(ctx.pins) if only is None or k in only}

    def work():
        try:
            ctx.syncer.run(only, manual=True, pins=pins)
        except AlreadyRunning:
            log.info("sync skipped: already running (timer)")
        except Exception:
            log.exception("sync failed")
        finally:
            pins.clear()
            ctx.running.clear()

    if ctx.run_in_thread:
        threading.Thread(target=work, daemon=True).start()
        return redirect(request, "/", tr(request, "Abruf gestartet. Die Seite aktualisiert sich, bis er fertig ist."))
    work()
    return redirect(request, "/", tr(request, "Abruf beendet."))


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, ctx: Ctx):
    return ctx.render(request, "settings.html")


@router.get("/unknown", response_class=HTMLResponse)
def unknown_page(request: Request, ctx: Ctx):
    return ctx.render(request, "unknown.html", events=ctx.state.unknown_events())


def _wf_done(secrets_: dict) -> bool:
    return bool(secrets_.get("wealthfolio_checked"))


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
