"""Command line: ``brokersync serve`` (web UI) and ``brokersync run`` (one sync, for the timer)."""

from __future__ import annotations

import argparse
import logging
import sys

from . import __version__
from .config import data_dir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="brokersync", description="Wealthfolio Broker Sync")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    serve = sub.add_parser("serve", help="run the web UI")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8090)
    run = sub.add_parser("run", help="sync all enabled brokers once")
    run.add_argument("--broker", action="append", help="only this broker (repeatable)")
    sub.add_parser("reset-ui-password", help="forget the web UI password; the next visit sets a new one")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.cmd == "serve":
        import uvicorn

        from .web.app import create_app

        uvicorn.run(create_app(data_dir()), host=args.host, port=args.port, log_level="info")
        return 0

    if args.cmd == "run":
        from .sync import AlreadyRunning, Syncer

        try:
            results = Syncer(data_dir()).run(args.broker)
        except AlreadyRunning as e:
            print(e)
            return 0
        for r in results:
            print(f"{r.broker}: {r.status} - {r.created} neu, {r.existing} schon vorhanden, {r.failed} fehlgeschlagen,"
                  f" {r.unknown} unbekannt")
        return 1 if any(r.status == "error" for r in results) else 0

    if args.cmd == "reset-ui-password":
        from .vault import Vault

        Vault(data_dir()).update(lambda d: d.pop("ui_password_hash", None))
        print("Web UI password removed. Open the web UI to set a new one.")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
