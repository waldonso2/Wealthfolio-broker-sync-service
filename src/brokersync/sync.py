"""One sync run: every enabled broker → Wealthfolio.

Per broker: log in (stored session), fetch the transactions since the last
successful run (minus an overlap), skip the ones already synced, check the rest
against Wealthfolio's existing activities, create what is missing. A broker
that fails is reported and the others still run (AC 9). Unknown event types are
stored and reported once, never dropped (AC 10).

Only one run at a time: the web UI and the systemd timer share a lock file.
"""

from __future__ import annotations

import fcntl
import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import config as config_mod
from .adapters import ADAPTERS, AdapterError, AuthRequired, BrokerAdapter
from .dedup import ExistingIndex
from .mapping import Accounts, MappingError, SecurityMapping, to_activities, tx_ref
from .model import Kind, Transaction
from .notify import Notifier
from .state import State
from .vault import Vault
from .wealthfolio import Duplicate, WealthfolioClient, WealthfolioError

log = logging.getLogger(__name__)

# Re-fetch this much before the last successful run: brokers book late.
OVERLAP = timedelta(days=7)


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
def run_lock(data_dir: Path) -> Iterator[None]:
    Path(data_dir).mkdir(parents=True, exist_ok=True)
    with open(Path(data_dir) / "sync.lock", "a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            raise AlreadyRunning("A sync is already running.") from e
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


class Syncer:
    def __init__(self, data_dir: Path, *, wealthfolio: Callable[[str, str | None], WealthfolioClient] | None = None,
                 notifier: Notifier | None = None, adapters: dict[str, type[BrokerAdapter]] | None = None):
        self.data_dir = Path(data_dir)
        self.vault = Vault(self.data_dir)
        self.state = State(self.data_dir)
        self.adapters = adapters or ADAPTERS
        self._wealthfolio = wealthfolio or (lambda url, pw: WealthfolioClient(url, pw))
        self._notifier = notifier

    def notifier(self, cfg: config_mod.Config) -> Notifier:
        if self._notifier:
            return self._notifier
        return Notifier(cfg.ntfy_server, cfg.ntfy_topic, self.vault.load().get("ntfy_token"))

    def link(self, cfg: config_mod.Config, path: str) -> str | None:
        return f"{cfg.public_url.rstrip('/')}{path}" if cfg.public_url else None

    def run(self, only: list[str] | None = None) -> list[BrokerResult]:
        with run_lock(self.data_dir):
            cfg = config_mod.load(self.data_dir)
            results = []
            for key, bcfg in cfg.brokers.items():
                if only and key not in only:
                    continue
                if not bcfg.enabled or key not in self.adapters:
                    continue
                results.append(self._run_broker(cfg, key))
            return results

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
        return result

    def _sync_broker(self, cfg: config_mod.Config, key: str, result: BrokerResult) -> None:
        bcfg = cfg.brokers[key]
        if not bcfg.cash_account_id or not bcfg.portfolio_account_id:
            raise AdapterError("No Wealthfolio accounts assigned - finish the setup for this broker.")
        secrets = self.vault.load()
        stored = secrets.get("brokers", {}).get(key, {})
        adapter = self.adapters[key](stored.get("credentials", {}), stored.get("session"))
        try:
            adapter.login()
        finally:
            # Keep whatever session the adapter has, even half-way through a login.
            self.vault.set_broker_session(key, adapter.session_state())

        since = self._since(key, bcfg)
        transactions = adapter.get_transactions(since)
        self.vault.set_broker_session(key, adapter.session_state())

        known = self.state.known(key)
        todo = [t for t in transactions if t.id not in known]
        self._report_unknown(cfg, key, [t for t in todo if t.kind == Kind.UNKNOWN], result)
        todo = [t for t in todo if t.kind != Kind.UNKNOWN]
        if not todo:
            return

        accounts = Accounts(bcfg.cash_account_id, bcfg.portfolio_account_id)
        mappings = {isin: SecurityMapping(m.get("symbol") or isin, m.get("exchangeMic"), m.get("name"))
                    for isin, m in cfg.security_mappings.items()}
        with self._wealthfolio(cfg.wealthfolio_url, secrets.get("wealthfolio_password")) as wf:
            first = min(t.datetime for t in todo) - timedelta(days=2)
            last = max(t.datetime for t in todo) + timedelta(days=2)
            existing = ExistingIndex(wf.search_activities([accounts.cash, accounts.portfolio], first.date(),
                                                          last.date()))
            for tx in sorted(todo, key=lambda t: t.datetime):
                self._sync_tx(wf, key, tx, accounts, mappings, existing, result)

    def _since(self, key: str, bcfg: config_mod.BrokerConfig) -> datetime | None:
        last = self.state.last_success(key)
        if last:
            return last - OVERLAP
        if bcfg.start_date:
            return datetime.fromisoformat(bcfg.start_date).replace(tzinfo=UTC)
        return None

    def _sync_tx(self, wf: WealthfolioClient, key: str, tx: Transaction, accounts: Accounts,
                 mappings: dict[str, SecurityMapping], existing: ExistingIndex, result: BrokerResult) -> None:
        try:
            payloads = to_activities(tx, key, accounts, mappings)
        except MappingError as e:
            result.failed += 1
            result.messages.append(f"{tx.datetime.date()} {tx.label or tx.kind} {tx.name}: {e}")
            return
        match = existing.find(tx_ref(key, tx), payloads)
        if match:
            self.state.mark(key, tx.id, "existing", [match])
            result.existing += 1
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
