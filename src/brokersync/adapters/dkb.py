"""DKB via FinTS: the giro account's balance and transactions, and the depot's holdings (``FintsAdapter``).

DKB confirms in the DKB app (decoupled TAN). Depot trades and payouts show up
on the giro account as ``SECURITIES_CASH``. The depot (no IBAN, only in the
user parameters) delivers its holdings over FinTS (HKWPD); they are compared
with Wealthfolio after each run. Its transactions (HKWDU, MT536 - python-fints
doesn't speak them, ``mt536``) are booked as purchases and sales, each with the
amount of the giro booking that settled it, which is matched in the same run;
a PDF import of the statements is recognised (``ExistingIndex``).
"""

from __future__ import annotations

from .fints import FintsAdapter, credential_fields


class DkbAdapter(FintsAdapter):
    key = "dkb"
    label = "DKB"
    credential_fields = credential_fields("der DKB", "DKB-Banking")
    reports_positions = True
    book_depot_movements = True

    blz = "12030000"
    server = "https://fints.dkb.de/fints"
    bank = "die DKB"
    app = "DKB-App"
    banking = "DKB-Banking"
