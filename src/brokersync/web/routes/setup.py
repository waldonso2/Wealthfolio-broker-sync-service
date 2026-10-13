"""First visit and login: UI password, login, logout, health check, the CA certificate."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from ... import __version__
from ... import security as security_mod
from ...vault import hash_password, needs_rehash, verify_password
from ..context import Ctx, redirect, tr

log = logging.getLogger(__name__)
router = APIRouter()

MIN_PASSWORD = 12


@router.get("/ca.crt")
def ca_certificate():
    """The container's CA certificate (public), to import once so browsers trust the UI."""
    path = security_mod.ca_file()
    if not path:
        raise HTTPException(404, "Kein CA-Zertifikat vorhanden.")
    return Response(path.read_bytes(), media_type="application/x-x509-ca-cert",
                    headers={"Content-Disposition": 'attachment; filename="wealthfolio-broker-sync-ca.crt"'})


@router.get("/healthz")
def healthz():
    return {"ok": True, "version": __version__}


@router.get("/setup-password", response_class=HTMLResponse)
def setup_password_page(request: Request, ctx: Ctx):
    if ctx.vault.load().get("ui_password_hash"):
        return RedirectResponse("/login", 303)
    return ctx.render(request, "setup_password.html")


@router.post("/setup-password")
def setup_password(request: Request, ctx: Ctx, password: str = Form(...), password2: str = Form(...)):
    if ctx.vault.load().get("ui_password_hash"):
        return RedirectResponse("/login", 303)
    if len(password) < MIN_PASSWORD or password != password2:
        return redirect(request, "/setup-password",
                        tr(request, "Das Passwort braucht mindestens {n} Zeichen, beide Eingaben müssen gleich sein.",
                           n=MIN_PASSWORD), "error")
    ctx.vault.update(lambda d: d.__setitem__("ui_password_hash", hash_password(password)))
    request.session.clear()  # a fresh session, nothing carried over from before the password existed
    request.session["user"] = "admin"
    if not ctx.cfg().public_url:
        c = ctx.cfg()
        c.public_url = str(request.base_url).rstrip("/")
        ctx.save(c)
    return redirect(request, "/", tr(request, "Passwort gespeichert. Jetzt Schritt für Schritt einrichten."))


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, ctx: Ctx):
    return ctx.render(request, "login.html")


@router.post("/login")
def login(request: Request, ctx: Ctx, password: str = Form(...)):
    client = request.client.host if request.client else "?"
    wait = ctx.throttle.blocked(client)
    if wait:
        return redirect(request, "/login", tr(request, "Zu viele Fehlversuche. Bitte in {n} Minuten erneut "
                                                       "versuchen.", n=wait // 60 + 1), "error")
    stored = ctx.vault.load().get("ui_password_hash")
    if verify_password(password, stored):
        ctx.throttle.success(client)
        if stored and needs_rehash(stored):
            ctx.vault.update(lambda d: d.__setitem__("ui_password_hash", hash_password(password)))
        request.session.clear()
        request.session["user"] = "admin"
        return redirect(request, "/")
    ctx.throttle.failure(client)
    log.warning("web UI: wrong password from %s", client)
    return redirect(request, "/login", tr(request, "Falsches Passwort."), "error")


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", 303)
