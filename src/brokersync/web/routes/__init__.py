"""The UI's pages, one router per area (#76). ``ROUTERS`` is the order ``app._create_app`` includes them in."""

from __future__ import annotations

from . import broker_login, brokers, check, duplicates, overview, security, settings, setup

ROUTERS = [setup.router, overview.router, settings.router, brokers.router, broker_login.router, check.router,
           duplicates.router, security.router]
