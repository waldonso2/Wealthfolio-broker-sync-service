"""One sync run: every enabled broker → Wealthfolio.

Per broker: log in (stored session), fetch the transactions - the whole
history for adapters that can read it cheaply (``full_history``), else since
the last successful run (minus an overlap) -, skip the ones already synced,
check the rest against Wealthfolio's existing activities, create what is
missing. Then compare the ones synced before with Wealthfolio
(``brokersync.coverage``): deleted ones and activities the broker no longer
lists are shown on the page *Prüfung* and reported, never re-created or
deleted without the user. A broker that fails is reported and the others still
run (AC 9). Unknown event types are stored and reported once, never dropped
(AC 10).

Only one run at a time: the web UI and the systemd timer share a lock file.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from . import assets as assets_mod
from . import config as config_mod
from . import coverage, retired
from . import repair as repair_mod
from .adapters import ADAPTERS, AdapterError, AuthRequired, BrokerAdapter
from .dedup import ExistingIndex
from .mapping import Accounts, MappingError, SecurityMapping, TransferPattern, to_activities, tx_ref
from .model import CashBalance, Kind, Position, Transaction
from .notify import Notifier
from .reconcile import compare
from .state import State
from .vault import Vault
from .wealthfolio import Duplicate, WealthfolioClient, WealthfolioError

log = logging.getLogger(__name__)
BERLIN = ZoneInfo("Europe/Berlin")

# Re-fetch this much before the last successful run: brokers book late.
OVERLAP = timedelta(days=7)
# A bank's securities settlement lies at most this many days from the trade.
SETTLEMENT_DAYS = 6
UNMATCHED_SECURITIES = "WERTPAPIER_OHNE_GEGENSTUECK"


def tax_flag(key: str) -> str:
    return f"dividend-tax:{key}"


def opening_id(isin: str, day: str) -> str:
    return f"start-{isin}-{day}"


def refetch_flag(key: str) -> str:
    return f"refetch:{key}"


class AlreadyRunning(Exception):
    pass


@dataclass
class BrokerResult:
    broker: str
    status: str  # ok | needs_auth | error | skipped
    created: int = 0
    existing: int = 0
    failed: int = 0
    unknown: int = 0
    messages: list[str] = field(default_factory=list)


@contextmanager
def run_lock(data_dir: Path, wait: float = 0) -> Iterator[None]:
    """The sync lock, shared by the web UI and the timer; waits up to ``wait`` seconds."""
    Path(data_dir).mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + wait
    with open(Path(data_dir) / "sync.lock", "a") as f:
        while True:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as e:
                if time.monotonic() >= deadline:
                    raise AlreadyRunning("A sync is already running.") from e
                time.sleep(0.1)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def is_running(data_dir: Path) -> bool:
    """Whether a sync holds the lock - in this process or another (the timer)."""
    try:
        with run_lock(data_dir):
            return False
    except AlreadyRunning:
        return True


class Syncer:
    def __init__(self, data_dir: Path, *, wealthfolio: Callable[[str, str | None], WealthfolioClient] | None = None,
                 notifier: Notifier | None = None, adapters: dict[str, type[BrokerAdapter]] | None = None):
        self.data_dir = Path(data_dir)
        self.vault = Vault(self.data_dir)
        self.state = State(self.data_dir)
        retired.clean_up(self.data_dir, self.state, self.vault)
        self.adapters = adapters or ADAPTERS
        self._wealthfolio = wealthfolio or (lambda url, pw: WealthfolioClient(url, pw))
        # Seconds to give Wealthfolio to recalculate holdings before the check.
        self.recalc_wait = float(os.environ.get("BROKERSYNC_RECALC_WAIT", "5"))
        self._notifier = notifier

    def notifier(self, cfg: config_mod.Config) -> Notifier:
        if self._notifier:
            return self._notifier
        return Notifier(cfg.ntfy_server, cfg.ntfy_topic, self.vault.load().get("ntfy_token"))

    def link(self, cfg: config_mod.Config, path: str) -> str | None:
        return f"{cfg.public_url.rstrip('/')}{path}" if cfg.public_url else None

    def run(self, only: list[str] | None = None) -> list[BrokerResult]:
        # Waits a moment: the status page holds the lock briefly for its checks.
        with run_lock(self.data_dir, wait=5):
            self.state.abort_stale_runs()
            cfg = config_mod.load(self.data_dir)
            results = []
            for key, bcfg in cfg.brokers.items():
                if only and key not in only:
                    continue
                if key not in self.adapters:
                    continue
                # "Automatisch abrufen" off: the timer and the general button skip
                # the broker, a request for exactly this broker still runs it.
                if not bcfg.enabled and not (only and key in only):
                    continue
                results.append(self._run_broker(cfg, key))
            return results

    def cleanup(self) -> None:
        """Close runs a dead process left at 'running', unless a sync is in progress."""
        try:
            with run_lock(self.data_dir):
                self.state.abort_stale_runs()
        except AlreadyRunning:
            pass

    def _run_broker(self, cfg: config_mod.Config, key: str) -> BrokerResult:
        run_id = self.state.start_run(key)
        result = BrokerResult(key, "ok")
        label = self.adapters[key].label
        notifier = self.notifier(cfg)
        try:
            self._sync_broker(cfg, key, result)
        except AuthRequired as e:
            result.status = "needs_auth"
            result.messages.append(str(e))
            notifier.send(f"{label}: Anmeldung nötig", f"{e} Öffne die Seite, um dich anzumelden.",
                          link=self.link(cfg, f"/brokers/{key}/login"), priority="high", tags="key")
        except (AdapterError, WealthfolioError) as e:
            result.status = "error"
            result.messages.append(str(e))
            notifier.send(f"{label}: Abruf fehlgeschlagen", str(e), link=self.link(cfg, "/"), priority="high",
                          tags="warning")
        except Exception as e:  # a broken adapter must not stop the others
            log.exception("sync of %s crashed", key)
            result.status = "error"
            result.messages.append(f"Unexpected error: {e}")
            notifier.send(f"{label}: Abruf fehlgeschlagen", f"Unerwarteter Fehler: {e}", link=self.link(cfg, "/"),
                          priority="high", tags="warning")
        if result.status == "ok" and result.failed:
            result.status = "error"
            notifier.send(f"{label}: {result.failed} Buchungen nicht übernommen", "\n".join(result.messages[:5]),
                          link=self.link(cfg, "/"), tags="warning")
        self.state.finish_run(run_id, result.status, created=result.created, existing=result.existing,
                              failed=result.failed, unknown=result.unknown, message="\n".join(result.messages))
        log.info("%s: %s, %d created, %d already there, %d failed, %d unknown", key, result.status,
                 result.created, result.existing, result.failed, result.unknown)
        if result.status != "ok" and result.messages:
            log.warning("%s: %s", key, " | ".join(result.messages[:5]))
        return result

    def _sync_broker(self, cfg: config_mod.Config, key: str, result: BrokerResult) -> None:
        bcfg = cfg.brokers[key]
        if not bcfg.cash_account_id or not bcfg.portfolio_account_id:
            raise AdapterError("No Wealthfolio accounts assigned - finish the setup for this broker.")
        secrets = self.vault.load()
        stored = secrets.get("brokers", {}).get(key, {})
        adapter_cls = self.adapters[key]
        adapter = adapter_cls(stored.get("credentials", {}), stored.get("session"))
        label = adapter_cls.label
        adapter.on_user_action = lambda message: self.notifier(cfg).send(
            f"{label}: Bestätigung nötig", message, link=self.link(cfg, "/"), priority="high", tags="key")
        # Once, for brokers with positions: the whole history, to learn the
        # Wealthfolio asset of every ISIN (brokersync.assets) for the check.
        backfill = adapter_cls.reports_positions and not self.state.flag(f"assets-learned:{key}")
        full = adapter_cls.full_history
        # "Ab Startdatum neu abrufen" in the broker settings: once from the start date again.
        refetch = not full and self.state.flag(refetch_flag(key))
        since = self._start(bcfg) if full or backfill or refetch else self._since(key, bcfg)
        # Before this run books anything: what the comparison with Wealthfolio checks.
        synced = self.state.synced(key)
        try:
            adapter.login()
            # Once per broker: details also for transactions found in an import, for the tax of their
            # dividends (Scalable's CSV export has none) - see _add_dividend_tax.
            tax_backfill = not self.state.flag(tax_flag(key))
            adapter.known_ids = {t for t, (status, _) in synced.items() if not (tax_backfill and status == "existing")}
            transactions = adapter.get_transactions(since) + self._openings(key)
            cash = self._broker_cash(key, adapter)
            positions = self._broker_positions(key, adapter) if adapter_cls.reports_positions else None
        finally:
            # Keep whatever session the adapter has, even half-way through a login.
            adapter.close()
            self.vault.set_broker_session(key, adapter.session_state())

        todo = [t for t in transactions if t.id not in synced]
        log.info("%s: %d transactions from the broker since %s, %d not synced yet", key, len(transactions),
                 since.date().isoformat() if since else "the beginning", len(todo))
        self._report_unknown(cfg, key, [t for t in todo if t.kind == Kind.UNKNOWN], result)
        todo = [t for t in todo if t.kind != Kind.UNKNOWN]

        accounts = Accounts(bcfg.cash_account_id, bcfg.portfolio_account_id)
        # The user's mappings win over the assets learned from earlier activities.
        mappings = assets_mod.mappings(self.state, key)
        mappings.update({isin: SecurityMapping(m.get("symbol") or isin, m.get("exchangeMic"), m.get("name"))
                         for isin, m in cfg.security_mappings.items()})
        patterns = [TransferPattern.from_config(p) for p in cfg.transfer_patterns]
        with self._wealthfolio(cfg.wealthfolio_url, secrets.get("wealthfolio_password")) as wf:
            # Before the check against existing activities: it then sees them repaired.
            repaired = self._repair_cash_assets(key, wf, accounts)
            unmatched: list[Transaction] = []
            activities = self._activities(wf, accounts, transactions, since if full else None, full)
            if todo:
                existing = ExistingIndex(activities)
                for tx in sorted(todo, key=lambda t: t.datetime):
                    if tx.kind == Kind.SECURITIES_CASH:
                        if not self._settle_securities_cash(key, tx, accounts, existing, result):
                            unmatched.append(tx)
                        continue
                    self._sync_tx(wf, key, tx, accounts, mappings, patterns, existing, result)
            self._compare(cfg, key, transactions, synced, activities, full, since)
            if tax_backfill:
                by_id = {a["id"]: a for a in activities}
                added, tax_errors = 0, 0
                for tx in transactions:
                    status, ids = synced.get(tx.id, ("", []))
                    if status == "existing" and ids and ids[0] in by_id:
                        try:
                            added += self._add_dividend_tax(wf, tx, by_id[ids[0]])
                        except WealthfolioError as e:
                            tax_errors += 1
                            log.info("%s: tax not added to %s: %s", key, tx.id, e)
                if added:
                    log.info("%s: added the tax to %d imported dividends", key, added)
                if not tax_errors and not result.failed and not adapter.incomplete:
                    self.state.set_flag(tax_flag(key))
            try:
                assets_mod.learn(self.state, key, transactions, wf, accounts)
                if backfill:
                    self.state.set_flag(f"assets-learned:{key}")
            except WealthfolioError as e:
                log.info("%s: assets not learned: %s", key, e)
            if cash is not None and not result.failed:
                if result.created or repaired:
                    time.sleep(self.recalc_wait)  # Wealthfolio recalculates holdings in the background
                self._reconcile(cfg, key, wf, accounts, cash, positions, mappings)
        self._report_unmatched(cfg, key, unmatched, result)
        if full:
            self._resolve_vanished_unknowns(key, transactions, since)
        if refetch and not result.failed:
            self.state.delete_meta(refetch_flag(key))

    @staticmethod
    def _activities(wf: WealthfolioClient, accounts: Accounts, transactions: list[Transaction],
                    since: datetime | None, full: bool) -> list[dict]:
        """Both accounts' activities around the fetched transactions - for a full history all of them."""
        # Wide enough for a settlement a few days after the trade (SECURITIES_CASH).
        margin = timedelta(days=SETTLEMENT_DAYS + 1)
        if full:
            first = (since - margin).date() if since else date(1970, 1, 1)
            last = date(2100, 1, 1)
        elif transactions:
            first = (min(t.datetime for t in transactions) - margin).date()
            last = (max(t.datetime for t in transactions) + margin).date()
        else:
            return []
        return wf.search_activities([accounts.cash, accounts.portfolio], first, last)

    def _compare(self, cfg: config_mod.Config, key: str, transactions: list[Transaction],
                 synced: dict[str, tuple[str, list[str]]], activities: list[dict], full: bool,
                 since: datetime | None) -> None:
        """Transactions synced before that are gone from Wealthfolio, and the reverse (brokersync.coverage)."""
        gaps = coverage.check(key, transactions, synced, activities, full_history=full, since=since)
        before = {(g["kind"], g["ref"]) for g in self.state.gaps(key)}
        self.state.set_gaps(key, [g.as_dict() for g in gaps])
        new = [g for g in gaps if g.kind != coverage.IGNORED and (g.kind, g.ref) not in before]
        if not new:
            return
        log.info("%s: %d transactions missing in Wealthfolio, %d activities no longer at the broker", key,
                 sum(g.kind != coverage.ORPHAN for g in new), sum(g.kind == coverage.ORPHAN for g in new))
        label = self.adapters[key].label
        lines = []
        missing = [g for g in new if g.kind != coverage.ORPHAN]
        orphans = [g for g in new if g.kind == coverage.ORPHAN]
        if missing:
            lines.append(f"{len(missing)} schon übernommene Buchung(en) fehlen in Wealthfolio (gelöscht?).")
        if orphans:
            lines.append(f"{len(orphans)} Buchung(en) in Wealthfolio gibt es bei {label} nicht (mehr), "
                         "z. B. storniert.")
        self.notifier(cfg).send(f"{label}: Abweichung zu Wealthfolio",
                                " ".join(lines) + " Auf der Seite Prüfung entscheidest du, was passiert.",
                                link=self.link(cfg, "/check"), tags="mag")

    def _resolve_vanished_unknowns(self, key: str, transactions: list[Transaction], since: datetime | None) -> None:
        """A full history is the whole truth: an unknown event the broker no longer lists as one is settled
        (e.g. a cancellation a newer version nets out with its original)."""
        still = {t.id for t in transactions if t.kind == Kind.UNKNOWN}
        for e in self.state.unknown_events(key):
            if e["raw_type"] == UNMATCHED_SECURITIES or e["tx_id"] in still:
                continue
            if since and datetime.fromisoformat(e["occurred_at"]) < since:
                continue
            self.state.resolve_unknown(key, e["tx_id"])

    @staticmethod
    def _add_dividend_tax(wf: WealthfolioClient, tx: Transaction, activity: dict) -> int:
        """Put the broker's tax on a dividend an import booked without it (Scalable's CSV has none).

        Only the ``tax`` field changes: amount (net) and comment stay, and tax is not part of
        Wealthfolio's duplicate fingerprint, so a re-import still finds the activity. The
        sync's own activities and dividends that carry a tax already are left alone.
        """
        if tx.kind != Kind.DIVIDEND or tx.tax <= 0 or activity.get("activityType") != "DIVIDEND":
            return 0
        if "[SYNC " in (activity.get("comment") or ""):
            return 0
        try:
            if Decimal(str(activity.get("tax") or 0)) != 0:
                return 0
        except ArithmeticError:
            return 0
        payload = repair_mod.payload(activity)
        payload.pop("asset", None)  # keep the asset
        payload["tax"] = format(tx.tax.normalize(), "f")
        wf.update_activity(payload)
        return 1

    def _repair_cash_assets(self, key: str, wf: WealthfolioClient, accounts: Accounts) -> int:
        """Once per broker: the cash activities older versions created with a $CASH asset (brokersync.repair).

        Until they are repaired nothing is booked: the sync would re-send a
        transaction of theirs without the asset, which Wealthfolio doesn't
        recognise as the same activity.
        """
        flag = f"cash-assets-repaired:{key}"
        if self.state.flag(flag):
            return 0
        updated, errors = repair_mod.repair(wf, key, accounts)
        if errors:
            raise WealthfolioError(
                f"{len(errors)} Überträge aus früheren Versionen konnten nicht korrigiert werden (nichts gebucht, "
                f"der nächste Abruf versucht es erneut): " + "; ".join(errors[:3]))
        self.state.set_flag(flag)
        return updated

    def _broker_cash(self, key: str, adapter: BrokerAdapter) -> list[CashBalance] | None:
        # The broker's balance, shown next to the run and compared with
        # Wealthfolio's. Not worth failing the run for.
        try:
            balances = adapter.get_cash()
        except (AdapterError, AuthRequired, NotImplementedError) as e:
            log.info("%s: no balance: %s", key, e)
            return None
        self.state.set_balances(key, [(b.currency, str(b.amount)) for b in balances])
        return balances

    def _broker_positions(self, key: str, adapter: BrokerAdapter) -> list[Position] | None:
        try:
            positions = adapter.get_positions()
        except (AdapterError, AuthRequired, NotImplementedError) as e:
            log.info("%s: no positions: %s", key, e)
            return None
        # For the page Prüfung: what a position missing in Wealthfolio is pre-filled with.
        self.state.set_meta(f"positions:{key}", json.dumps(
            [{"isin": p.isin, "name": p.name, "shares": str(p.shares), "currency": p.currency,
              "cost": str(p.cost) if p.cost is not None else ""} for p in positions]))
        return positions

    def _openings(self, key: str) -> list[Transaction]:
        """Opening positions the user entered (page Prüfung): a buy on that day, its cost deposited first."""
        out = []
        for o in self.state.openings(key):
            shares, price, fee = Decimal(o["shares"]), Decimal(o["price"]), Decimal(o["fee"])
            day = date.fromisoformat(o["day"])
            out.append(Transaction(
                id=opening_id(o["isin"], o["day"]), kind=Kind.BUY,
                datetime=datetime(day.year, day.month, day.day, 12, tzinfo=BERLIN).astimezone(UTC),
                currency=o["currency"], net=shares * price + fee, isin=o["isin"], name=o["name"], shares=shares,
                gross=shares * price, fee=fee, label="Anfangsbestand", external_cash=True))
        return out

    def _reconcile(self, cfg: config_mod.Config, key: str, wf: WealthfolioClient, accounts: Accounts,
                   cash: list[CashBalance], positions: list[Position] | None,
                   mappings: dict[str, SecurityMapping]) -> None:
        try:
            wf_cash = wf.holdings(accounts.cash)
            wf_portfolio = wf.holdings(accounts.portfolio)
        except WealthfolioError as e:
            log.info("%s: no holdings for the check: %s", key, e)
            return
        deviations = [d.as_dict() for d in compare(cash, positions, wf_cash, wf_portfolio,
                                                   {i: m.symbol for i, m in mappings.items()},
                                                   {i: a["asset_id"] for i, a in self.state.assets(key).items()})]
        previous = self.state.reconcile(key) or {}
        # Report only what shows up twice in a row (not a recalculation in
        # progress), and each set of deviations only once.
        persistent = [d for d in deviations if d in previous.get("deviations", [])]
        if persistent and persistent != previous.get("notified"):
            lines = [f"{d['name']}: {self.adapters[key].label} {d['broker']}, Wealthfolio {d['wealthfolio']}"
                     for d in persistent[:6]]
            self.notifier(cfg).send(f"{self.adapters[key].label}: Bestand weicht ab", "\n".join(lines),
                                    link=self.link(cfg, "/"), tags="scales")
        self.state.set_reconcile(key, deviations, notified=persistent)

    def _settle_securities_cash(self, key: str, tx: Transaction, accounts: Accounts, existing: ExistingIndex,
                                result: BrokerResult) -> bool:
        """A bank booking for a securities trade/payout: there if the securities side booked its transfer."""
        signed = tx.signed if tx.signed is not None else -tx.net
        match = existing.find_settlement(accounts.cash, signed, tx.datetime, SETTLEMENT_DAYS)
        if not match:
            return False
        self.state.mark(key, tx.id, "existing", [match])
        self.state.resolve_unknown(key, tx.id)
        result.existing += 1
        return True

    def _report_unmatched(self, cfg: config_mod.Config, key: str, events: list[Transaction],
                          result: BrokerResult) -> None:
        # Not marked as synced: once the PDF statement is imported, the next run finds it.
        new = [t for t in events if self.state.add_unknown(
            key, t.id, UNMATCHED_SECURITIES, t.datetime.isoformat(),
            {"betrag": str(t.signed if t.signed is not None else -t.net), "buchungstext": t.label,
             "verwendungszweck": t.text[:200]})]
        result.unknown += len(events)
        if new:
            total = ", ".join(f"{t.signed if t.signed is not None else -t.net} {t.currency}" for t in new[:5])
            self.notifier(cfg).send(
                f"{self.adapters[key].label}: Wertpapier-Buchung ohne Gegenstück",
                f"{len(new)} Buchung(en) ({total}) gehören zu Wertpapiergeschäften, die noch nicht in Wealthfolio "
                "sind. Importiere die PDF-Abrechnung mit dem Broker Importer Addon; danach erkennt der nächste "
                "Abruf sie.",
                link=self.link(cfg, "/unknown"), tags="question",
            )

    def _since(self, key: str, bcfg: config_mod.BrokerConfig) -> datetime | None:
        last = self.state.last_success(key)
        if last:
            since = last - OVERLAP
            # Unmatched securities settlements stay in the window until the
            # securities side (PDF import) books them and the check finds them.
            open_since = self.state.oldest_open(key, UNMATCHED_SECURITIES)
            if open_since and open_since - timedelta(days=1) < since:
                since = open_since - timedelta(days=1)
            return since
        return self._start(bcfg)

    @staticmethod
    def _start(bcfg: config_mod.BrokerConfig) -> datetime | None:
        if bcfg.start_date:
            return datetime.fromisoformat(bcfg.start_date).replace(tzinfo=UTC)
        return None

    def _sync_tx(self, wf: WealthfolioClient, key: str, tx: Transaction, accounts: Accounts,
                 mappings: dict[str, SecurityMapping], patterns: list[TransferPattern], existing: ExistingIndex,
                 result: BrokerResult) -> None:
        try:
            payloads = to_activities(tx, key, accounts, mappings, patterns)
        except MappingError as e:
            result.failed += 1
            result.messages.append(f"{tx.datetime.date()} {tx.label or tx.kind} {tx.name}: {e}")
            return
        match = existing.find(tx_ref(key, tx), payloads)
        if match:
            self.state.mark(key, tx.id, "existing", [match])
            result.existing += 1
            imported = next((e for e in existing.existing if e["id"] == match), None)
            if imported is not None:
                try:
                    self._add_dividend_tax(wf, tx, imported)
                except WealthfolioError as e:
                    log.info("%s: tax not added to %s: %s", key, tx.id, e)
            return
        ids: list[str] = []
        try:
            for p in payloads:
                created = wf.create_activity(p)
                if isinstance(created, Duplicate):
                    continue  # this leg was created by an earlier, interrupted run
                ids.append(created.get("id", ""))
        except WealthfolioError as e:
            # Not marked: the next run retries; legs created now come back as duplicates.
            result.failed += 1
            result.messages.append(f"{tx.datetime.date()} {tx.label or tx.kind} {tx.name}: {e}")
            return
        self.state.mark(key, tx.id, "imported", ids)
        if ids:
            result.created += 1
        else:
            result.existing += 1

    def _report_unknown(self, cfg: config_mod.Config, key: str, events: list[Transaction],
                        result: BrokerResult) -> None:
        new = [t for t in events
               if self.state.add_unknown(key, t.id, t.raw_type or t.label, t.datetime.isoformat(), t.raw)]
        result.unknown = len(events)
        if new:
            types = ", ".join(sorted({t.raw_type or t.label for t in new}))
            self.notifier(cfg).send(
                f"{self.adapters[key].label}: {len(new)} unbekannte Buchungen",
                f"Nicht übernommen, bitte prüfen und ggf. von Hand eintragen: {types}",
                link=self.link(cfg, "/unknown"), tags="question",
            )
