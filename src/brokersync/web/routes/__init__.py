"""The UI's pages, one router per area (#76). ``ROUTERS`` is the order ``app._create_app`` includes them in."""

from __future__ import annotations

from . import broker_login, brokers, check, duplicates, overview, security, settings, setup, transfers

ROUTERS = [setup.router, overview.router, settings.router, brokers.router, broker_login.router, check.router,
           # *Überträge*: listed under *Einstellungen* only while patterns are saved (#84).
           transfers.router,
           # Hidden for now (waldonso2/wealthfolio-importer-addon#84) - back into ROUTERS to show it again:
           # duplicates.router,
           security.router]
# The page not shown at the moment (#84); its code and tests stay (tests include it on its own).
HIDDEN = [duplicates.router]
