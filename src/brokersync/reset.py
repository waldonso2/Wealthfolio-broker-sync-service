"""Start a broker over: delete every activity the sync booked for it and forget its sync state.

Used when a broker's booking rules change (comdirect 0.7.0 books only the depot,
no longer the giro account): the next run fetches everything from the start
date again and books it with the current rules. Only activities carrying the
sync's own ``[SYNC <broker>:…]`` reference in the broker's two accounts are
deleted - CSV/PDF imports and activities entered by hand stay. The user
confirms it in the web UI.
"""

from __future__ import annotations

from datetime import date

from .mapping import Accounts
from .state import State
from .wealthfolio import WealthfolioClient, WealthfolioError


def own_activities(wf: WealthfolioClient, broker: str, accounts: Accounts) -> list[dict]:
    tag = f"[SYNC {broker}:"
    return [a for a in wf.search_activities([accounts.cash, accounts.portfolio], date(1970, 1, 1), date(2100, 1, 1))
            if tag in (a.get("comment") or "")]


def reset(wf: WealthfolioClient, state: State, broker: str, accounts: Accounts) -> tuple[int, list[str]]:
    """Delete the sync's activities of ``broker``; returns (deleted, errors). The state is reset only without errors."""
    deleted = 0
    errors: list[str] = []
    gone_groups: set[str] = set()
    for a in own_activities(wf, broker, accounts):
        group = a.get("sourceGroupId")
        if group and group in gone_groups:
            deleted += 1  # Wealthfolio deleted it with its partner leg
            continue
        try:
            wf.delete_activity(a["id"])
            deleted += 1
        except WealthfolioError as e:
            if "not found" in str(e).lower():
                deleted += 1
            else:
                errors.append(f"{str(a.get('date', ''))[:10]} {a.get('activityType')} {a.get('amount')}: {e}")
                continue
        if group:
            gone_groups.add(group)
    if not errors:
        state.reset_broker(broker)
    return deleted, errors
