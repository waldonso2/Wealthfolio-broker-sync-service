"""Every key of the ``meta`` table in ``state.db`` (#77): what it holds and whether it belongs to a broker.

No module builds a ``meta`` key itself - it calls one of these. ``State.forget_broker`` and
``State.reset_broker`` delete the keys of ``BROKER_KEYS`` for a broker.

Kinds: ``flag`` (set or not, value = when it was set), ``text``, ``json``.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MetaKey:
    prefix: str
    kind: str  # flag | text | json
    per_broker: bool
    doc: str

    def __call__(self, suffix: str = "") -> str:
        return f"{self.prefix}:{suffix}" if suffix else self.prefix


ASSETS_LEARNED = MetaKey("assets-learned", "flag", True,
                         "positions' assets were learned once from the whole history (assets.py)")
CASH_ASSETS_REPAIRED = MetaKey("cash-assets-repaired", "flag", True,
                               "the $CASH asset was removed from the sync's own cash activities (repair.py)")
REFETCH = MetaKey("refetch", "flag", True, "the next run reads again from the start date")
SAVINGS_ACCOUNT = MetaKey("savings-account", "text", True,
                          "the savings account (Wealthfolio id) a run read without failures")
DIVIDEND_TAX = MetaKey("dividend-tax", "flag", True,
                       "dividends matched before were re-read once to add their tax to imports")
HISTORY_START = MetaKey("history-start", "text", True, "the earliest giro day ever read (ISO date)")
POSITIONS = MetaKey("positions", "json", True,
                    "the broker's positions of the last check: [{isin, name, shares, ...}] (page Prüfung)")
RETIRED_NOTICE = MetaKey("retired-notice", "json", True,
                         "a removed broker left activities in Wealthfolio: {label, activities, search}")
LOGO = MetaKey("logo", "json", False, "logo status of a Wealthfolio asset: {isin, status, at} (logos.py)")
LOGOS_LAST = MetaKey("logos:last", "json", False, "the result of the last logo pass (logos.py)")
PROGRESS = MetaKey("progress", "json", False,
                   "the run going on: {broker, label, phase, n, m, since}, gone after it (overview, #104)")
CHECKLIST_HIDDEN = MetaKey("checklist-hidden", "flag", False, "the setup checklist of the overview was hidden (#110)")
UPDATE = MetaKey("update", "json", False, "the newest release GitHub named: {version, checked} (updates.py)")
SPENDING = MetaKey("spending", "json", False,
                   "cash accounts the sync put into Wealthfolio's spending tracker: [account id] (spending.py, #134)")

ALL = (ASSETS_LEARNED, CASH_ASSETS_REPAIRED, REFETCH, SAVINGS_ACCOUNT, DIVIDEND_TAX, HISTORY_START, POSITIONS,
       RETIRED_NOTICE, LOGO, LOGOS_LAST, PROGRESS, CHECKLIST_HIDDEN, UPDATE,
       SPENDING)
BROKER_KEYS = tuple(k for k in ALL if k.per_broker)
