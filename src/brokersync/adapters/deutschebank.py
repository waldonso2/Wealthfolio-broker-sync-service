"""Deutsche Bank via FinTS: the giro account and the maxblue depot's holdings (``FintsAdapter``).

The bank code differs per branch, so it is a credential. Login with the
Deutsche Bank ID or branch and account number; confirmation with BestSign in
the app (decoupled). Holdings come over FinTS; depot transactions don't - they
come from the depot's CSV export the user uploads (``deutschebank_csv``), and
their giro bookings (``SECURITIES_CASH``) are matched against them.
At login the adapter logs which accounts and functions the bank offers.
"""

from __future__ import annotations

from . import deutschebank_csv
from .fints import FintsAdapter, credential_fields


class DeutscheBankAdapter(FintsAdapter):
    key = "deutschebank"
    label = "Deutsche Bank"
    credential_fields = credential_fields(
        "der Deutschen Bank", "Deutsche Bank Online-Banking", blz=True,
        username_help="Deine Deutsche-Bank-ID oder Filialnummer und Kontonummer (10-stellig, ggf. mit führenden "
                      "Nullen), wie im Online-Banking.",
    )
    reports_positions = True
    statement_label = "Depotumsätze (CSV-Export aus dem Online-Banking)"
    parse_statement = staticmethod(deutschebank_csv.parse)
    statement_transactions = staticmethod(deutschebank_csv.transactions)

    server = "https://fints.deutsche-bank.de/"
    bank = "die Deutsche Bank"
    app = "Deutsche Bank App (BestSign)"
    banking = "Deutsche Bank Online-Banking"
