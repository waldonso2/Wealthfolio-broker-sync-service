"""Sync state in SQLite (``<data>/state.db``): which broker transactions are
already in Wealthfolio, the run history and the unknown events.

This is the first line of deduplication; Wealthfolio's own fingerprint and the
check against existing activities (``brokersync.dedup``) are the others.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS synced (
  broker TEXT NOT NULL,
  tx_id TEXT NOT NULL,
  -- imported: activities created by the sync; existing: already in Wealthfolio
  -- (CSV/PDF import or an earlier sync whose state was lost)
  status TEXT NOT NULL,
  activity_ids TEXT NOT NULL DEFAULT '[]',
  synced_at TEXT NOT NULL,
  PRIMARY KEY (broker, tx_id)
);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  broker TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  -- running | ok | needs_auth | error | aborted
  status TEXT NOT NULL,
  created INTEGER NOT NULL DEFAULT 0,
  existing INTEGER NOT NULL DEFAULT 0,
  failed INTEGER NOT NULL DEFAULT 0,
  unknown INTEGER NOT NULL DEFAULT 0,
  message TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS reconcile (
  broker TEXT PRIMARY KEY,
  deviations TEXT NOT NULL,
  notified TEXT NOT NULL DEFAULT '[]',
  at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS balances (
  broker TEXT NOT NULL,
  currency TEXT NOT NULL,
  amount TEXT NOT NULL,
  at TEXT NOT NULL,
  PRIMARY KEY (broker, currency)
);
CREATE TABLE IF NOT EXISTS unknown_events (
  broker TEXT NOT NULL,
  tx_id TEXT NOT NULL,
  raw_type TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  payload TEXT NOT NULL,
  first_seen TEXT NOT NULL,
  PRIMARY KEY (broker, tx_id)
);
"""


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass(frozen=True)
class Run:
    id: int
    broker: str
    started_at: str
    finished_at: str | None
    status: str
    created: int
    existing: int
    failed: int
    unknown: int
    message: str


class State:
    def __init__(self, data_dir: Path):
        Path(data_dir).mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(Path(data_dir) / "state.db", timeout=30, check_same_thread=False)
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    # ── transactions ────────────────────────────────────────────────────────
    def known(self, broker: str) -> set[str]:
        rows = self.db.execute("SELECT tx_id FROM synced WHERE broker = ?", (broker,))
        return {r[0] for r in rows}

    def mark(self, broker: str, tx_id: str, status: str, activity_ids: list[str]) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO synced VALUES (?, ?, ?, ?, ?)",
                (broker, tx_id, status, json.dumps(activity_ids), now()),
            )

    # ── unknown events ──────────────────────────────────────────────────────
    def add_unknown(self, broker: str, tx_id: str, raw_type: str, occurred_at: str, payload: dict) -> bool:
        """Store an unknown event; True if it wasn't known yet (so it gets reported once)."""
        with self.db:
            cur = self.db.execute(
                "INSERT OR IGNORE INTO unknown_events VALUES (?, ?, ?, ?, ?, ?)",
                (broker, tx_id, raw_type, occurred_at, json.dumps(payload, default=str), now()),
            )
        return cur.rowcount == 1

    def oldest_open(self, broker: str, raw_type: str) -> datetime | None:
        """When the oldest still open event of this type happened (e.g. an unmatched settlement)."""
        row = self.db.execute("SELECT MIN(occurred_at) FROM unknown_events WHERE broker = ? AND raw_type = ?",
                              (broker, raw_type)).fetchone()
        return datetime.fromisoformat(row[0]) if row and row[0] else None

    def resolve_unknown(self, broker: str, tx_id: str) -> None:
        with self.db:
            self.db.execute("DELETE FROM unknown_events WHERE broker = ? AND tx_id = ?", (broker, tx_id))

    # ── balances ────────────────────────────────────────────────────────────
    def set_balances(self, broker: str, balances: list[tuple[str, str]]) -> None:
        with self.db:
            self.db.execute("DELETE FROM balances WHERE broker = ?", (broker,))
            self.db.executemany("INSERT INTO balances VALUES (?, ?, ?, ?)",
                                [(broker, c, a, now()) for c, a in balances])

    def set_reconcile(self, broker: str, deviations: list[dict], notified: list[dict] | None = None) -> None:
        old = self.reconcile(broker)
        keep = notified if notified is not None else (old or {}).get("notified", [])
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO reconcile VALUES (?, ?, ?, ?)",
                            (broker, json.dumps(deviations), json.dumps(keep), now()))

    def reconcile(self, broker: str) -> dict | None:
        row = self.db.execute("SELECT deviations, notified, at FROM reconcile WHERE broker = ?", (broker,)).fetchone()
        if not row:
            return None
        return {"deviations": json.loads(row[0]), "notified": json.loads(row[1]), "at": row[2]}

    def balances(self, broker: str) -> list[dict]:
        rows = self.db.execute("SELECT currency, amount, at FROM balances WHERE broker = ? ORDER BY currency",
                               (broker,))
        return [{"currency": c, "amount": a, "at": t} for c, a, t in rows]

    def unknown_events(self, broker: str | None = None) -> list[dict]:
        sql = "SELECT broker, tx_id, raw_type, occurred_at, payload, first_seen FROM unknown_events"
        args: tuple = ()
        if broker:
            sql += " WHERE broker = ?"
            args = (broker,)
        rows = self.db.execute(sql + " ORDER BY occurred_at DESC", args)
        return [
            {"broker": b, "tx_id": t, "raw_type": r, "occurred_at": o, "payload": json.loads(p), "first_seen": f}
            for b, t, r, o, p, f in rows
        ]

    # ── runs ────────────────────────────────────────────────────────────────
    def start_run(self, broker: str) -> int:
        with self.db:
            cur = self.db.execute(
                "INSERT INTO runs (broker, started_at, status) VALUES (?, ?, 'running')", (broker, now())
            )
        return int(cur.lastrowid)

    def abort_stale_runs(self) -> int:
        """Close runs left at 'running' by a process that died (restart, update, crash).

        Only call while holding the run lock: then no run can really be in progress.
        """
        with self.db:
            cur = self.db.execute(
                "UPDATE runs SET status = 'aborted', finished_at = ?, message = ? WHERE status = 'running'",
                (now(), "Abgebrochen - der Dienst wurde während des Abrufs beendet (Neustart, Update oder Absturz)."),
            )
        return cur.rowcount

    def finish_run(self, run_id: int, status: str, *, created: int = 0, existing: int = 0, failed: int = 0,
                   unknown: int = 0, message: str = "") -> None:
        with self.db:
            self.db.execute(
                "UPDATE runs SET finished_at = ?, status = ?, created = ?, existing = ?, failed = ?, unknown = ?,"
                " message = ? WHERE id = ?",
                (now(), status, created, existing, failed, unknown, message, run_id),
            )

    def runs(self, broker: str | None = None, limit: int = 20) -> list[Run]:
        sql = "SELECT * FROM runs"
        args: tuple = ()
        if broker:
            sql += " WHERE broker = ?"
            args = (broker,)
        rows = self.db.execute(sql + " ORDER BY id DESC LIMIT ?", (*args, limit))
        return [Run(*r) for r in rows]

    def last_run(self, broker: str) -> Run | None:
        r = self.runs(broker, 1)
        return r[0] if r else None

    def last_success(self, broker: str) -> datetime | None:
        row = self.db.execute(
            "SELECT started_at FROM runs WHERE broker = ? AND status = 'ok' ORDER BY id DESC LIMIT 1", (broker,)
        ).fetchone()
        return datetime.fromisoformat(row[0]) if row else None
