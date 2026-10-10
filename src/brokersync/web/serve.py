"""Runs the web UI: HTTPS with the certificate systemd hands over, plus a plain
HTTP port that only redirects to it (old bookmarks and links keep working)."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import uvicorn

from .app import create_app

log = logging.getLogger(__name__)


def redirect_app(https_port: int):
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
        host = raw[:raw.index("]") + 1] if raw.startswith("[") and "]" in raw else raw.split(":")[0]
        host = host or (scope.get("server") or ("localhost",))[0]
        path = scope.get("raw_path", scope.get("path", "/").encode()).decode("latin-1")
        query = scope.get("query_string", b"").decode("latin-1")
        location = f"https://{host}:{https_port}{path}" + (f"?{query}" if query else "")
        await send({"type": "http.response.start", "status": 308,
                    "headers": [(b"location", location.encode("latin-1")), (b"content-length", b"0")]})
        await send({"type": "http.response.body", "body": b""})

    return app


def serve(data_dir: Path, *, host: str, port: int, certfile: str | None, keyfile: str | None,
          redirect_port: int | None) -> None:
    secure = bool(certfile and keyfile)
    app = create_app(data_dir, secure=secure)
    configs = [uvicorn.Config(app, host=host, port=port, log_config=None, ssl_certfile=certfile,
                              ssl_keyfile=keyfile, server_header=False, proxy_headers=False)]
    if secure and redirect_port:
        configs.append(uvicorn.Config(redirect_app(port), host=host, port=redirect_port, log_config=None,
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
