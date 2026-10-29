"""Broker login from the UI: start it, then a TAN, a code, a PIN or an app confirmation (``ctx.pending`` in
between). With ``run`` the fetch follows the login; "Alle abrufen" chains the logins (``run_queue``, #85)."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ... import redact
from ...adapters import AdapterError, AuthRequired, BrokerAdapter
from ...sync import AlreadyRunning, run_lock
from ..context import AppContext, Ctx, redirect, tr
from .overview import start_run

router = APIRouter()


def _page(request: Request, ctx: AppContext, key: str, challenge, run: bool,
          since: float | None = None) -> HTMLResponse:
    # Until when this login waits for the user (UX-05): shown, so an expired one is no surprise.
    left = PENDING_SECONDS - (time.monotonic() - since if since is not None else 0)
    until = datetime.now(UTC) + timedelta(seconds=max(left, 0))
    queue = request.session.get("run_queue") or {}
    position = None  # "Anmeldung 2 von 3" during "Alle abrufen"
    if run and key in queue.get("todo", []):
        total = len(queue["login"])
        position = (total - len(queue["todo"]) + 1, total)
    return ctx.render(request, "broker_login.html", key=key, adapter=ctx.adapters[key], challenge=challenge,
                      run=run, position=position, safe=ctx.cfg().safe_mode, until=until)


# An open login waits this long for its TAN or confirmation; then it is closed (and a typed PIN dropped).
PENDING_SECONDS = 15 * 60


@router.get("/brokers/{key}/login", response_class=HTMLResponse)
def broker_login_page(request: Request, ctx: Ctx, key: str):
    """Shows an open login again, or offers to start one - a GET never contacts the broker (#99): reloading,
    going back or a browser's prefetch must not ask for another TAN (comdirect locks after five)."""
    if key not in ctx.adapters:
        return redirect(request, "/brokers", tr(request, "Unbekannter Broker."), "error")
    run = request.query_params.get("run") == "1" or ctx.cfg().safe_mode
    adapter = _open_login(ctx, key)
    if adapter is not None:
        return _page(request, ctx, key, adapter.ui_challenge, run, adapter.ui_since)
    return ctx.render(request, "broker_login_start.html", key=key, adapter=ctx.adapters[key], run=run)


@router.post("/brokers/{key}/login/start")
def broker_login_start(request: Request, ctx: Ctx, key: str, run: str = Form("")):
    if key not in ctx.adapters:
        return redirect(request, "/brokers", tr(request, "Unbekannter Broker."), "error")
    return start_login(request, ctx, key, run == "1" or ctx.cfg().safe_mode)


def start_login(request: Request, ctx: AppContext, key: str, run: bool):
    """Log in at the broker - only from a POST (the button, *Jetzt abrufen*, the next of *Alle abrufen*)."""
    with ctx.login_step(key) as free:
        if not free:
            return redirect(request, "/broker", tr(request, "Die Anmeldung läuft gerade schon - bitte kurz warten."),
                            "warn")
        return _start_login(request, ctx, key, run)


def _start_login(request: Request, ctx: AppContext, key: str, run: bool):
    if ctx.adapters[key].consent and not ctx.cfg().broker(key).consented:  # #112: never without the user's yes
        return redirect(request, f"/brokers/{key}", tr(request, "Bitte bestätige, dass du den Hinweis zur "
                                                               "Schnittstelle gelesen hast."), "error")
    old = ctx.pending.pop(key, None)
    if old is not None:
        ctx.finish_login(key, old)  # its counters (wrong TANs) are stored before the new login reads them
    stored = ctx.vault.broker(key)
    adapter = ctx.adapters[key](stored.get("credentials", {}), stored.get("session"))
    try:
        # Under the sync lock: a run (the timer) uses the stored session, the two must not race for the
        # lock-out counters. Only the call to the broker - the run that may follow takes the lock itself.
        with run_lock(ctx.data_dir):
            try:
                adapter.login()
                outcome: AuthRequired | AdapterError | None = None
            except (AuthRequired, AdapterError) as e:
                outcome = e
            ctx.vault.store_session(key, adapter)
    except AlreadyRunning:
        return redirect(request, "/broker", tr(request, "Gerade läuft ein Abruf - bitte danach anmelden."), "warn")
    if isinstance(outcome, AuthRequired):
        adapter.ui_challenge, adapter.ui_since = outcome.challenge, time.monotonic()
        ctx.pending[key] = adapter
        return _page(request, ctx, key, outcome.challenge, run, adapter.ui_since)
    if isinstance(outcome, AdapterError):
        ctx.finish_login(key, adapter)  # closed, then stored: e.g. a rejected PIN, so nothing retries it
        return _failed(request, ctx, key, outcome, run)
    return _logged_in(request, ctx, key, adapter, run, confirmed=False)


def _open_login(ctx: AppContext, key: str):
    """The login waiting for the user, unless it waited too long (then it is closed)."""
    adapter = ctx.pending.get(key)
    if adapter is not None and time.monotonic() - adapter.ui_since > PENDING_SECONDS:
        ctx.pending.pop(key, None)
        ctx.pins.pop(key, None)
        ctx.finish_login(key, adapter)
        return None
    return adapter


@router.post("/brokers/{key}/login")
def broker_login_finish(request: Request, ctx: Ctx, key: str, code: str = Form(""), run: str = Form("")):
    with ctx.login_step(key) as free:
        if not free:
            return redirect(request, "/broker", tr(request, "Die Anmeldung läuft gerade schon - bitte kurz warten."),
                            "warn")
        return _finish_login(request, ctx, key, code, run == "1")


def _finish_login(request: Request, ctx: AppContext, key: str, code: str, want_run: bool):
    adapter = _open_login(ctx, key)
    if adapter is None:
        return redirect(request, f"/brokers/{key}/login{'?run=1' if want_run else ''}",
                        tr(request, "Die Anmeldung ist abgelaufen, bitte neu starten."), "warn")
    try:
        adapter.complete_login(code)
    except AuthRequired as e:
        adapter.ui_challenge = e.challenge
        ctx.vault.store_session(key, adapter)  # a wrong TAN is counted at once, not only at the end
        return _page(request, ctx, key, e.challenge, want_run, adapter.ui_since)
    except AdapterError as e:
        ctx.pending.pop(key, None)
        ctx.finish_login(key, adapter)
        return _failed(request, ctx, key, e, want_run)
    ctx.pending.pop(key, None)
    return _logged_in(request, ctx, key, adapter, want_run, confirmed=True)


@router.post("/brokers/{key}/login/skip")
def broker_login_skip(request: Request, ctx: Ctx, key: str):
    """"Alle abrufen": leave this broker out and go on with the next login."""
    with ctx.login_step(key) as free:
        if not free:
            return redirect(request, "/broker", tr(request, "Die Anmeldung läuft gerade schon - bitte kurz warten."),
                            "warn")
        return _skip_login(request, ctx, key)


def _skip_login(request: Request, ctx: AppContext, key: str):
    old = ctx.pending.pop(key, None)
    if old is not None:
        ctx.finish_login(key, old)
    ctx.pins.pop(key, None)
    label = ctx.adapters[key].label if key in ctx.adapters else key
    return _next(request, ctx, key, dropped=True, note=tr(request, "{label} übersprungen.", label=label))


def _logged_in(request: Request, ctx: AppContext, key: str, adapter: BrokerAdapter, run: bool,
               confirmed: bool) -> RedirectResponse:
    ctx.finish_login(key, adapter)
    label = ctx.adapters[key].label
    if not run:
        if confirmed:
            return redirect(request, "/broker",
                            tr(request, "{label}: angemeldet. Du kannst jetzt abrufen.", label=label))
        return redirect(request, "/broker",
                        tr(request, "{label}: angemeldet - die gespeicherte Sitzung ist noch gültig, "
                                    "eine Bestätigung war nicht nötig. Du kannst jetzt abrufen.", label=label))
    # A PIN typed at this login (not stored) is handed to the run in memory only.
    stored = ctx.vault.broker(key).get("credentials", {})
    for f in ctx.adapters[key].credential_fields:
        if f.ask and not stored.get(f.name) and adapter.credentials.get(f.name):
            ctx.pins[key] = adapter.credentials[f.name]
    return _next(request, ctx, key)


def _failed(request: Request, ctx: AppContext, key: str, e: AdapterError, run: bool) -> RedirectResponse:
    label = ctx.adapters[key].label
    queue = request.session.get("run_queue") or {}
    if run and key in queue.get("todo", []):
        note = tr(request, "{label}: Anmeldung fehlgeschlagen: {error}", label=label, error=e)
        return _next(request, ctx, key, dropped=True, note=note, level="error")
    return redirect(request, f"/brokers/{key}", tr(request, "Anmeldung fehlgeschlagen: {error}", error=e), "error")


def _next(request: Request, ctx: AppContext, key: str, dropped: bool = False, note: str = "",
          level: str = "warn") -> RedirectResponse:
    """After a broker's login: the next login of "Alle abrufen", or the run once all are done."""
    queue = request.session.get("run_queue")
    if queue and key in queue["todo"]:
        queue["todo"].remove(key)
        if dropped and key in queue["run"]:
            queue["run"].remove(key)
        request.session["run_queue"] = queue
        if queue["todo"]:
            if note:  # shown on the next broker's login page
                request.session["flash"] = {"text": redact.redact(note), "level": level}
            return start_login(request, ctx, queue["todo"][0], True)
        request.session.pop("run_queue", None)
        if not queue["run"]:
            return redirect(request, "/broker", note or tr(request, "Nichts abzurufen."), level)
        return start_run(request, ctx, queue["run"])
    if dropped:
        return redirect(request, "/broker", note or tr(request, "Übersprungen."), level)
    return start_run(request, ctx, [key])
