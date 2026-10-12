"""Broker login from the UI: start it, then a TAN, a code or an app confirmation (``ctx.pending`` in between)."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from ...adapters import AdapterError, AuthRequired
from ..context import Ctx, redirect

router = APIRouter()


@router.get("/brokers/{key}/login", response_class=HTMLResponse)
def broker_login_start(request: Request, ctx: Ctx, key: str):
    adapters = ctx.adapters
    if key not in adapters:
        return redirect(request, "/brokers", "Unbekannter Broker.", "error")
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
        return ctx.render(request, "broker_login.html", key=key, adapter=adapters[key], challenge=e.challenge)
    except AdapterError as e:
        ctx.finish_login(key, adapter)  # keeps e.g. a rejected PIN, so nothing retries it
        return redirect(request, f"/brokers/{key}", f"Anmeldung fehlgeschlagen: {e}", "error")
    ctx.finish_login(key, adapter)
    return redirect(request, "/", f"{adapters[key].label}: angemeldet - die gespeicherte Sitzung ist noch gültig, "
                                  "eine Bestätigung war nicht nötig. Du kannst jetzt abrufen.")


@router.post("/brokers/{key}/login")
def broker_login_finish(request: Request, ctx: Ctx, key: str, code: str = Form("")):
    adapters = ctx.adapters
    adapter = ctx.pending.get(key)
    if adapter is None:
        return redirect(request, f"/brokers/{key}/login", "Die Anmeldung ist abgelaufen, bitte neu starten.",
                        "warn")
    try:
        adapter.complete_login(code)
    except AuthRequired as e:
        return ctx.render(request, "broker_login.html", key=key, adapter=adapters[key], challenge=e.challenge)
    except AdapterError as e:
        ctx.pending.pop(key, None)
        ctx.finish_login(key, adapter)
        return redirect(request, f"/brokers/{key}", f"Anmeldung fehlgeschlagen: {e}", "error")
    ctx.pending.pop(key, None)
    ctx.finish_login(key, adapter)
    return redirect(request, "/", f"{adapters[key].label}: angemeldet. Du kannst jetzt abrufen.")
