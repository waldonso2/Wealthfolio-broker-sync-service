"""Command line: ``brokersync serve`` (web UI, HTTPS), ``brokersync run`` (one sync, for the timer) and the
helpers ``deploy/setup.sh`` uses (``make-cert``, ``migrate``)."""

from __future__ import annotations

import argparse
import logging
import sys

from . import __version__, redact
from .config import data_dir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="brokersync", description="Wealthfolio Broker Sync")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    serve = sub.add_parser("serve", help="run the web UI")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8090)
    serve.add_argument("--certfile", help="TLS certificate: serve HTTPS")
    serve.add_argument("--keyfile", help="TLS key")
    serve.add_argument("--redirect-port", type=int, help="also listen with plain HTTP here, redirecting to HTTPS")
    run = sub.add_parser("run", help="sync all enabled brokers once")
    run.add_argument("--broker", action="append", help="only this broker (repeatable)")
    sub.add_parser("reset-ui-password", help="forget the web UI password; the next visit sets a new one")
    sc = sub.add_parser("install-sc", help="install or update Scalable's official CLI (sc), signature-checked")
    sc.add_argument("--dir", default="/opt/wealthfolio-broker-sync/bin")
    cert = sub.add_parser("make-cert", help="create or renew the self-signed TLS certificate of the web UI")
    cert.add_argument("--dir", default="/etc/wealthfolio-broker-sync/tls")
    cert.add_argument("--host", action="append", default=[], help="host name for the certificate (repeatable)")
    cert.add_argument("--ip", action="append", default=[], help="IP address for the certificate (repeatable)")
    sub.add_parser("migrate", help="bring the data of an older version up to date (encryption)")
    sub.add_parser("security-status", help="what is protected how (the page Sicherheit)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    redact.install()

    if args.cmd == "serve":
        from .web.serve import serve

        serve(data_dir(), host=args.host, port=args.port, certfile=args.certfile, keyfile=args.keyfile,
              redirect_port=args.redirect_port)
        return 0

    if args.cmd == "make-cert":
        from pathlib import Path

        from .tls import ensure_certificate

        print(ensure_certificate(Path(args.dir), args.host, args.ip))
        return 0

    if args.cmd == "migrate":
        from .security import migrate

        for line in migrate(data_dir()) or ["Nothing to migrate."]:
            print(line)
        return 0

    if args.cmd == "security-status":
        import os

        from .security import status

        for c in status(data_dir(), secure=bool(os.environ.get("BROKERSYNC_TLS"))):
            print(f"{'HINWEIS' if c.warn else 'OK' if c.ok else 'PROBLEM':8} {c.label}: {c.detail}")
        return 0

    if args.cmd == "run":
        from . import crypto
        from .sync import AlreadyRunning, Syncer

        if crypto.is_locked(data_dir()):
            _notify_locked()
            return 1
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

        def forget(d: dict) -> None:
            d.pop("ui_password_hash", None)
            d.pop("ui_session_key", None)  # signs everyone out

        Vault(data_dir()).update(forget)
        print("Web UI password removed. Open the web UI to set a new one.")
        return 0
    if args.cmd == "install-sc":
        from pathlib import Path

        from .sc_install import InstallError, install

        try:
            version, new = install(Path(args.dir))
        except (InstallError, OSError) as e:
            print(f"Scalable CLI not installed: {e}", file=sys.stderr)
            return 1
        print(f"Scalable CLI {version} {'installed' if new else 'is up to date'} in {args.dir}.")
        return 0
    return 2


def _notify_locked() -> None:
    """The timer found the service locked (master passphrase, after a restart): say so, with a link."""
    from . import config as config_mod
    from .notify import Notifier

    logging.getLogger("brokersync").warning("service is locked: enter the master passphrase in the web UI")
    cfg = config_mod.load_notify(data_dir())
    link = f"{cfg.public_url.rstrip('/')}/unlock" if cfg.public_url else None
    Notifier(cfg.ntfy_server, cfg.ntfy_topic, cfg.ntfy_token).send(
        "Broker Sync gesperrt", "Nach einem Neustart: Bitte in der Weboberfläche die Master-Passphrase eingeben, "
        "sonst ruft der Dienst nichts ab.", link=link, priority="high", tags="lock")


if __name__ == "__main__":
    sys.exit(main())
