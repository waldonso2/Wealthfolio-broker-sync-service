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

import logging
import re
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime

from .model import Kind, Transaction, when

log = logging.getLogger(__name__)

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
    gaps = [g for tx in transactions if (g := _gap(tx, synced.get(tx.id), tagged, by_id))]
    if full_history:
        listed = {tx.id for tx in transactions}
        gaps += [_orphan(a, tx_id, moment) for tx_id, acts in tagged.items() if tx_id not in listed for a in acts
                 if (moment := _moment(a)) is not None
                 and not (since and moment < since)]  # before the start date: not comparable
    return sorted(gaps, key=lambda g: g.occurred_at)


def _gap(tx: Transaction, row: tuple[str, list[str]] | None, tagged: dict[str, list[dict]],
         by_id: dict[str, dict]) -> Gap | None:
    """What a transaction synced before lacks in Wealthfolio, if anything."""
    if not row or tx.kind == Kind.UNKNOWN:
        return None  # new: the run books it
    status, ids = row
    if status == "ignored":
        return _tx_gap(IGNORED, tx)
    ids = [i for i in ids if i]
    present = {a["id"] for a in tagged.get(tx.id, [])} | {i for i in ids if i in by_id}
    if not present:
        return _tx_gap(MISSING, tx)
    if status == "imported" and len(present) < len(ids):
        return _tx_gap(PARTIAL, tx)
    return None


def _moment(a: dict) -> datetime | None:
    """The activity's moment; None (and a log line) for one without a readable date (#134 M2): one odd activity
    must not stop the whole comparison."""
    try:
        return when(a.get("date") or "")
    except ValueError:
        log.warning("activity %s without a readable date skipped", a.get("id"))
        return None


def _orphan(a: dict, tx_id: str, moment: datetime) -> Gap:
    """An activity of the sync whose transaction the broker no longer lists."""
    return Gap(ORPHAN, a["id"], moment.isoformat(), a.get("activityType") or "",
               a.get("assetSymbol") or "", str(a.get("amount") or ""), a.get("currency") or "", tx_id)
