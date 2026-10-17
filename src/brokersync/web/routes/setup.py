"""First visit and login: UI password, login, logout, health check, the CA certificate."""

from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response

from ... import __version__
from ... import security as security_mod
from ...model import when
from ...notify import Notifier
from ...vault import check_setup_code, end_ui_sessions, hash_password, needs_rehash, verify_password, weak_password
from ..context import Ctx, redirect, sign_in, tr

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


@router.get("/metrics", response_class=PlainTextResponse)
def metrics(ctx: Ctx) -> str:
    """Prometheus text format (OPS-04 of #85): runs per broker and outcome, the last run of each. After the UI
    login, or with ``BROKERSYNC_METRICS_TOKEN`` as bearer token (``app.require_login``). No amounts, no names."""
    counts = ctx.state.run_counts()
    lines = ["# HELP brokersync_runs_total Fetches per broker and outcome.", "# TYPE brokersync_runs_total counter"]
    lines += [f'brokersync_runs_total{{broker="{b}",status="{s}"}} {n}' for b, s, n in counts]
    last = {b: r for b in sorted({b for b, _, _ in counts}) if (r := ctx.state.last_run(b))}
    gauges = {
        "last_run_timestamp_seconds": ("Start of the last fetch (Unix time).",
                                       lambda r: when(r.started_at).timestamp()),
        "last_run_duration_seconds": ("Duration of the last fetch.", lambda r: (
            when(r.finished_at) - when(r.started_at)).total_seconds() if r.finished_at else None),
        "last_run_ok": ("1 if the last fetch succeeded.", lambda r: int(r.status == "ok")),
        "last_run_failed_bookings": ("Bookings the last fetch could not book.", lambda r: r.failed),
    }
    for name, (text, value) in gauges.items():
        lines += [f"# HELP brokersync_{name} {text}", f"# TYPE brokersync_{name} gauge"]
        lines += [f'brokersync_{name}{{broker="{b}"}} {v}' for b, r in last.items() if (v := value(r)) is not None]
    lines += ["# HELP brokersync_unknown_events Unknown bookings waiting on the page Unbekannt.",
              "# TYPE brokersync_unknown_events gauge", f"brokersync_unknown_events {ctx.state.unknown_count()}"]
    return "\n".join(lines) + "\n"


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
        ctx.event(request, "setup_password", ok=False)
        return redirect(request, "/setup-password",
                        tr(request, "Der Einrichtungscode stimmt nicht oder ist abgelaufen."), "error")
    if error := password_problem(request, password, password2):
        # The page again, the code in its form - never in a URL, where the browser's history keeps it (V-01).
        page = ctx.render(request, "setup_password.html", code=code.strip()[:64],
                          flash={"text": error, "level": "error"})
        page.status_code = 422
        return page

    hashed = hash_password(password)

    def set_password(d: dict) -> None:
        d["ui_password_hash"] = hashed
        d.pop("setup_code", None)  # used up

    sign_in(request, ctx.vault.update(set_password))
    ctx.event(request, "setup_password")
    url = str(request.base_url).rstrip("/")
    ctx.update(lambda c: setattr(c, "public_url", c.public_url or url))
    return redirect(request, "/", tr(request, "Passwort gespeichert. Die Übersicht zeigt, was noch einzurichten ist."))


def password_problem(request: Request, password: str, password2: str) -> str | None:
    """Why a new UI password can't be taken, or None (#96)."""
    if len(password) < MIN_PASSWORD or password != password2:
        return tr(request, "Das Passwort braucht mindestens {n} Zeichen, beide Eingaben müssen gleich sein.",
                  n=MIN_PASSWORD)
    if weak_password(password):
        return tr(request, "Dieses Passwort ist zu verbreitet und schnell erraten. Am besten eines aus einem "
                           "Passwortmanager.")
    return None


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
        secrets_ = ctx.vault.load()
        if stored and needs_rehash(stored):
            rehashed = hash_password(password)
            secrets_ = ctx.vault.update(lambda d: d.__setitem__("ui_password_hash", rehashed))
        sign_in(request, secrets_)
        _new_client(ctx, client)
        ctx.event(request, "login")
        return redirect(request, "/")
    log.warning("web UI: wrong password from %s", client)
    ctx.event(request, "login", ok=False)
    return redirect(request, "/login", tr(request, "Falsches Passwort."), "error")


def _new_client(ctx, client: str) -> None:
    """A login from an address none came from before (#102): ntfy says so - unless it is the very first."""
    if ctx.state.known_client(client) or not ctx.state.events(1):
        return
    try:
        c = ctx.cfg()
        if c.ntfy_topic:
            (ctx.notifier or Notifier(c.ntfy_server, c.ntfy_topic, c.ntfy_token)).send(
                "Broker Sync: neue Anmeldung", f"Anmeldung an der Oberfläche von {client} - zum ersten Mal von dort. "
                "Warst du das nicht, das Passwort ändern.", link=f"{c.public_url}/security" if c.public_url else None,
                tags="warning")
    except Exception:  # a notification never stops a login
        log.exception("notification of a new login failed")


@router.post("/theme")
def theme_switch(request: Request, next: str = Form("/")):
    """Light/dark the other way round than the device, for this session (#123): every page until logout."""
    request.session["flip"] = not request.session.get("flip")
    # Back to the page it came from - a path of this site only, never another host.
    safe = re.fullmatch(r"/[A-Za-z0-9/_.\-]*", next) and not next.startswith("//")
    return RedirectResponse(next if safe else "/", 303)


@router.post("/logout")
def logout(request: Request, ctx: Ctx):
    # A copied cookie signs in nobody afterwards (#96); one user, so every session ends.
    ctx.vault.update(end_ui_sessions)
    ctx.event(request, "logout")
    request.session.clear()
    return RedirectResponse("/login", 303, headers={"Clear-Site-Data": '"cookies"'})
