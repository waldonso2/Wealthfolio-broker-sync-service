"""Runs the web UI: HTTPS with the certificate systemd hands over, plus a plain
HTTP port that only redirects to it (old bookmarks and links keep working)."""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from pathlib import Path
from urllib.parse import urlsplit

import uvicorn

from .. import config as config_mod
from .app import create_app
from .gate import Hosts

log = logging.getLogger(__name__)


def known_hosts(data_dir: Path, certfile: str | None) -> set[str]:
    """The names the UI answers to besides IP addresses (#107): this machine's, the certificate's, the public
    URL's (read when asked: it can change) and ``BROKERSYNC_ALLOWED_HOSTS`` (comma-separated)."""
    names = {socket.gethostname(), "localhost"}
    names |= {n.strip() for n in os.environ.get("BROKERSYNC_ALLOWED_HOSTS", "").split(",") if n.strip()}
    if certfile and Path(certfile).exists():
        from cryptography import x509

        try:
            cert = x509.load_pem_x509_certificate(Path(certfile).read_bytes())
            san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            names |= set(san.get_values_for_type(x509.DNSName))
        except (ValueError, x509.ExtensionNotFound):
            pass
    try:
        url = config_mod.load_notify(data_dir).public_url
    except Exception:  # unreadable for now (e.g. no data yet): the other names still count
        url = ""
    if url:
        names.add(urlsplit(url).hostname or "")
    return {n.lower() for n in names if n}


def redirect_app(https_port: int, hosts: Hosts | None = None):
    async def app(scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        raw = dict(scope.get("headers") or []).get(b"host", b"").decode("latin-1")
        if hosts and not hosts.allows(raw):  # never a redirect to a host someone else named (#107)
            await send({"type": "http.response.start", "status": 400, "headers": [(b"content-length", b"0")]})
            await send({"type": "http.response.body", "body": b""})
            return
        host = raw[:raw.index("]") + 1] if raw.startswith("[") and "]" in raw else raw.split(":")[0]
        host = host or (scope.get("server") or ("localhost",))[0]
        path = scope.get("raw_path", scope.get("path", "/").encode()).decode("latin-1")
        query = scope.get("query_string", b"").decode("latin-1")
        location = f"https://{host}:{https_port}{path}" + (f"?{query}" if query else "")
        await send({"type": "http.response.start", "status": 308,
                    "headers": [(b"location", location.encode("latin-1")), (b"content-length", b"0")]})
        await send({"type": "http.response.body", "body": b""})

    return app


def proxy_options() -> dict:
    """Behind a reverse proxy every client has the proxy's address, so five wrong passwords anywhere would
    lock everyone out. ``BROKERSYNC_TRUST_PROXY`` names the proxy (address, comma-separated for several):
    only its ``X-Forwarded-For`` is believed, then the brake counts per real client (#90). Unset: no header
    is believed - the default, direct access."""
    trusted = os.environ.get("BROKERSYNC_TRUST_PROXY", "").strip()
    return {"proxy_headers": True, "forwarded_allow_ips": trusted} if trusted else {"proxy_headers": False}


def serve(data_dir: Path, *, host: str, port: int, certfile: str | None, keyfile: str | None,
          redirect_port: int | None) -> None:
    secure = bool(certfile and keyfile)
    hosts = Hosts(lambda: known_hosts(data_dir, certfile))
    app = create_app(data_dir, secure=secure, hosts=hosts)
    configs = [uvicorn.Config(app, host=host, port=port, log_config=None, ssl_certfile=certfile,
                              ssl_keyfile=keyfile, server_header=False, **proxy_options())]
    if secure and redirect_port:
        configs.append(uvicorn.Config(redirect_app(port, hosts), host=host, port=redirect_port, log_config=None,
                                      server_header=False, access_log=False))
    if not secure:
        log.warning("web UI without TLS: passwords cross the network in plain text")
    servers = [uvicorn.Server(c) for c in configs]

    async def main():
        async def stop_together():
            # Only the last server's signal handler is active: stop the others with it.
            while not any(s.should_exit for s in servers):
                await asyncio.sleep(0.5)
            for s in servers:
                s.should_exit = True

        await asyncio.gather(*(s.serve() for s in servers), stop_together())

    asyncio.run(main())
