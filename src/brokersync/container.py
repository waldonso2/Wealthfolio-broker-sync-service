"""Running as a Docker/Podman container (waldonso2/wealthfolio-importer-addon#148): ``wealthfolio-companion container``.

What systemd does in the LXC happens here in one process:

- the key comes from a container secret (``COMPANION_KEY_FILE``, ``/run/secrets/companion-key`` in the image);
  without it nothing starts - a key is never made in ``data/`` (``crypto.KeyMissing``);
- ``migrate`` (and ``recover``, by ``cli.main``) before anything reads the data;
- the first setup code is printed to the container's log while no UI password is set (``docker logs``);
- the UI certificate from the container's own CA in ``COMPANION_TLS_DIR`` (renewed at every start and, when it is
  due, by the scheduler - then the process ends and the container's restart policy brings it back with it), or plain
  HTTP behind a reverse proxy with ``COMPANION_TLS=off``;
- the daily fetch (``wealthfolio-companion-run.timer`` in the LXC) as a thread: ``RUN_AT`` German time plus up to
  ``JITTER`` minutes, under the same sync lock as every other run.

Updates are a new image (``docker compose pull``): the page *Updates* says so instead of offering to install.
"""

from __future__ import annotations

import logging
import os
import random
import signal
import socket
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from .config import data_dir
from .sync import BERLIN

log = logging.getLogger(__name__)

RUN_AT = "06:00"
JITTER = 45  # minutes, like RandomizedDelaySec of the LXC's timer
TLS_DIR = "/tls"


def active() -> bool:
    """Running in the container image (it sets COMPANION_CONTAINER)."""
    return bool(os.environ.get("COMPANION_CONTAINER"))


def next_run(now: datetime, at: str = RUN_AT, jitter: float = 0.0) -> datetime:
    """The next daily run after ``now``: ``at`` German time plus ``jitter`` minutes."""
    hour, minute = (int(x) for x in at.split(":"))
    local = now.astimezone(BERLIN)
    run = local.replace(hour=hour, minute=minute, second=0, microsecond=0) + timedelta(minutes=jitter)
    while run <= local:
        run = (run - timedelta(minutes=jitter) + timedelta(days=1)).replace(hour=hour, minute=minute)
        run += timedelta(minutes=jitter)
    return run


def run_once() -> None:
    """One scheduled run, as the LXC's timer starts it (``wealthfolio-companion run``); a failure only goes to the
    log."""
    from .cli import _run

    class Args:
        broker = None

    try:
        _run(Args())
    except Exception:
        log.exception("scheduled run failed")


def tls_enabled() -> bool:
    """TLS of the UI: on unless ``COMPANION_TLS`` says off (in the LXC the unit sets it to 1)."""
    value = os.environ.get("COMPANION_TLS", "").lower()
    return value not in ("off", "0", "no") and (bool(value) or active())


def _tls_dir() -> Path:
    return Path(os.environ.get("COMPANION_TLS_DIR", TLS_DIR))


def certificate() -> Path | None:
    """The UI certificate for this container's name and the extra names; None with ``COMPANION_TLS=off``."""
    if not tls_enabled():
        return None
    from .tls import ensure_certificate, extra_names

    directory = _tls_dir()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    names, ips = extra_names(directory)
    extra = [n.strip() for n in os.environ.get("COMPANION_CERT_NAMES", "").split(",") if n.strip()]
    try:
        own = [socket.gethostbyname(socket.gethostname())]
    except OSError:
        own = []
    ensure_certificate(directory, [socket.gethostname(), *names, *extra], own + ips)
    for f in directory.glob("*.pem"):
        f.chmod(0o600)
    return directory


def scheduler(stop: threading.Event, *, at: str = RUN_AT, run=run_once, renew=None) -> None:
    """Daily: renew the certificate when due (then end the process to load it) and fetch."""
    while not stop.is_set():
        when = next_run(datetime.now(BERLIN), at, random.uniform(0, JITTER))  # noqa: S311 - spreading only
        log.info("next scheduled run: %s", when.isoformat(timespec="minutes"))
        if stop.wait(max(0.0, (when - datetime.now(BERLIN)).total_seconds())):
            return
        if renew is not None and renew():
            log.warning("UI certificate renewed: restarting to load it")
            os.kill(os.getpid(), signal.SIGTERM)
            return
        run()


def _cert_changed() -> bool:
    directory = _tls_dir()
    before = (directory / "cert.pem").read_bytes() if (directory / "cert.pem").exists() else b""
    certificate()
    return (directory / "cert.pem").read_bytes() != before


def main(args) -> int:
    """``wealthfolio-companion container``: migrate, first setup code, certificate, scheduler, web UI."""
    from . import crypto
    from .cli import _setup_code
    from .security import migrate
    from .vault import Vault
    from .web.serve import serve

    d = data_dir()
    try:
        crypto.host_key(d)
    except crypto.KeyMissing as e:
        log.error("%s", e)
        return 2
    for line in migrate(d):
        log.info("migrate: %s", line)
    if not crypto.is_locked(d) and not Vault(d).load().get("ui_password_hash"):
        class Code:
            cmd = "setup-code"

        _setup_code(Code())  # printed to the container's log, never logged by the service itself
    tls = certificate()
    stop = threading.Event()
    threading.Thread(target=scheduler, args=(stop,), kwargs={"at": os.environ.get("COMPANION_RUN_AT", RUN_AT),
                                                              "renew": _cert_changed if tls else None},
                     daemon=True, name="scheduler").start()
    try:
        if tls:
            serve(d, host=args.host, port=8443, certfile=str(tls / "cert.pem"), keyfile=str(tls / "key.pem"),
                  redirect_port=8090)
        else:
            serve(d, host=args.host, port=8090, certfile=None, keyfile=None, redirect_port=None)
    finally:
        stop.set()
    time.sleep(0)
    return 0
