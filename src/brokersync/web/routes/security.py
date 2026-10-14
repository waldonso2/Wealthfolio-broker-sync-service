"""The page *Sicherheit*: checks, master passphrase set/change/remove, locking the service."""

from __future__ import annotations

import logging
from urllib.parse import urlparse

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ... import crypto
from ... import security as security_mod
from ...sync import AlreadyRunning
from ...vault import verify_password
from ..context import Ctx, redirect, tr

log = logging.getLogger(__name__)
router = APIRouter()


@router.get("/security", response_class=HTMLResponse)
def security_page(request: Request, ctx: Ctx):
    c = ctx.cfg()
    wf_plain = c.wealthfolio_url.startswith("http://") and not _is_local(c.wealthfolio_url)
    return ctx.render(request, "security.html", checks=security_mod.status(ctx.data_dir, secure=ctx.secure),
                      ca=bool(security_mod.ca_file()), ca_fingerprint=security_mod.ca_fingerprint(),
                      has_passphrase=crypto.passphrase_enabled(ctx.data_dir), min_length=crypto.MIN_PASSPHRASE,
                      wf_plain=wf_plain, sync_mode=c.sync_mode)


@router.post("/security/mode")
def security_mode(request: Request, ctx: Ctx, mode: str = Form("")):
    """Abrufmodus (#85): "auto" keeps the brokers' sessions, "safe" ends them after every fetch."""
    if mode not in ("auto", "safe"):
        return redirect(request, "/security", tr(request, "Unbekannter Abrufmodus."), "error")
    c = ctx.cfg()
    c.sync_mode = mode
    ctx.save(c)
    if mode == "safe":
        ctx.syncer.end_sessions()  # every saved login ends now, not only after the next fetch
        for adapter in ctx.pending.values():
            adapter.close()
        ctx.pending.clear()
        log.info("sync mode: safe - broker sessions ended")
        return redirect(request, "/security", tr(request, "Sicherer Modus an: Alle gespeicherten Anmeldungen sind "
                                                          "beendet. Jeder Abruf startet jetzt mit einer Anmeldung, "
                                                          "danach meldet sich der Dienst wieder ab."))
    log.info("sync mode: auto")
    return redirect(request, "/security", tr(request, "Automatischer Modus an: Anmeldungen bleiben gespeichert, der "
                                                      "tägliche Abruf läuft ohne dich."))


@router.post("/security/passphrase")
def security_passphrase(request: Request, ctx: Ctx, password: str = Form(...), action: str = Form(...),
                        new: str = Form(""), new2: str = Form("")):
    client = request.client.host if request.client else "?"
    with ctx.throttle.attempt(client) as a:
        if a.refused:
            text, params = ctx.throttle.message(a)
            return redirect(request, "/security", tr(request, text, **params), "error")
        a.ok = verify_password(password, ctx.vault.load().get("ui_password_hash"))
    if not a.ok:
        return redirect(request, "/security", tr(request, "Das Passwort der Oberfläche stimmt nicht."), "error")
    if action == "remove":
        target = None
    else:
        if new != new2:
            return redirect(request, "/security", tr(request, "Die beiden Eingaben der Passphrase sind verschieden."),
                            "error")
        if len(new) < crypto.MIN_PASSPHRASE:
            return redirect(request, "/security", tr(request, "Die Passphrase braucht mindestens {n} Zeichen.",
                                                     n=crypto.MIN_PASSPHRASE), "error")
        target = new
    try:
        security_mod.change_passphrase(ctx.data_dir, target)
    except ValueError as e:
        return redirect(request, "/security", str(e), "error")
    except AlreadyRunning:
        return redirect(request, "/security", tr(request, "Gerade läuft ein Abruf. Bitte danach erneut versuchen."),
                        "warn")
    log.info("master passphrase %s", "removed" if target is None else "set")
    ctx.gate.reset()  # the database connection and the vault use the new key from the next request on
    return redirect(request, "/security", tr(request, "Master-Passphrase entfernt." if target is None
                                             else "Master-Passphrase gesetzt. Merke sie dir gut: Ohne sie sind die "
                                                  "Daten verloren."))


@router.post("/security/lock")
def security_lock(request: Request, ctx: Ctx):
    crypto.lock(ctx.data_dir)
    ctx.gate.reset()
    log.info("web UI: service locked")
    return RedirectResponse("/unlock", 303)


def _is_local(url: str) -> bool:
    host = urlparse(url).hostname or ""
    return host in ("localhost", "127.0.0.1", "::1")
