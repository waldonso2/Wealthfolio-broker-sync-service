"""Whether a newer release of the service exists - asked at most once a day, after a run.

Only the redirect of GitHub's ``releases/latest`` page is read (no API, no token, nothing about
the user is sent). The answer is kept in ``meta`` (``UPDATE``); the overview shows a newer
version, and a newly found one is notified once. ``BROKERSYNC_UPDATE_CHECK=0`` turns it off.
Installing stays the user's ``update`` in the container's console.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import UTC, datetime, timedelta

import httpx

from . import meta_keys
from .model import when
from .state import State

log = logging.getLogger(__name__)

REPO = "https://github.com/waldonso2/Wealthfolio-broker-sync-service"
RELEASES = f"{REPO}/releases"
EVERY = timedelta(days=1)
KEY = meta_keys.UPDATE()


def _parse(version: str) -> tuple[int, ...] | None:
    m = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", version.strip())
    return tuple(int(x) for x in m.groups()) if m else None


def newer(latest: str, current: str) -> bool:
    a, b = _parse(latest), _parse(current)
    return bool(a and b and a > b)


def latest(http: httpx.Client) -> str | None:
    """The newest release's version (``0.27.0``), None when GitHub doesn't say."""
    r = http.get(f"{RELEASES}/latest", follow_redirects=False)
    m = re.search(r"/releases/tag/v(\d+\.\d+\.\d+)$", r.headers.get("location", ""))
    return m.group(1) if m else None


def available(state: State, current: str) -> str | None:
    """The newer version found by the last check, if any."""
    stored = json.loads(state.meta(KEY) or "{}").get("version")
    return stored if stored and newer(stored, current) else None


def check(state: State, current: str, *, http: httpx.Client | None = None) -> str | None:
    """Asks GitHub when the last answer is older than a day; returns a newer version found for the
    first time (to notify once), else None. Never raises: no answer only means no hint."""
    if os.environ.get("BROKERSYNC_UPDATE_CHECK", "1") == "0":
        return None
    stored = json.loads(state.meta(KEY) or "{}")
    if stored.get("checked") and datetime.now(UTC) - when(stored["checked"]) < EVERY:
        return None
    client = http or httpx.Client(timeout=10)
    try:
        version = latest(client)
    except httpx.HTTPError as e:
        log.info("update check: GitHub not reachable (%s)", type(e).__name__)
        version = None
    finally:
        if http is None:
            client.close()
    state.set_meta(KEY, json.dumps({"version": version or stored.get("version"),
                                    "checked": datetime.now(UTC).isoformat()}))
    if version and newer(version, current) and version != stored.get("version"):
        return version
    return None
