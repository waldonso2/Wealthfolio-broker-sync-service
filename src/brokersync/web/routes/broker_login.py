"""Broker login from the UI: start it, then a TAN, a code, a PIN or an app confirmation (``ctx.pending`` in
between). With ``run`` the fetch follows the login; "Alle abrufen" chains the logins (``run_queue``, #85)."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ...adapters import AdapterError, AuthRequired, BrokerAdapter
from ..context import AppContext, Ctx, redirect
from .overview import start_run

router = APIRouter()


def _page(request: Request, ctx: AppContext, key: str, challenge, run: bool) -> HTMLResponse:
    queue = request.session.get("run_queue") or {}
    position = None  # "Anmeldung 2 von 3" during "Alle abrufen"
    if run and key in queue.get("todo", []):
        total = len(queue["login"])
        position = (total - len(queue["todo"]) + 1, total)
    return ctx.render(request, "broker_login.html", key=key, adapter=ctx.adapters[key], challenge=challenge,
                      run=run, position=position, safe=ctx.cfg().safe_mode)


@router.get("/brokers/{key}/login", response_class=HTMLResponse)
def broker_login_start(request: Request, ctx: Ctx, key: str):
    adapters = ctx.adapters
    if key not in adapters:
        return redirect(request, "/brokers", "Unbekannter Broker.", "error")
    # In the safe mode a login is only good for the fetch that follows it.
    run = request.query_params.get("run") == "1" or ctx.cfg().safe_mode
    old = ctx.pending.pop(key, None)
    if old is not None:
        old.close()
    stored = ctx.vault.broker(key)
    adapter = adapters[key](stored.get("credentials", {}), stored.get("session"))
    try:
        adapter.login()
    except AuthRequired as e:
        ctx.pending[key] = adapter
        ctx.vault.set_broker_session(key, adapter.session_state())
        return _page(request, ctx, key, e.challenge, run)
    except AdapterError as e:
        ctx.finish_login(key, adapter)  # keeps e.g. a rejected PIN, so nothing retries it
        return _failed(request, ctx, key, e, run)
    return _logged_in(request, ctx, key, adapter, run, confirmed=False)


@router.post("/brokers/{key}/login")
def broker_login_finish(request: Request, ctx: Ctx, key: str, code: str = Form(""), run: str = Form("")):
    want_run = run == "1"
    adapter = ctx.pending.get(key)
    if adapter is None:
        return redirect(request, f"/brokers/{key}/login{'?run=1' if want_run else ''}",
                        "Die Anmeldung ist abgelaufen, bitte neu starten.", "warn")
    try:
        adapter.complete_login(code)
    except AuthRequired as e:
        return _page(request, ctx, key, e.challenge, want_run)
    except AdapterError as e:
        ctx.pending.pop(key, None)
        ctx.finish_login(key, adapter)
        return _failed(request, ctx, key, e, want_run)
    ctx.pending.pop(key, None)
    return _logged_in(request, ctx, key, adapter, want_run, confirmed=True)


@router.post("/brokers/{key}/login/skip")
def broker_login_skip(request: Request, ctx: Ctx, key: str):
    """"Alle abrufen": leave this broker out and go on with the next login."""
    old = ctx.pending.pop(key, None)
    if old is not None:
        ctx.finish_login(key, old)
    ctx.pins.pop(key, None)
    label = ctx.adapters[key].label if key in ctx.adapters else key
    return _next(request, ctx, key, dropped=True, note=f"{label} übersprungen.")


def _logged_in(request: Request, ctx: AppContext, key: str, adapter: BrokerAdapter, run: bool,
               confirmed: bool) -> RedirectResponse:
    ctx.finish_login(key, adapter)
    label = ctx.adapters[key].label
    if not run:
        if confirmed:
            return redirect(request, "/", f"{label}: angemeldet. Du kannst jetzt abrufen.")
        return redirect(request, "/", f"{label}: angemeldet - die gespeicherte Sitzung ist noch gültig, eine "
                                      "Bestätigung war nicht nötig. Du kannst jetzt abrufen.")
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
        return _next(request, ctx, key, dropped=True, note=f"{label}: Anmeldung fehlgeschlagen: {e}", level="error")
    return redirect(request, f"/brokers/{key}", f"Anmeldung fehlgeschlagen: {e}", "error")


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
            return redirect(request, f"/brokers/{queue['todo'][0]}/login?run=1", note or None, level)
        request.session.pop("run_queue", None)
        if not queue["run"]:
            return redirect(request, "/", note or "Nichts abzurufen.", level)
        return start_run(request, ctx, queue["run"])
    if dropped:
        return redirect(request, "/", note or "Übersprungen.", level)
    return start_run(request, ctx, [key])
