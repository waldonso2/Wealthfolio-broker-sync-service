"""Scalable Capital through Scalable's official command line (``sc``, Scalable CLI).

``sc`` is Scalable's own CLI on top of the Scalable API (github.com/ScalableCapital/scalable-cli,
Apache-2.0). The user enables it once in the Scalable web platform under
*Profil > Sicherheit > Agentic Investing*. The service only calls its read
commands with ``--json`` and logs in with ``--local-read-only``, so even the CLI
itself refuses write commands (AC 7 of #35).

**Login** is OAuth's device-code flow: ``sc login`` prints a link and a code,
the user confirms both in the browser, the CLI polls until done. The session
(rotating refresh token, ``offline_access``) and the key the CLI signs its
requests with (DPoP) are files in the CLI's config directory. They live in a
temporary directory only while the adapter runs and are kept in the vault as
``session["files"]`` - nothing of it stays on disk. Later runs refresh the
token without the user; when Scalable asks for a new login, a scheduled run
sends the link by ntfy and waits for the confirmation.

**Transactions** (``sc broker transactions``, FILLED/SETTLED only) are mapped
like the addon's CSV import of the same data (``src/scalable.ts``; the CSV
comes from the userscript "Scalable Capital Transactions Exporter", which reads
the same API):

- Trades: BUY/SELL with shares, market value, fee (transaction + venue +
  crypto spread fee) and tax from ``sc broker transaction details``.
- Cash: deposits, withdrawals, interest, taxes and tax refunds, fees.
  Distributions are dividends without a share count (the API has none).
- A depot migration moves positions out and back in (same ISIN and share
  count within 7 days); its cash leg is a deposit marked ``SWITCH-`` plus a
  withdrawal of the same amount. Such pairs cancel out and are skipped.
- Cancellations (``isCancellation``), unpaired security transfers, fund swaps,
  ELTIFs and every other type are ``Kind.UNKNOWN``: listed and reported, never
  booked. Their payload keeps types, ISIN and amount only - the description of
  a cash transfer can carry a name.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

from ..model import BrokerAccount, CashBalance, Kind, Position, Transaction
from .base import AdapterError, AuthRequired, BrokerAdapter, Challenge, CredentialField

log = logging.getLogger(__name__)
BERLIN = ZoneInfo("Europe/Berlin")

# Where install-sc puts the binary; BROKERSYNC_SC overrides, else sc on the PATH.
DEFAULT_SC = "/opt/wealthfolio-broker-sync/bin/sc"
CONFIG_TOML = '[auth]\nsession_backend = "file"\nsigning_key_backend = "file"\n'
# Files the CLI keeps in its config directory that make up the session.
SESSION_FILES = ("session.json", "auth-signing-key.json", "broker_context.json", "state.json")
COMMAND_TIMEOUT = 120
LOGIN_PROMPT_TIMEOUT = 30
# How long "Ich habe bestätigt" waits for the CLI to see the confirmation (it polls every few seconds).
CONFIRM_WAIT = 45
# A scheduled run waits this long for the user to confirm the link from the notification.
UNATTENDED_LOGIN_WAIT = 10 * 60
PAGE_SIZE = 100
# Scalable rate-limits the API (RATE_LIMITED after ~150 detail queries in half a
# minute): pause between detail queries, and on the limit wait and try again.
DETAIL_PAUSE = 1.0
RATE_LIMIT_WAITS = (30, 60, 120)
RETRY_AFTER = re.compile(r"retry after (\d+)s")
NETTING_WINDOW = timedelta(days=7)
RELOGIN_CODES = {"no_session", "refresh_relogin_required"}

CASH_KINDS = {
    "DEPOSIT": Kind.DEPOSIT,
    "CASH_TRANSFER_IN": Kind.DEPOSIT,
    "WITHDRAWAL": Kind.WITHDRAWAL,
    "CASH_TRANSFER_OUT": Kind.WITHDRAWAL,
    "TAX_RETURN": Kind.TAX_REFUND,
}
LABELS = {
    "DEPOSIT": "Einlage", "CASH_TRANSFER_IN": "Einlage", "WITHDRAWAL": "Entnahme", "CASH_TRANSFER_OUT": "Entnahme",
    "TAX_RETURN": "Steuerrückerstattung", "DISTRIBUTION": "Dividende", "INTEREST": "Zinsen", "TAX": "Steuer",
    "FEE": "Gebühr", "BUY": "Kauf", "SELL": "Verkauf", "SAVINGS_PLAN": "Sparplan",
}


def shape(v):
    """The structure of a CLI answer - keys and value types, no values - for the log."""
    if isinstance(v, dict):
        return {k: shape(x) for k, x in v.items()}
    if isinstance(v, list):
        return [shape(v[0]), f"x{len(v)}"] if v else []
    return None if v is None else type(v).__name__


def summary(items: list[dict]) -> str:
    """Transactions by type, status and subtype, for the log (no amounts, ids or names)."""
    kinds = Counter((i.get("type"), i.get("status"), i.get("cash_transaction_type") or i.get("side")
                     or i.get("non_trade_security_transaction_type")) for i in items)
    return ", ".join(f"{'/'.join(str(x) for x in k)}: {n}" for k, n in kinds.most_common()) or "none"


def sc_binary() -> str:
    explicit = os.environ.get("BROKERSYNC_SC")
    if explicit:
        return explicit
    if Path(DEFAULT_SC).exists():
        return DEFAULT_SC
    return shutil.which("sc") or DEFAULT_SC


class ScError(AdapterError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class RateLimited(AdapterError):
    """Scalable still rate-limits after waiting."""


def _rate_limited(e: ScError) -> bool:
    return e.code == "rate_limited" or "RATE_LIMITED" in str(e)


def _dec(v) -> Decimal | None:
    if v is None or v == "":
        return None
    if isinstance(v, dict):  # money objects {"amount": .., "currency": ..}
        v = v.get("amount", v.get("value"))
    try:
        return Decimal(str(v))
    except InvalidOperation:
        return None


def _when(v) -> datetime | None:
    if isinstance(v, dict):
        v = v.get("time")
    if not v:
        return None
    d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def _first(*values):
    return next((v for v in values if v is not None), None)


# ── mapping ─────────────────────────────────────────────────────────────────
def _unknown(item: dict, when: datetime, raw_type: str, note: str = "") -> Transaction:
    payload = {k: item.get(k) for k in ("type", "cash_transaction_type", "security_transaction_type",
                                        "non_trade_security_transaction_type", "side", "status", "is_cancellation",
                                        "isin", "related_isin", "quantity") if item.get(k) not in (None, "")}
    if item.get("amount") not in (None, ""):
        payload["betrag"] = f"{item['amount']} {item.get('currency') or 'EUR'}"
    if note:
        payload["hinweis"] = note
    return Transaction(id=item["id"], kind=Kind.UNKNOWN, datetime=when, currency=item.get("currency") or "EUR",
                       net=Decimal(0), isin=item.get("isin") or item.get("related_isin") or None,
                       label=LABELS.get(raw_type, raw_type), raw_type=raw_type, raw=payload)


def _trade(item: dict, details: dict | None, when: datetime) -> Transaction:
    side = item.get("side") or ""
    trade = (details or {}).get("security_trade") or {}
    amounts = trade.get("trade_transaction_amounts") or {}
    shares_info = trade.get("number_of_shares") or {}
    shares = _first(_dec(shares_info.get("filled")), _dec(item.get("quantity")))
    gross = _dec(amounts.get("market_valuation"))
    fee = sum((_dec(amounts.get(k)) or Decimal(0) for k in ("transaction_fee", "venue_fee", "crypto_spread_fee")),
              Decimal(0))
    tax = _dec(amounts.get("tax_amount")) or Decimal(0)
    net = abs(_first(_dec(item.get("amount")), _dec(trade.get("total_amount"))) or Decimal(0))
    security = (details or {}).get("security") or {}
    subtype = item.get("security_transaction_type") or ""
    return Transaction(
        id=item["id"], kind=Kind.BUY if side == "BUY" else Kind.SELL, datetime=when,
        currency=item.get("currency") or "EUR", net=net, isin=item.get("isin") or security.get("isin"),
        name=security.get("name") or item.get("description") or "", shares=abs(shares) if shares else None,
        gross=abs(gross) if gross is not None else None, fee=abs(fee), tax=tax,
        label=LABELS["SAVINGS_PLAN"] if subtype == "SAVINGS_PLAN" else LABELS.get(side, side.title()),
    )


def _cash(item: dict, details: dict | None, when: datetime) -> Transaction:
    typ = item.get("cash_transaction_type") or ""
    amount = _dec(item.get("amount")) or Decimal(0)
    ccy = item.get("currency") or "EUR"
    common = dict(id=item["id"], datetime=when, currency=ccy, net=abs(amount), label=LABELS.get(typ, typ))
    if typ in CASH_KINDS:
        text = item.get("description") or ""
        return Transaction(kind=CASH_KINDS[typ], text=text, **common)
    if typ == "INTEREST" and amount >= 0:
        return Transaction(kind=Kind.INTEREST, **common)
    if typ == "TAX":
        return Transaction(kind=Kind.TAX if amount <= 0 else Kind.TAX_REFUND, **common)
    if typ == "FEE" and amount <= 0:
        return Transaction(kind=Kind.FEE, text=item.get("description") or "", **common)
    if typ == "DISTRIBUTION" and amount > 0 and item.get("related_isin"):
        tax_details = ((details or {}).get("cash") or {}).get("tax_details") or {}
        gross, tax = _dec(tax_details.get("gross_amount")), _dec(tax_details.get("tax_amount"))
        security = (details or {}).get("security") or {}
        if gross is not None and tax is not None and abs(abs(gross) - abs(tax) - abs(amount)) <= Decimal("0.01"):
            common.update(gross=abs(gross), tax=abs(tax))
        return Transaction(kind=Kind.DIVIDEND, isin=item["related_isin"],
                           name=security.get("name") or item.get("description") or "", **common)
    return _unknown(item, when, typ or "CASH_TRANSACTION")


def needs_details(item: dict) -> bool:
    if item.get("is_cancellation"):
        return False
    return item.get("type") == "SECURITY_TRANSACTION" or item.get("cash_transaction_type") == "DISTRIBUTION"


def _direction(item: dict) -> int:
    """+1 into the depot, -1 out of it, 0 unknown (security transfers)."""
    typ = item.get("non_trade_security_transaction_type") or ""
    d = 1 if typ.endswith("_IN") else -1 if typ.endswith("_OUT") else 0
    return -d if item.get("is_cancellation") else d


def _netted(items: list[dict]) -> set[str]:
    """Ids of depot-migration pairs that cancel out (security legs and their cash leg)."""
    done: set[str] = set()
    transfers = [i for i in items if i.get("type") == "NON_TRADE_SECURITY_TRANSACTION" and _direction(i)]
    outs = sorted((i for i in transfers if _direction(i) < 0), key=lambda i: _when(i["last_event_datetime"]))
    ins = [i for i in transfers if _direction(i) > 0]
    for o in outs:
        best = None
        for i in ins:
            if i["id"] in done or i.get("isin") != o.get("isin"):
                continue
            if abs(_dec(i.get("quantity")) or 0) != abs(_dec(o.get("quantity")) or 0):
                continue
            gap = abs(_when(i["last_event_datetime"]) - _when(o["last_event_datetime"]))
            if gap <= NETTING_WINDOW and (best is None or gap < best[0]):
                best = (gap, i)
        if best:
            done.update((o["id"], best[1]["id"]))
    deposits = [i for i in items if i.get("cash_transaction_type") == "DEPOSIT"
                and "SWITCH-" in f"{i.get('id', '')} {i.get('description') or ''}"]
    withdrawals = [i for i in items if i.get("cash_transaction_type") == "WITHDRAWAL"]
    for dep in deposits:
        best = None
        for w in withdrawals:
            if w["id"] in done or abs(_dec(w.get("amount")) or 0) != abs(_dec(dep.get("amount")) or 0):
                continue
            gap = abs(_when(w["last_event_datetime"]) - _when(dep["last_event_datetime"]))
            if gap <= NETTING_WINDOW and (best is None or gap < best[0]):
                best = (gap, w)
        if best:
            done.update((dep["id"], best[1]["id"]))
    return done


def _day(item: dict):
    return _when(item["last_event_datetime"]).astimezone(BERLIN).date()


def _sales(items: list[dict]) -> tuple[list[Transaction], set[str]]:
    """Securities that left the depot for cash, booked as a sale like the addon does.

    - Fund swap / forced exchange: security ``SWAP_OUT`` plus cash ``SWAP_OUT``
      of the same ISIN on the same day; the cash leg is the proceeds.
    - Certificate redemption / knock-out: a security leg out with value 0 plus a
      distribution of the same ISIN on the same day, which is the payout.
    Returns the sales and the ids of the rows they consume.
    """
    out: list[Transaction] = []
    used: set[str] = set()
    legs = [i for i in items if i.get("type") == "NON_TRADE_SECURITY_TRANSACTION" and not i.get("is_cancellation")
            and _direction(i) < 0 and i.get("isin")]
    def cash_leg(leg: dict, typ: str) -> dict | None:
        return next((c for c in items if c["id"] not in used and c.get("type") == "CASH_TRANSACTION"
                     and not c.get("is_cancellation") and c.get("cash_transaction_type") == typ
                     and c.get("related_isin") == leg["isin"] and _day(c) == _day(leg)
                     and (_dec(c.get("amount")) or Decimal(0)) > 0), None)

    for leg in legs:
        shares = abs(_dec(leg.get("quantity")) or Decimal(0))
        if not shares or leg["id"] in used:
            continue
        cash, swap = None, False
        if (leg.get("non_trade_security_transaction_type") or "") == "SWAP_OUT":
            cash, swap = cash_leg(leg, "SWAP_OUT"), True
        if cash is None and (_dec(leg.get("amount")) or Decimal(0)) == 0:
            cash, swap = cash_leg(leg, "DISTRIBUTION"), False
        if cash is None:
            continue
        proceeds = _dec(cash["amount"])
        out.append(Transaction(
            id=leg["id"], kind=Kind.SELL, datetime=_when(leg["last_event_datetime"]),
            currency=cash.get("currency") or "EUR", net=proceeds, isin=leg["isin"],
            name=leg.get("description") or cash.get("description") or "", shares=shares, gross=proceeds,
            label="Fondstausch" if swap else "Rückzahlung"))
        used.update((leg["id"], cash["id"]))
    return out, used


def _cancelled(items: list[dict], known: set[str]) -> set[str]:
    """A cancelled distribution and its original cancel out, like in the addon - unless the
    original was synced already: then the cancellation stays a report (nothing is deleted)."""
    used: set[str] = set()
    originals = sorted((i for i in items if i.get("cash_transaction_type") == "DISTRIBUTION"
                        and not i.get("is_cancellation") and (_dec(i.get("amount")) or Decimal(0)) > 0),
                       key=lambda i: _when(i["last_event_datetime"]), reverse=True)
    for c in items:
        if not (c.get("is_cancellation") and c.get("cash_transaction_type") == "DISTRIBUTION"):
            continue
        amount = abs(_dec(c.get("amount")) or Decimal(0))
        original = next((o for o in originals if o["id"] not in used and o.get("related_isin") == c.get("related_isin")
                         and _dec(o.get("amount")) == amount
                         and _when(o["last_event_datetime"]) <= _when(c["last_event_datetime"])), None)
        if original and original["id"] not in known:
            used.update((c["id"], original["id"]))
    return used


def to_transactions(items: list[dict], details: dict[str, dict], known: set[str] | None = None) -> list[Transaction]:
    """Transaction summaries (``sc broker transactions``) plus details → transactions, oldest first.

    ``known``: ids the sync has already - a cancellation whose original is among
    them can't be undone here and is reported instead.
    """
    booked = [i for i in items if i.get("status") in ("FILLED", "SETTLED") and _when(i.get("last_event_datetime"))]
    out, consumed = _sales(booked)
    consumed |= _cancelled(booked, known or set())
    netted = _netted([i for i in booked if i["id"] not in consumed]) | consumed
    for item in booked:
        if item["id"] in netted:
            continue
        when = _when(item["last_event_datetime"])
        typ = item.get("type") or ""
        if item.get("is_cancellation"):
            out.append(_unknown(item, when, f"{item.get('cash_transaction_type') or typ}_CANCELLATION",
                                "Storno - die stornierte Buchung bitte in Wealthfolio prüfen"))
        elif typ == "SECURITY_TRANSACTION" and item.get("side") in ("BUY", "SELL"):
            out.append(_trade(item, details.get(item["id"]), when))
        elif typ == "CASH_TRANSACTION":
            out.append(_cash(item, details.get(item["id"]), when))
        else:
            sub = item.get("non_trade_security_transaction_type") if typ == "NON_TRADE_SECURITY_TRANSACTION" else None
            out.append(_unknown(item, when, sub or typ or item.get("summary_type") or "UNKNOWN"))
    return sorted(out, key=lambda t: t.datetime)


# ── the CLI ─────────────────────────────────────────────────────────────────
class ScCli:
    """Runs ``sc`` with its config directory in a private temporary directory."""

    def __init__(self, files: dict[str, str]):
        self._tmp = tempfile.TemporaryDirectory(prefix="brokersync-sc-")
        self.home = Path(self._tmp.name)
        self.config_dir = self.home / "config" / "scalable-cli"
        self.config_dir.mkdir(parents=True, mode=0o700)
        self._write("config.toml", CONFIG_TOML)
        for name, content in files.items():
            if name in SESSION_FILES:
                self._write(name, content)
        self.login_process: subprocess.Popen | None = None
        self._login_output: list[str] = []

    def _write(self, name: str, content: str) -> None:
        fd = os.open(self.config_dir / name, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(content)

    def files(self) -> dict[str, str]:
        return {name: (self.config_dir / name).read_text() for name in SESSION_FILES
                if (self.config_dir / name).is_file()}

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.update(HOME=str(self.home), XDG_CONFIG_HOME=str(self.home / "config"), NO_COLOR="1", TERM="dumb")
        return env

    def run(self, *args: str) -> dict:
        try:
            r = subprocess.run([sc_binary(), *args, "--json"], capture_output=True, text=True, env=self._env(),
                               cwd=self.home, timeout=COMMAND_TIMEOUT)
        except FileNotFoundError as e:
            raise AdapterError("Scalable CLI (sc) ist nicht installiert - im Container `update` ausführen.") from e
        except subprocess.TimeoutExpired as e:
            raise AdapterError("Scalable CLI hat nicht rechtzeitig geantwortet.") from e
        try:
            body = json.loads(r.stdout)
        except ValueError:
            detail = (r.stderr or r.stdout).strip()[:300] or f"exit {r.returncode}"
            raise AdapterError(f"Scalable CLI: {detail}") from None
        if not body.get("ok"):
            err = body.get("error") or {}
            raise ScError(err.get("code") or "error", err.get("message") or "Scalable CLI meldet einen Fehler.")
        data = body.get("data") or {}
        # Broker commands wrap their answer with the account/portfolio they used:
        # {"account_id", "portfolio_id", "resolution", "result": {...}} - read it like sc does.
        if isinstance(data, dict) and isinstance(data.get("result"), dict):
            return data["result"]
        return data

    def start_login(self) -> tuple[str, str]:
        """Start ``sc login``; returns (link, code) as soon as the CLI prints them."""
        try:
            self.login_process = subprocess.Popen(
                [sc_binary(), "login", "--local-read-only"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, env=self._env(), cwd=self.home)
        except FileNotFoundError as e:
            raise AdapterError("Scalable CLI (sc) ist nicht installiert - im Container `update` ausführen.") from e
        found = threading.Event()

        def read() -> None:
            for line in self.login_process.stdout:
                self._login_output.append(line.rstrip("\n"))
                if _parse_prompt(self._login_output):
                    found.set()
            found.set()

        threading.Thread(target=read, daemon=True).start()
        found.wait(LOGIN_PROMPT_TIMEOUT)
        prompt = _parse_prompt(self._login_output)
        if not prompt:
            self.stop_login()
            text = " ".join(self._login_output)[-300:]
            if "grant" in text.lower() or "unauthorized_client" in text:
                raise AdapterError(_NOT_ENABLED)
            raise AdapterError(f"Scalable-Anmeldung konnte nicht gestartet werden: {text or 'keine Antwort'}")
        return prompt

    def wait_login(self, seconds: float) -> bool | None:
        """True when the login finished, False when it failed, None while it is still waiting."""
        if self.login_process is None:
            return False
        try:
            code = self.login_process.wait(seconds)
        except subprocess.TimeoutExpired:
            return None
        self.login_process = None
        return code == 0

    def login_error(self) -> str:
        lines = [x for x in self._login_output if x.strip()]
        return lines[-1][:300] if lines else "unbekannter Fehler"

    def stop_login(self) -> None:
        if self.login_process is not None and self.login_process.poll() is None:
            self.login_process.kill()
            self.login_process.wait()
        self.login_process = None

    def cleanup(self) -> None:
        self.stop_login()
        self._tmp.cleanup()


_NOT_ENABLED = ("Scalable lässt die Anmeldung des CLI nicht zu: im Scalable-Webportal unter Profil > Sicherheit > "
                "Agentic Investing die Scalable CLI aktivieren und die Anmeldung neu starten.")
URL = re.compile(r"https://\S+")
CODE = re.compile(r"Verify the code\s+(\S+)")


def _parse_prompt(lines: list[str]) -> tuple[str, str] | None:
    text = "\n".join(lines)
    url, code = URL.search(text), CODE.search(text)
    return (url.group(0), code.group(1)) if url and code else None


class _Replay:
    """Answers ``ScCli.run`` from a recording (contract tests)."""

    def __init__(self, recording: dict):
        self.recording = recording
        self.pages = list(recording.get("transactions", []))

    def run(self, *args: str) -> dict:
        if args[:2] == ("broker", "transactions"):
            return self.pages.pop(0) if self.pages else {"items": [], "cursor": None}
        if args[:3] == ("broker", "transaction", "details"):
            return self.recording.get("details", {}).get(args[args.index("--transaction-id") + 1], {})
        if args[:2] == ("broker", "holdings"):
            return self.recording.get("holdings", {"items": []})
        if args[:2] == ("broker", "cash-breakdown"):
            return self.recording.get("cash", {})
        if args[0] == "whoami":
            return {}
        raise AdapterError(f"not recorded: {args}")

    def files(self) -> dict[str, str]:
        return {}

    def cleanup(self) -> None:
        pass


class ScalableAdapter(BrokerAdapter):
    key = "scalable"
    label = "Scalable Capital"
    credential_fields = [
        CredentialField("portfolio_id", "Depot-ID (optional)",
                        help="Nur nötig, wenn du mehrere Scalable-Depots hast: die portfolioId aus der Adresse der "
                             "Depot-Seite im Browser."),
    ]
    reports_positions = True

    # Replaced in tests.
    cli_factory: Callable[[dict[str, str]], ScCli] | None = None

    def __init__(self, credentials, session=None):
        super().__init__(credentials, session)
        self._cli = None

    @property
    def cli(self):
        if self._cli is None:
            factory = type(self).cli_factory or ScCli
            self._cli = factory(dict(self.session.get("files") or {}))
        return self._cli

    def _portfolio(self) -> list[str]:
        pid = (self.credentials.get("portfolio_id") or "").strip()
        return ["--portfolio-id", pid] if pid else []

    def _run(self, *args: str) -> dict:
        for attempt, wait in enumerate((*RATE_LIMIT_WAITS, None)):
            try:
                data = self.cli.run(*args)
                break
            except ScError as e:
                if not _rate_limited(e):
                    raise self._translate(e) from e
                if wait is None:
                    raise RateLimited(f"Scalable: {e}") from e
                m = RETRY_AFTER.search(str(e))
                pause = min(int(m.group(1)), 300) if m else wait
                log.info("scalable: rate limited (%s), waiting %ds (attempt %d)", args[1] if len(args) > 1 else args[0],
                         pause, attempt + 1)
                time.sleep(pause)
        self._keep()
        return data

    def _translate(self, e: ScError) -> Exception:
        if e.code in RELOGIN_CODES:
            return AuthRequired(Challenge("confirm", "Scalable möchte eine neue Anmeldung."))
        if e.code == "auth_grant_not_enabled":
            return AdapterError(_NOT_ENABLED)
        if e.code == "broker_context_missing":
            return AdapterError("Du hast mehrere Scalable-Depots: bitte die Depot-ID in den Broker-Einstellungen "
                                "eintragen.")
        return AdapterError(f"Scalable: {e}")

    def _keep(self) -> None:
        files = self.cli.files()
        if files:
            self.session["files"] = files

    # ── login ───────────────────────────────────────────────────────────────
    def login(self) -> None:
        if self.session.get("files"):
            try:
                self.cli.run("whoami")
                self._keep()
                return
            except ScError as e:
                if e.code not in RELOGIN_CODES:
                    raise self._translate(e) from e
        url, code = self.cli.start_login()
        if self.on_user_action:
            # Scheduled run: send the link to the phone and wait for the confirmation.
            self.on_user_action(f"Scalable möchte eine neue Anmeldung: Link öffnen und den Code {code} bestätigen. "
                                f"{url}")
            if self.cli.wait_login(UNATTENDED_LOGIN_WAIT):
                self._keep()
                return
            self.cli.stop_login()
            raise AuthRequired(Challenge("confirm", "Die Scalable-Anmeldung wurde nicht bestätigt."))
        raise AuthRequired(Challenge("confirm", f"Öffne den Link, melde dich bei Scalable an und bestätige den Code "
                                                f"{code}. Danach hier auf „Ich habe bestätigt“ klicken.", url))

    def complete_login(self, code: str) -> None:
        done = self.cli.wait_login(CONFIRM_WAIT)
        if done is None:
            prompt = _parse_prompt(self.cli._login_output)
            raise AuthRequired(Challenge("confirm", "Scalable hat die Bestätigung noch nicht gemeldet. Bitte im "
                                                    f"Browser den Code {prompt[1] if prompt else ''} bestätigen und "
                                                    "erneut klicken.", prompt[0] if prompt else ""))
        if not done:
            message = self.cli.login_error()
            if "grant" in message.lower() or "unauthorized_client" in message:
                raise AdapterError(_NOT_ENABLED)
            raise AdapterError(f"Scalable-Anmeldung fehlgeschlagen: {message}")
        self._keep()

    def close(self) -> None:
        if self._cli is not None:
            self._keep()
            self._cli.cleanup()
            self._cli = None

    @classmethod
    def replay(cls, recording: dict) -> ScalableAdapter:
        adapter = cls(recording.get("credentials", {}), {})
        adapter._cli = _Replay(recording)
        return adapter

    # ── data ────────────────────────────────────────────────────────────────
    def get_accounts(self) -> list[BrokerAccount]:
        pid = (self.credentials.get("portfolio_id") or "").strip()
        return [BrokerAccount(pid or "scalable", "Scalable Capital", "EUR")]

    def get_transactions(self, since: datetime | None) -> list[Transaction]:
        items: list[dict] = []
        cursor = None
        base = ["broker", "transactions", *self._portfolio(), "--page-size", str(PAGE_SIZE),
                "--include-reinvestment-subtypes"]
        if since:
            base += ["--from-time", since.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")]
        while True:
            page = self._run(*base, *(["--cursor", cursor] if cursor else []))
            items.extend(page.get("items") or [])
            cursor = page.get("cursor")
            if not cursor or not page.get("items"):
                break
        log.info("scalable: %d transactions from sc (%s)", len(items), summary(items))
        if items and not any(_when(i.get("last_event_datetime")) for i in items):
            log.warning("scalable: transactions without a usable date, first one: %s", json.dumps(shape(items[0])))
        # Details only for what the sync doesn't have yet: one query each, and Scalable rate-limits.
        wanted = [i for i in items if i.get("status") in ("FILLED", "SETTLED") and needs_details(i)
                  and i["id"] not in self.known_ids]
        details: dict[str, dict] = {}
        for n, item in enumerate(wanted):
            if n:
                time.sleep(DETAIL_PAUSE)
            try:
                details[item["id"]] = self._run("broker", "transaction", "details", *self._portfolio(),
                                                "--transaction-id", item["id"])
            except RateLimited:
                # Still limited after waiting: book what has its details now. Trades
                # without them fail visibly and the next run fetches them.
                log.warning("scalable: details for %d of %d transactions not fetched (rate limit), the next run "
                            "continues", len(wanted) - n, len(wanted))
                break
        txs = to_transactions(items, details, self.known_ids)
        missing = {i["id"] for i in wanted if i["id"] not in details}
        txs = [replace(t, label=f"{t.label} (Details folgen beim nächsten Abruf)") if t.id in missing
               and t.kind in (Kind.BUY, Kind.SELL) else t for t in txs]
        return [t for t in txs if since is None or t.datetime >= since]

    def get_positions(self) -> list[Position]:
        data = self._run("broker", "holdings", *self._portfolio())
        out = []
        for h in data.get("items") or []:
            shares = _dec(h.get("quantity"))
            if not h.get("isin") or not shares:
                continue
            out.append(Position(h["isin"], h.get("name") or "", shares,
                                h.get("valuation_currency") or "EUR", _dec(h.get("valuation"))))
        return out

    def get_cash(self) -> list[CashBalance]:
        data = self._run("broker", "cash-breakdown", *self._portfolio())
        amount = _dec(data.get("cash_balance"))
        if amount is None:
            log.warning("scalable: no cash_balance in sc broker cash-breakdown: %s", json.dumps(shape(data)))
            raise AdapterError("Scalable hat keinen Kontostand geliefert.")
        return [CashBalance("EUR", amount)]
