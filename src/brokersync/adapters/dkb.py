"""DKB via FinTS: the giro account's balance and transactions (``FintsAdapter``).

DKB confirms in the DKB app (decoupled TAN). Depot trades and payouts show up
on the giro account as ``SECURITIES_CASH``; the addon's PDF import books the
securities side. Whether DKB offers depot holdings over FinTS is not known yet.
"""

from __future__ import annotations

from .fints import FintsAdapter, credential_fields


class DkbAdapter(FintsAdapter):
    key = "dkb"
    label = "DKB"
    credential_fields = credential_fields("der DKB", "DKB-Banking")

    blz = "12030000"
    server = "https://fints.dkb.de/fints"
    bank = "die DKB"
    app = "DKB-App"
    banking = "DKB-Banking"
