"""Finds activities the sync created although the same transaction was already
in Wealthfolio from the addon's CSV or PDF import - and removes the sync's copy.

Before 0.3.2 the check against existing activities required the same symbol,
but the addon books securities under the ticker the user mapped and the sync
under the ISIN: trades and dividends imported by CSV were created a second
time. This finds them with the same rules the sync uses now
(``dedup.ExistingIndex``).

Only the sync's own activities are ever deleted - all of one transaction (the
trade or dividend and its transfer legs, recognised by the shared
``[SYNC <broker>:<id>]`` reference). The CSV/PDF activity stays. The
transaction is then marked as "existing" in the sync state, so it isn't
created again.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from .dedup import MAIN_TYPES, ExistingIndex
from .mapping import Accounts
from .wealthfolio import WealthfolioClient

REF = re.compile(r"\[SYNC ([\w-]+):([^\]]+)\]")
SECURITY_TYPES = ("BUY", "SELL", "DIVIDEND")


@dataclass
class Duplicate:
    tx_id: str
    sync: dict  # the sync's main activity
    original: dict  # the CSV/PDF activity it duplicates
    activity_ids: list[str] = field(default_factory=list)  # every activity of the sync's transaction


def _main(acts: list[dict]) -> dict:
    for a in acts:
        if a.get("activityType") in SECURITY_TYPES:
            return a
    for a in acts:
        if a.get("activityType") in MAIN_TYPES:
            return a
    return acts[0]


def _as_payload(a: dict) -> dict:
    """An existing activity in the shape ExistingIndex compares (a NewActivity payload)."""
    return {
        "accountId": a.get("accountId"),
        "activityType": a.get("activityType"),
        "subtype": a.get("subtype") or None,
        "activityDate": a.get("date"),
        "amount": a.get("amount"),
        "quantity": a.get("quantity"),
        "asset": {"symbol": a.get("assetSymbol") or a.get("assetId") or ""},
    }


def find(wf: WealthfolioClient, broker: str, accounts: Accounts) -> list[Duplicate]:
    activities = wf.search_activities([accounts.cash, accounts.portfolio], date(1970, 1, 1), date(2100, 1, 1))
    ours: dict[str, list[dict]] = {}
    others: list[dict] = []
    for a in activities:
        m = REF.search(a.get("comment") or "")
        if m and m.group(1) == broker:
            ours.setdefault(m.group(2), []).append(a)
        elif not m:
            others.append(a)
    index = ExistingIndex(others)
    out: list[Duplicate] = []
    for tx_id, acts in sorted(ours.items(), key=lambda kv: min(a.get("date", "") for a in kv[1])):
        main = _main(acts)
        if main.get("activityType") not in MAIN_TYPES:
            continue  # pure transfers (own accounts) have no CSV counterpart to compare with
        match_id = index.find(f"[SYNC {broker}:{tx_id}]", [_as_payload(main)])
        if match_id:
            original = next(o for o in others if o["id"] == match_id)
            out.append(Duplicate(tx_id, main, original, [a["id"] for a in acts]))
    return out


def remove(wf: WealthfolioClient, duplicates: list[Duplicate]) -> tuple[int, list[str]]:
    """Delete the sync's copies; returns (activities deleted, transaction ids done)."""
    deleted = 0
    done: list[str] = []
    for d in duplicates:
        # Transfer legs first, the trade last: an interruption leaves the trade,
        # which the next check finds again.
        ordered = sorted(d.activity_ids, key=lambda i: i == d.sync["id"])
        for activity_id in ordered:
            wf.delete_activity(activity_id)
            deleted += 1
        done.append(d.tx_id)
    return deleted, done
