"""In front of the web UI: security headers on every answer, the brake on
wrong passwords, and the unlock page while a master passphrase is set and not
entered - the UI itself (``app._create_app``) is only built once the data can
be read, and dropped again when the service is locked.
"""

from __future__ import annotations

import ipaddress
import logging
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import __version__, crypto
from .i18n import N_, template_gettext

log = logging.getLogger(__name__)

CSP = ("default-src 'self'; script-src 'none'; style-src 'self'; img-src 'self' data:; "
       "form-action 'self'; frame-ancestors 'none'; base-uri 'none'; object-src 'none'")
HEADERS = [
    (b"content-security-policy", CSP.encode()),
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
    (b"cross-origin-opener-policy", b"same-origin"),
    (b"cross-origin-resource-policy", b"same-origin"),
    (b"cache-control", b"no-store"),
]
HSTS = (b"strict-transport-security", b"max-age=31536000")
UNKNOWN_HOST = ("Unbekannter Name für diese Oberfläche. Über die IP-Adresse geht es weiter; einen eigenen Namen "
                "erlaubt BROKERSYNC_ALLOWED_HOSTS (siehe README).")
# Largest request body: the biggest upload (a 5 MB statement) plus the form around it. Checked before
# anything is read - also for the login and the first password, which answer without a session.
MAX_BODY = 6 * 1024 * 1024


# The local name suffixes a container's name gets in a home network (the same as the CA's, tls.LOCAL_DOMAINS).
LOCAL_SUFFIXES = ("local", "lan", "home.arpa", "fritz.box", "internal", "localdomain")


def host_of(raw: str) -> str:
    """The host of a Host header, without port and brackets, lower case."""
    raw = raw.strip().lower()
    if raw.startswith("["):
        return raw[1:raw.index("]")] if "]" in raw else raw
    return raw.rsplit(":", 1)[0] if raw.count(":") == 1 else raw


class Hosts:
    """The names the UI answers to (#107). An IP address always - DNS rebinding needs a name the attacker
    controls -, a name only when it is known: the container's (also with a local suffix like ``.fritz.box``),
    the certificate's, the public URL's, or one in ``BROKERSYNC_ALLOWED_HOSTS``. ``known`` is asked again
    when a name isn't found (the public URL can change while the service runs)."""

    def __init__(self, known: Callable[[], set[str]]):
        self.known = known
        self._names = {n.lower() for n in known()}

    def allows(self, raw: str) -> bool:
        host = host_of(raw)
        if not host:
            return False
        try:
            ipaddress.ip_address(host)
            return True
        except ValueError:
            pass
        if self._match(host):
            return True
        self._names = {n.lower() for n in self.known()}
        return self._match(host)

    def _match(self, host: str) -> bool:
        if host in self._names:
            return True
        first, _, rest = host.partition(".")
        return first in self._names and rest in LOCAL_SUFFIXES


class Attempt:
    """One password check through the ``Throttle``: ``wait`` > 0 - not now (seconds), ``busy`` - too many
    checks at once; else go ahead and set ``ok`` when the password was right."""

    def __init__(self, wait: int = 0, busy: bool = False):
        self.wait, self.busy, self.ok = wait, busy, False

    @property
    def refused(self) -> bool:
        return bool(self.wait or self.busy)


class Throttle:
    """Wrong passwords (UI password, master passphrase, setup code), #90.

    - per client (IPv6 by its /64): after ``limit`` within ``window`` seconds, no attempt for ``block``;
    - all clients together: after ``global_limit`` within ``window`` (many addresses can't go on guessing);
    - an attempt counts *before* the check (argon2id, 64 MiB each) and is only taken back when it was
      right, and at most ``slots`` checks run at once - parallel requests can neither slip past the brake
      nor exhaust the container's memory;
    - at most ``max_clients`` addresses are remembered (oldest out first)."""

    def __init__(self, limit: int = 5, window: int = 900, block: int = 900, global_limit: int = 30,
                 slots: int = 2, max_clients: int = 10_000):
        self.limit, self.window, self.block = limit, window, block
        self.global_limit, self.max_clients = global_limit, max_clients
        self._fails: dict[str, list[float]] = {}
        self._all: list[float] = []
        self._lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(slots)

    @staticmethod
    def key(client: str) -> str:
        try:
            ip = ipaddress.ip_address(client)
        except ValueError:
            return client
        return str(ipaddress.ip_network(f"{ip}/64", strict=False)) if ip.version == 6 else str(ip)

    def _wait(self, key: str, now: float) -> int:
        fails = [t for t in self._fails.get(key, []) if now - t < max(self.window, self.block)]
        self._fails.pop(key, None)
        if fails:
            self._fails[key] = fails  # re-inserted: the most recent client last
        self._all = [t for t in self._all if now - t < self.window]
        waits = []
        if len(fails) >= self.limit:
            waits.append(self.block - (now - fails[-1]))
        if len(self._all) >= self.global_limit:
            waits.append(self.window - (now - self._all[0]))
        remaining = max(waits, default=0)
        return int(remaining) + 1 if remaining > 0 else 0

    @contextmanager
    def attempt(self, client: str) -> Iterator[Attempt]:
        key, now = self.key(client), time.monotonic()
        refused = None
        with self._lock:
            wait = self._wait(key, now)
            if wait:
                refused = Attempt(wait=wait)
            elif not self._slots.acquire(blocking=False):
                refused = Attempt(busy=True)
            else:
                self._fails.setdefault(key, []).append(now)
                self._all.append(now)
                while len(self._fails) > self.max_clients:
                    self._fails.pop(next(iter(self._fails)))
        if refused:
            yield refused
            return
        a = Attempt()
        try:
            yield a
        finally:
            self._slots.release()
            if a.ok:
                with self._lock:
                    self._fails.pop(key, None)
                    if now in self._all:
                        self._all.remove(now)

    def message(self, a: Attempt) -> tuple[str, dict]:
        """The text (a translation key) and its params for a refused attempt."""
        if a.busy:
            return N_("Gerade laufen zu viele Anmeldungen gleichzeitig. Bitte gleich noch einmal versuchen."), {}
        return N_("Zu viele Fehlversuche. Bitte in {n} Minuten erneut versuchen."), {"n": a.wait // 60 + 1}


class Gate:
    def __init__(self, data_dir: Path, *, secure: bool, throttle: Throttle, templates_dir: Path, static_dir: Path,
                 hosts: Hosts | None = None):
        self.data_dir = Path(data_dir)
        self.hosts = hosts  # None: every Host header (development, tests)
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
        if scope["type"] == "http":
            headers = dict(scope.get("headers") or [])
            if self.hosts and not self.hosts.allows(headers.get(b"host", b"").decode("latin-1")):
                await PlainTextResponse(UNKNOWN_HOST, status_code=400, headers={
                    k.decode(): v.decode() for k, v in extra})(scope, receive, send)
                return
            length = headers.get(b"content-length", b"")
            if length and (not length.isdigit() or int(length) > MAX_BODY):
                await PlainTextResponse("Anfrage zu groß.", status_code=413, headers={
                    k.decode(): v.decode() for k, v in extra})(scope, receive, send)
                return
            receive = _capped(receive, MAX_BODY)

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                names = {k.lower() for k, _ in message.get("headers", [])}
                message.setdefault("headers", [])
                message["headers"] = list(message["headers"]) + [(k, v) for k, v in extra if k not in names]
            await send(message)

        await self.app()(scope, receive, send_with_headers)


def _capped(receive, limit: int):
    """``receive`` that ends the request (as a disconnect) once the body grows past ``limit`` bytes - for a
    body sent without Content-Length (chunked)."""
    seen = 0

    async def wrapped():
        nonlocal seen
        message = await receive()
        if message["type"] == "http.request":
            seen += len(message.get("body", b""))
            if seen > limit:
                log.warning("web UI: request body over %d bytes, dropped", limit)
                return {"type": "http.disconnect"}
        return message

    return wrapped


def _locked_app(gate: Gate, templates_dir: Path, static_dir: Path) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    templates = Jinja2Templates(directory=templates_dir)
    # No language while locked - the config holding it can't be read: the unlock page is German.
    templates.env.globals["_"] = template_gettext
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    def page(request: Request, error: str = "", status: int = 200) -> HTMLResponse:
        return templates.TemplateResponse(request, "unlock.html", {"error": error, "version": __version__},
                                          status_code=status)

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "locked": True}

    @app.get("/unlock", response_class=HTMLResponse)
    def unlock_page(request: Request):
        return page(request)

    @app.post("/unlock")
    def unlock(request: Request, passphrase: str = Form(...)):
        client = request.client.host if request.client else "?"
        with gate.throttle.attempt(client) as a:
            if a.refused:
                text, params = gate.throttle.message(a)
                return page(request, text.format(**params), 429)
            a.ok = crypto.unlock(gate.data_dir, passphrase)
        if a.ok:
            log.info("web UI: service unlocked")
            return RedirectResponse("/login", 303)
        log.warning("web UI: wrong master passphrase from %s", client)
        return page(request, "Die Passphrase stimmt nicht.", 403)

    @app.api_route("/{path:path}", methods=["GET", "POST"])
    def everything_else(path: str):
        return RedirectResponse("/unlock", 303)

    return app
