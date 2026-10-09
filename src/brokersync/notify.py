"""Notifications via ntfy (https://ntfy.sh or a self-hosted server).

A notification carries a link to the page that fixes the problem (e.g. the
broker's login page for a TAN). Failing to notify never fails a sync.
"""

from __future__ import annotations

import base64
import logging

import httpx

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, server: str, topic: str, token: str | None = None, *,
                 transport: httpx.BaseTransport | None = None):
        self.server = server.rstrip("/")
        self.topic = topic.strip()
        self.token = token or None
        self.transport = transport

    @property
    def enabled(self) -> bool:
        return bool(self.server and self.topic)

    def send(self, title: str, message: str, *, link: str | None = None, priority: str = "default",
             tags: str = "") -> bool:
        if not self.enabled:
            log.info("notification (ntfy not configured): %s - %s", title, message)
            return False
        headers = {"Title": _header(title), "Priority": priority}
        if link:
            headers["Click"] = link
        if tags:
            headers["Tags"] = tags
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            with httpx.Client(timeout=15, transport=self.transport) as c:
                r = c.post(f"{self.server}/{self.topic}", content=message.encode(), headers=headers)
            r.raise_for_status()
            return True
        except httpx.HTTPError as e:
            log.warning("ntfy notification failed: %s", e)
            return False


def _header(s: str) -> str:
    # HTTP headers are ASCII; ntfy decodes RFC 2047 for anything else (umlauts).
    if s.isascii():
        return s
    return f"=?UTF-8?B?{base64.b64encode(s.encode()).decode()}?="
