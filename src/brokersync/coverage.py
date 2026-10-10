"""Compares what the broker lists with what is in Wealthfolio - after every run.

Wealthfolio, not the sync state, says whether a transaction is booked: a
transaction the sync booked (or found in a CSV/PDF import) counts as booked
only while its activities exist - found by their ``[SYNC <broker>:<id>]``
reference or by the ids the state stored. What the run finds:

- **missing**: booked before, but none of its activities is in Wealthfolio any
  more (deleted by hand, or the import activity it was matched to is gone).
- **partial**: some of the activities the sync created are gone (e.g. one
  transfer leg).
- **ignored**: the user decided not to book it ("Ignorieren").
- **orphan** (only for adapters that read the whole history, ``full_history``):
  an activity carrying the sync's reference whose transaction the broker no
  longer lists - cancelled at the broker, or netted out by a newer version.

Nothing is created or deleted here: a deletion in Wealthfolio may have been on
purpose (a duplicate of an import), so the user decides on the page *Prüfung*
whether a missing transaction is booked again or ignored, and deletes orphans
in Wealthfolio.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime

from .model import Kind, Transaction

SYNC_REF = re.compile(r"\[SYNC ([^:\]\s]+):([^\]]+)\]")
MISSING, PARTIAL, IGNORED, ORPHAN = "missing", "partial", "ignored", "orphan"


@dataclass(frozen=True)
class Gap:
    kind: str
    ref: str  # the transaction id, for an orphan the activity id
    occurred_at: str
    label: str = ""
    name: str = ""
    amount: str = ""
    currency: str = ""
    tx_id: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def _amount(tx: Transaction) -> str:
    return str(tx.signed if tx.signed is not None else tx.net)


def _tx_gap(kind: str, tx: Transaction) -> Gap:
    return Gap(kind, tx.id, tx.datetime.isoformat(), tx.label or tx.kind.value, tx.name, _amount(tx), tx.currency,
               tx.id)


def check(broker: str, transactions: list[Transaction], synced: dict[str, tuple[str, list[str]]],
          activities: list[dict], *, full_history: bool, since: datetime | None) -> list[Gap]:
    """``synced``: tx id → (status, activity ids) from the state; ``activities``: both accounts' activities."""
    by_id = {a["id"]: a for a in activities}
    tagged: dict[str, list[dict]] = defaultdict(list)
    for a in activities:
        m = SYNC_REF.search(a.get("comment") or "")
        if m and m.group(1) == broker:
            tagged[m.group(2)].append(a)

    gaps: list[Gap] = []
    listed: set[str] = set()
    for tx in transactions:
        listed.add(tx.id)
        row = synced.get(tx.id)
        if not row or tx.kind == Kind.UNKNOWN:
            continue  # new: the run books it
        status, ids = row
        if status == "ignored":
            gaps.append(_tx_gap(IGNORED, tx))
            continue
        ids = [i for i in ids if i]
        present = {a["id"] for a in tagged.get(tx.id, [])} | {i for i in ids if i in by_id}
        if not present:
            gaps.append(_tx_gap(MISSING, tx))
        elif status == "imported" and len(present) < len(ids):
            gaps.append(_tx_gap(PARTIAL, tx))

    if full_history:
        for tx_id, acts in tagged.items():
            if tx_id in listed:
                continue
            for a in acts:
                when = datetime.fromisoformat(str(a.get("date")).replace("Z", "+00:00"))
                if since and when < since:
                    continue  # before the start date: not fetched, so not comparable
                amount = a.get("amount") or ""
                gaps.append(Gap(ORPHAN, a["id"], when.isoformat(), a.get("activityType") or "",
                                a.get("assetSymbol") or "", str(amount), a.get("currency") or "", tx_id))
    return sorted(gaps, key=lambda g: g.occurred_at)
