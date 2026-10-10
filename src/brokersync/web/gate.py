"""In front of the web UI: security headers on every answer, the brake on
wrong passwords, and the unlock page while a master passphrase is set and not
entered - the UI itself (``app._create_app``) is only built once the data can
be read, and dropped again when the service is locked.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import __version__, crypto

log = logging.getLogger(__name__)

CSP = ("default-src 'self'; script-src 'none'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "form-action 'self'; frame-ancestors 'none'; base-uri 'none'; object-src 'none'")
HEADERS = [
    (b"content-security-policy", CSP.encode()),
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
    (b"cross-origin-opener-policy", b"same-origin"),
    (b"cache-control", b"no-store"),
]
HSTS = (b"strict-transport-security", b"max-age=31536000")


class Throttle:
    """Wrong passwords per client: after ``limit`` within ``window`` seconds, no attempt for ``block`` seconds."""

    def __init__(self, limit: int = 5, window: int = 900, block: int = 900):
        self.limit, self.window, self.block = limit, window, block
        self._fails: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def blocked(self, client: str) -> int:
        """Seconds the client still has to wait (0: may try)."""
        now = time.monotonic()
        with self._lock:
            fails = [t for t in self._fails.get(client, []) if now - t < max(self.window, self.block)]
            self._fails[client] = fails
            if len(fails) < self.limit:
                return 0
            remaining = self.block - (now - fails[-1])
            if remaining <= 0:
                self._fails.pop(client, None)
                return 0
            return int(remaining) + 1

    def failure(self, client: str) -> None:
        with self._lock:
            self._fails.setdefault(client, []).append(time.monotonic())

    def success(self, client: str) -> None:
        with self._lock:
            self._fails.pop(client, None)


class Gate:
    def __init__(self, data_dir: Path, *, secure: bool, throttle: Throttle, templates_dir: Path, static_dir: Path):
        self.data_dir = Path(data_dir)
        self.secure = secure
        self.throttle = throttle
        self.build: Callable[[], FastAPI] | None = None
        self.inner: FastAPI | None = None
        self._build_lock = threading.Lock()
        self.locked_app = _locked_app(self, templates_dir, static_dir)

    def reset(self) -> None:
        """Rebuild the UI on the next request (after the keys changed)."""
        self.inner = None

    def app(self):
        if crypto.is_locked(self.data_dir):
            self.inner = None
            return self.locked_app
        if self.inner is None:
            with self._build_lock:
                if self.inner is None:
                    self.inner = self.build()
        return self.inner

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        extra = HEADERS + ([HSTS] if self.secure else [])

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                names = {k.lower() for k, _ in message.get("headers", [])}
                message.setdefault("headers", [])
                message["headers"] = list(message["headers"]) + [(k, v) for k, v in extra if k not in names]
            await send(message)

        await self.app()(scope, receive, send_with_headers)


def _locked_app(gate: Gate, templates_dir: Path, static_dir: Path) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    templates = Jinja2Templates(directory=templates_dir)
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    def page(request: Request, error: str = "", status: int = 200) -> HTMLResponse:
        return templates.TemplateResponse(request, "unlock.html", {"error": error, "version": __version__},
                                          status_code=status)

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "version": __version__, "locked": True}

    @app.get("/unlock", response_class=HTMLResponse)
    def unlock_page(request: Request):
        return page(request)

    @app.post("/unlock")
    def unlock(request: Request, passphrase: str = Form(...)):
        client = request.client.host if request.client else "?"
        wait = gate.throttle.blocked(client)
        if wait:
            return page(request, f"Zu viele Fehlversuche. Bitte in {wait // 60 + 1} Minuten erneut versuchen.", 429)
        if crypto.unlock(gate.data_dir, passphrase):
            gate.throttle.success(client)
            log.info("web UI: service unlocked")
            return RedirectResponse("/login", 303)
        gate.throttle.failure(client)
        log.warning("web UI: wrong master passphrase from %s", client)
        return page(request, "Die Passphrase stimmt nicht.", 403)

    @app.api_route("/{path:path}", methods=["GET", "POST"])
    def everything_else(path: str):
        return RedirectResponse("/unlock", 303)

    return app
