"""Deutsche Bank via FinTS: the giro account and the maxblue depot's holdings (``FintsAdapter``).

The bank code differs per branch, so it is a credential. Login with the
Deutsche Bank ID or branch and account number; confirmation with BestSign in
the app (decoupled). The bank lists depot holdings among its FinTS functions;
depot trades and payouts show up on the giro account as ``SECURITIES_CASH``.
At login the adapter logs which accounts and functions the bank offers.
"""

from __future__ import annotations

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

    server = "https://fints.deutsche-bank.de/"
    bank = "die Deutsche Bank"
    app = "Deutsche Bank App (BestSign)"
    banking = "Deutsche Bank Online-Banking"
