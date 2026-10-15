"""First visit and login: UI password, login, logout, health check, the CA certificate."""

from __future__ import annotations

import logging
from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from ... import __version__
from ... import security as security_mod
from ...vault import check_setup_code, hash_password, needs_rehash, password_tag, verify_password
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
def healthz(request: Request):
    # The version only after the login (#107): it tells a scanner which fixes are missing.
    return {"ok": True, "version": __version__} if request.session.get("user") else {"ok": True}


@router.get("/setup-password", response_class=HTMLResponse)
def setup_password_page(request: Request, ctx: Ctx):
    if ctx.vault.load().get("ui_password_hash"):
        return RedirectResponse("/login", 303)
    return ctx.render(request, "setup_password.html", code=request.query_params.get("code", "")[:64])


@router.post("/setup-password")
def setup_password(request: Request, ctx: Ctx, password: str = Form(...), password2: str = Form(...),
                   code: str = Form("")):
    if ctx.vault.load().get("ui_password_hash"):
        return RedirectResponse("/login", 303)
    client = request.client.host if request.client else "?"
    # The one-time code (#89): something only the console of the container shows.
    with ctx.throttle.attempt(client) as a:
        if a.refused:
            text, params = ctx.throttle.message(a)
            return redirect(request, "/setup-password", tr(request, text, **params), "error")
        a.ok = check_setup_code(ctx.vault, code)
    if not a.ok:
        log.warning("web UI: wrong or expired setup code from %s", client)
        return redirect(request, "/setup-password",
                        tr(request, "Der Einrichtungscode stimmt nicht oder ist abgelaufen."), "error")
    if len(password) < MIN_PASSWORD or password != password2:
        return redirect(request, f"/setup-password?code={quote(code.strip())}",
                        tr(request, "Das Passwort braucht mindestens {n} Zeichen, beide Eingaben müssen gleich sein.",
                           n=MIN_PASSWORD), "error")

    hashed = hash_password(password)

    def set_password(d: dict) -> None:
        d["ui_password_hash"] = hashed
        d.pop("setup_code", None)  # used up

    ctx.vault.update(set_password)
    request.session.clear()  # a fresh session, nothing carried over from before the password existed
    request.session.update(user="admin", pw=password_tag(hashed))
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
    with ctx.throttle.attempt(client) as a:
        if a.refused:
            text, params = ctx.throttle.message(a)
            return redirect(request, "/login", tr(request, text, **params), "error")
        stored = ctx.vault.load().get("ui_password_hash")
        a.ok = verify_password(password, stored)
    if a.ok:
        if stored and needs_rehash(stored):
            stored = hash_password(password)
            ctx.vault.update(lambda d: d.__setitem__("ui_password_hash", stored))
        request.session.clear()
        request.session.update(user="admin", pw=password_tag(stored))
        return redirect(request, "/")
    log.warning("web UI: wrong password from %s", client)
    return redirect(request, "/login", tr(request, "Falsches Passwort."), "error")


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", 303)
