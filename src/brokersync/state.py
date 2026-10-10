"""Sync state in SQLite (``<data>/state.db``, encrypted with SQLCipher, AES-256,
key from the data tier of ``brokersync.crypto``): which broker transactions the
sync booked (and the activity ids), which the user ignores, the run history,
the unknown events and the last comparison with Wealthfolio.

``synced`` decides only what is *new*: a transaction listed there is not booked
automatically again. Whether it is still in Wealthfolio is checked against
Wealthfolio after every run (``brokersync.coverage``).
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from . import crypto

try:  # SQLCipher: wheels for x86-64; elsewhere the database stays plain SQLite (shown on the page Sicherheit)
    import sqlcipher3 as sqlcipher
except ImportError:  # pragma: no cover - depends on the platform
    sqlcipher = None

ENCRYPTED = sqlcipher is not None
PLAIN_HEADER = b"SQLite format 3\x00"

SCHEMA = """
CREATE TABLE IF NOT EXISTS synced (
  broker TEXT NOT NULL,
  tx_id TEXT NOT NULL,
  -- imported: activities created by the sync; existing: already in Wealthfolio
  -- (CSV/PDF import or an earlier sync whose state was lost); ignored: the user
  -- chose not to book it
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
-- The Wealthfolio asset a broker's ISIN is booked under (learned from the
-- activities of its trades and dividends): the addon books under the ticker
-- the user mapped, which Wealthfolio's holdings don't link to an ISIN.
CREATE TABLE IF NOT EXISTS assets (
  broker TEXT NOT NULL,
  isin TEXT NOT NULL,
  asset_id TEXT NOT NULL,
  symbol TEXT NOT NULL,
  exchange_mic TEXT,
  name TEXT,
  PRIMARY KEY (broker, isin)
);
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
-- Positions the user books as bought on a day (held before the start date):
-- the sync books each as a buy with a deposit of its cost (page Prüfung).
CREATE TABLE IF NOT EXISTS openings (
  broker TEXT NOT NULL,
  isin TEXT NOT NULL,
  day TEXT NOT NULL,
  name TEXT NOT NULL,
  shares TEXT NOT NULL,
  price TEXT NOT NULL,
  fee TEXT NOT NULL,
  currency TEXT NOT NULL,
  PRIMARY KEY (broker, isin, day)
);
-- What the last run found comparing the broker with Wealthfolio (brokersync.coverage).
CREATE TABLE IF NOT EXISTS gaps (
  broker TEXT NOT NULL,
  kind TEXT NOT NULL,
  ref TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  payload TEXT NOT NULL,
  PRIMARY KEY (broker, kind, ref)
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


def db_key(data_dir: Path, key: bytes | None = None) -> bytes:
    return crypto.subkey(key or crypto.data_key(data_dir), "state db")


def _pragma_key(key: bytes) -> str:
    return f"\"x'{key.hex()}'\""


def _connect(path: Path, key: bytes):
    if sqlcipher is None:
        return sqlite3.connect(path, timeout=30, check_same_thread=False)
    db = sqlcipher.connect(str(path), timeout=30, check_same_thread=False)
    db.execute(f"PRAGMA key = {_pragma_key(key)}")
    return db


def is_plain(path: Path) -> bool:
    if not path.exists() or path.stat().st_size == 0:
        return False
    with open(path, "rb") as f:
        return f.read(16) == PLAIN_HEADER


def encrypt_plain(path: Path, key: bytes) -> None:
    """A plain database (before 0.9.0) → SQLCipher, in place."""
    tmp = path.with_name(path.name + ".enc-tmp")
    tmp.unlink(missing_ok=True)
    db = sqlcipher.connect(str(path))
    try:
        db.execute(f"ATTACH DATABASE '{tmp}' AS enc KEY {_pragma_key(key)}")
        db.execute("SELECT sqlcipher_export('enc')")
        db.execute("DETACH DATABASE enc")
    finally:
        db.close()
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    for suffix in ("-journal", "-wal", "-shm"):
        path.with_name(path.name + suffix).unlink(missing_ok=True)


def rekey(data_dir: Path, old: bytes, new: bytes) -> None:
    """Re-encrypt the database for a new data key (passphrase set, changed or removed)."""
    if sqlcipher is None:
        return
    path = Path(data_dir) / "state.db"
    if not path.exists():
        return
    db = _connect(path, db_key(data_dir, old))
    try:
        db.execute(f"PRAGMA rekey = {_pragma_key(db_key(data_dir, new))}")
    finally:
        db.close()


class State:
    def __init__(self, data_dir: Path, key: bytes | None = None):
        Path(data_dir).mkdir(mode=0o700, parents=True, exist_ok=True)
        path = Path(data_dir) / "state.db"
        k = db_key(data_dir, key) if sqlcipher is not None else b""
        if sqlcipher is not None and is_plain(path):
            with crypto.file_lock(data_dir, "state.lock"):
                if is_plain(path):
                    encrypt_plain(path, k)
        self.db = _connect(path, k)
        self.db.executescript(SCHEMA)
        if path.exists():
            os.chmod(path, 0o600)

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

    def synced(self, broker: str) -> dict[str, tuple[str, list[str]]]:
        rows = self.db.execute("SELECT tx_id, status, activity_ids FROM synced WHERE broker = ?", (broker,))
        return {t: (s, json.loads(ids)) for t, s, ids in rows}

    def forget(self, broker: str, tx_ids: list[str]) -> None:
        """The next run treats these transactions as new: books them unless they are in Wealthfolio."""
        with self.db:
            self.db.executemany("DELETE FROM synced WHERE broker = ? AND tx_id = ?", [(broker, t) for t in tx_ids])
            self.db.executemany("DELETE FROM gaps WHERE broker = ? AND ref = ? AND kind != 'orphan'",
                                [(broker, t) for t in tx_ids])

    def ignore(self, broker: str, tx_ids: list[str]) -> None:
        with self.db:
            for t in tx_ids:
                self.db.execute("INSERT OR REPLACE INTO synced VALUES (?, ?, 'ignored', '[]', ?)", (broker, t, now()))
                self.db.execute("UPDATE gaps SET kind = 'ignored' WHERE broker = ? AND ref = ? AND kind != 'orphan'",
                                (broker, t))

    # ── opening positions ───────────────────────────────────────────────────
    def add_opening(self, broker: str, isin: str, day: str, name: str, shares: str, price: str, fee: str,
                    currency: str = "EUR") -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO openings VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            (broker, isin, day, name, shares, price, fee, currency))

    def delete_opening(self, broker: str, isin: str, day: str) -> None:
        with self.db:
            self.db.execute("DELETE FROM openings WHERE broker = ? AND isin = ? AND day = ?", (broker, isin, day))

    def openings(self, broker: str) -> list[dict]:
        rows = self.db.execute("SELECT isin, day, name, shares, price, fee, currency FROM openings WHERE broker = ? "
                               "ORDER BY day, isin", (broker,))
        return [{"isin": i, "day": d, "name": n, "shares": s, "price": p, "fee": f, "currency": c}
                for i, d, n, s, p, f, c in rows]

    def set_gaps(self, broker: str, gaps: list[dict]) -> None:
        with self.db:
            self.db.execute("DELETE FROM gaps WHERE broker = ?", (broker,))
            self.db.executemany("INSERT OR REPLACE INTO gaps VALUES (?, ?, ?, ?, ?)",
                                [(broker, g["kind"], g["ref"], g["occurred_at"], json.dumps(g)) for g in gaps])

    def gaps(self, broker: str) -> list[dict]:
        rows = self.db.execute("SELECT kind, payload FROM gaps WHERE broker = ? ORDER BY occurred_at", (broker,))
        return [{**json.loads(p), "kind": k} for k, p in rows]

    def activity_ids(self, broker: str, tx_id: str) -> list[str]:
        row = self.db.execute("SELECT activity_ids FROM synced WHERE broker = ? AND tx_id = ?",
                              (broker, tx_id)).fetchone()
        return json.loads(row[0]) if row else []

    # ── assets per ISIN ─────────────────────────────────────────────────────
    def assets(self, broker: str) -> dict[str, dict]:
        rows = self.db.execute("SELECT isin, asset_id, symbol, exchange_mic, name FROM assets WHERE broker = ?",
                               (broker,))
        return {i: {"asset_id": a, "symbol": s, "exchange_mic": m, "name": n} for i, a, s, m, n in rows}

    def set_asset(self, broker: str, isin: str, asset_id: str, symbol: str, exchange_mic: str | None,
                  name: str | None) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO assets VALUES (?, ?, ?, ?, ?, ?)",
                            (broker, isin, asset_id, symbol, exchange_mic, name))

    def flag(self, key: str) -> bool:
        return self.db.execute("SELECT 1 FROM meta WHERE key = ?", (key,)).fetchone() is not None

    def set_flag(self, key: str) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, now()))

    def meta(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))

    def delete_meta(self, key: str) -> None:
        with self.db:
            self.db.execute("DELETE FROM meta WHERE key = ?", (key,))

    def forget_broker(self, broker: str) -> int:
        """Remove everything stored for a broker; returns how many activities its sync created."""
        created = sum(len(json.loads(ids)) for (ids,) in self.db.execute(
            "SELECT activity_ids FROM synced WHERE broker = ? AND status = 'imported'", (broker,)))
        with self.db:
            for table in ("synced", "runs", "reconcile", "balances", "assets", "unknown_events", "gaps", "openings"):
                self.db.execute(f"DELETE FROM {table} WHERE broker = ?", (broker,))
            self.db.execute("DELETE FROM meta WHERE key LIKE ?", (f"%:{broker}",))
        return created

    def reset_broker(self, broker: str) -> None:
        """Forget what was synced for a broker (brokersync.reset): the next run starts from the start date.

        Keeps what the user entered (opening positions) and the learned assets.
        """
        with self.db:
            for table in ("synced", "runs", "reconcile", "balances", "unknown_events", "gaps"):
                self.db.execute(f"DELETE FROM {table} WHERE broker = ?", (broker,))
            self.db.execute("DELETE FROM meta WHERE key LIKE ?", (f"%:{broker}",))

    # ── unknown events ──────────────────────────────────────────────────────
    def add_unknown(self, broker: str, tx_id: str, raw_type: str, occurred_at: str, payload: dict) -> bool:
        """Store an unknown event; True if it wasn't known yet (so it gets reported once).

        A known one gets the current payload (a newer version may show more).
        """
        data = json.dumps(payload, default=str)
        with self.db:
            cur = self.db.execute(
                "INSERT OR IGNORE INTO unknown_events VALUES (?, ?, ?, ?, ?, ?)",
                (broker, tx_id, raw_type, occurred_at, data, now()),
            )
            if cur.rowcount == 1:
                return True
            self.db.execute("UPDATE unknown_events SET payload = ? WHERE broker = ? AND tx_id = ?",
                            (data, broker, tx_id))
        return False

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

    def clear_reconcile(self, broker: str) -> None:
        """Forget the last check (out of date, e.g. after removing duplicates); the next run recomputes it."""
        with self.db:
            self.db.execute("DELETE FROM reconcile WHERE broker = ?", (broker,))

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
