"""DKB via FinTS: the giro account's balance and transactions, and the depot's holdings (``FintsAdapter``).

DKB confirms in the DKB app (decoupled TAN). Depot trades and payouts show up
on the giro account as ``SECURITIES_CASH``; a PDF import of the statements books
the securities side. The depot (no IBAN, only in the user parameters) delivers
its holdings over FinTS (HKWPD); they are compared with Wealthfolio after each
run. DKB also offers depot transactions (HKWDU, MT536), which python-fints
doesn't speak.
"""

from __future__ import annotations

from .fints import FintsAdapter, credential_fields


class DkbAdapter(FintsAdapter):
    key = "dkb"
    label = "DKB"
    credential_fields = credential_fields("der DKB", "DKB-Banking")
    reports_positions = True

    blz = "12030000"
    server = "https://fints.dkb.de/fints"
    bank = "die DKB"
    app = "DKB-App"
    banking = "DKB-Banking"
