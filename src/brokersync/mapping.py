"""Transactions → Wealthfolio activity payloads.

Same rules as the Broker Importer addon (``src/pdf/activities.ts``,
``src/common.ts`` in waldonso2/wealthfolio-importer-addon), so the sync and the
CSV/PDF imports book the same way:

- Two accounts per broker: trades and dividends on the securities account,
  everything else on the cash account. Every cash movement on the securities
  account is funded from / swept to the cash account as a
  TRANSFER_OUT/TRANSFER_IN pair sharing one ``sourceGroupId`` - without it
  Wealthfolio counts the pair as spending. The securities account ends up
  holding no cash.
- BUY/SELL carry ``amount = tradeFinalCash`` (exact ``quantity × unitPrice ±
  (fee + tax)``): Wealthfolio's duplicate fingerprint hashes the exact amount.
- Fee and tax go into the ``fee``/``tax`` fields; a dividend or interest
  payment is one activity with its net amount. A tax refund can't go into the
  tax field, so it is its own CREDIT/TAX_REFUND.
- Comments end in ``[SYNC <broker>:<id>]`` and are part of the fingerprint:
  never reword them, or every synced activity reappears as new.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from .model import Kind, Transaction

CASH_SYMBOL_PREFIX = "$CASH-"
# Booked net must match gross ± fee ± tax within this.
TOLERANCE = Decimal("0.01")


@dataclass(frozen=True)
class Accounts:
    cash: str
    portfolio: str


@dataclass(frozen=True)
class SecurityMapping:
    symbol: str
    exchange_mic: str | None = None
    name: str | None = None
    asset_id: str | None = None  # an existing Wealthfolio asset (``brokersync.assets``)


@dataclass(frozen=True)
class TransferPattern:
    label: str
    iban: str = ""
    keyword: str = ""
    destination_account_id: str = ""

    @classmethod
    def from_config(cls, d: dict) -> TransferPattern:
        return cls(d.get("label", ""), (d.get("iban") or "").replace(" ", "").upper(), d.get("keyword") or "",
                   d.get("destinationAccountId") or "")


def match_pattern(tx: Transaction, patterns: list[TransferPattern]) -> TransferPattern | None:
    """Same order as the addon's matchPattern: IBAN exact, IBAN in the text, keyword in the text."""
    text = f"{tx.counterparty} {tx.text}".upper()
    iban = tx.counterparty_iban.replace(" ", "").upper()
    for test in (lambda p: p.iban and p.iban == iban,
                 lambda p: p.iban and p.iban in text.replace(" ", ""),
                 lambda p: p.keyword and p.keyword.upper() in text):
        for p in patterns:
            if test(p):
                return p
    return None


class MappingError(Exception):
    """The transaction can't be booked as reported (amounts don't add up, data missing)."""


def fmt(d: Decimal) -> str:
    """Plain decimal string without exponent or trailing zeros ("3016", "25.125")."""
    s = format(d.normalize(), "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def round6(d: Decimal) -> Decimal:
    return d.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)


def trade_final_cash(kind: Kind, quantity: str, unit_price: str, fee: str, tax: str = "0") -> str:
    """Exact cash flow of a trade, as the addon's ``tradeFinalCash``."""
    gross = abs(Decimal(quantity)) * abs(Decimal(unit_price))
    charges = abs(Decimal(fee or "0")) + abs(Decimal(tax or "0"))
    return fmt(gross + charges if kind == Kind.BUY else gross - charges)


def iso(dt: datetime, seconds: int = 0) -> str:
    """UTC ISO timestamp with milliseconds, like JavaScript's toISOString()."""
    d = (dt + timedelta(seconds=seconds)).astimezone(UTC)
    return d.strftime("%Y-%m-%dT%H:%M:%S.") + f"{d.microsecond // 1000:03d}Z"


def group_id(broker: str, tx: Transaction) -> str:
    return f"sync-{broker}-{tx.id}"


def tx_ref(broker: str, tx: Transaction) -> str:
    return f" [SYNC {broker}:{tx.id}]"


def _check_amounts(tx: Transaction) -> None:
    if tx.net < 0:
        raise MappingError("Booked amount is negative.")
    if tx.kind in (Kind.BUY, Kind.SELL):
        if not tx.isin or not tx.shares or tx.shares <= 0 or tx.gross is None:
            raise MappingError("ISIN, share count or market value missing.")
        expected = tx.gross + tx.fee + tx.tax if tx.kind == Kind.BUY else tx.gross - tx.fee - tx.tax
        if abs(expected - tx.net) > TOLERANCE:
            raise MappingError(
                f"Market value, fee and tax ({fmt(expected)}) don't add up to the booked amount ({fmt(tx.net)})."
            )
    if tx.kind == Kind.DIVIDEND:
        if not tx.isin or not tx.shares or tx.shares <= 0:
            raise MappingError("ISIN or share count missing.")
    if tx.kind in (Kind.DIVIDEND, Kind.INTEREST) and tx.gross is not None:
        if abs(tx.gross - tx.tax - tx.net) > TOLERANCE:
            raise MappingError(
                f"Gross income minus tax ({fmt(tx.gross - tx.tax)}) doesn't match the booked amount ({fmt(tx.net)})."
            )


def to_activities(
    tx: Transaction,
    broker: str,
    accounts: Accounts,
    mappings: dict[str, SecurityMapping] | None = None,
    patterns: list[TransferPattern] | None = None,
) -> list[dict]:
    """Wealthfolio ``NewActivity`` payloads for one transaction, in booking order."""
    if tx.kind == Kind.UNKNOWN:
        raise MappingError(f"Unknown event type {tx.raw_type or tx.label!r}.")
    if tx.kind == Kind.SECURITIES_CASH:
        raise MappingError("The cash side of a securities transaction is booked by the securities side.")
    _check_amounts(tx)
    mappings = mappings or {}
    ref = tx_ref(broker, tx)
    gid = group_id(broker, tx)
    ccy = tx.currency
    out: list[dict] = []

    def cash(account: str, activity_type: str, seconds: int, amount: Decimal, comment: str, *,
             subtype: str | None = None, source_group: str | None = None, tax: Decimal | None = None) -> dict:
        p = {
            "accountId": account,
            "activityType": activity_type,
            "subtype": subtype,
            "activityDate": iso(tx.datetime, seconds),
            "currency": ccy,
            "quantity": "1",
            "unitPrice": "1",
            "amount": fmt(amount),
            "tax": fmt(tax) if tax else None,
            "comment": comment,
            "asset": {"symbol": f"{CASH_SYMBOL_PREFIX}{ccy}"},
            "sourceGroupId": source_group,
        }
        return _clean(p)

    tax_field = fmt(tx.tax) if tx.tax > 0 else None
    refund = -tx.tax if tx.tax < 0 else Decimal(0)

    # Cash-only kinds live on the cash account.
    if tx.kind == Kind.DEPOSIT:
        return [cash(accounts.cash, "DEPOSIT", 0, tx.net, f"{tx.label or 'Deposit'}{_name(tx)}{_text(tx)}{ref}")]
    if tx.kind == Kind.WITHDRAWAL:
        # Only outbound money checks the transfer patterns (addon rule): an
        # inbound transfer is always a deposit.
        p = match_pattern(tx, patterns or [])
        if p:
            what = tx.text or tx.label or "Transfer"
            out.append(cash(accounts.cash, "TRANSFER_OUT", 0, tx.net, f"-> {p.label}: {what}{_name(tx, ' (')}{ref}",
                            source_group=gid if p.destination_account_id else None))
            if p.destination_account_id:
                out.append(cash(p.destination_account_id, "TRANSFER_IN", 0, tx.net,
                                f"<- {broker.upper()}: {what}{ref}", source_group=gid))
            return out
        return [cash(accounts.cash, "WITHDRAWAL", 0, tx.net,
                     f"{tx.label or 'Withdrawal'}{_name(tx)}{_text(tx)}{ref}")]
    if tx.kind == Kind.FEE:
        return [cash(accounts.cash, "FEE", 0, tx.net, f"{tx.label or 'Fee'}{_name(tx)}{_text(tx)}{ref}")]
    if tx.kind == Kind.TAX:
        return [cash(accounts.cash, "TAX", 0, tx.net, f"{tx.label or 'Tax'}{_name(tx)}{ref}")]
    if tx.kind == Kind.BONUS:
        return [cash(accounts.cash, "CREDIT", 0, tx.net, f"{tx.label or 'Bonus'}{_name(tx)}{_text(tx)}{ref}",
                     subtype="BONUS")]
    if tx.kind == Kind.TAX_REFUND:
        return [cash(accounts.cash, "CREDIT", 0, tx.net, f"{tx.label or 'Tax refund'}{_name(tx)}{ref}",
                     subtype="TAX_REFUND")]
    if tx.kind == Kind.INTEREST:
        out.append(cash(accounts.cash, "INTEREST", 0, tx.net - refund,
                        f"{tx.label or 'Interest'}{_name(tx)}{_text(tx)}{ref}",
                        tax=tx.tax if tx.tax > 0 else None))
        if refund:
            out.append(cash(accounts.cash, "CREDIT", 0, refund, f"Tax refund on interest{ref}", subtype="TAX_REFUND"))
        return out

    m = mappings.get(tx.isin or "")
    asset = _clean({
        "id": m.asset_id if m else None,
        "symbol": m.symbol if m else tx.isin,
        "exchangeMic": m.exchange_mic if m else None,
        "name": (m.name if m and m.name else None) or tx.name or None,
        "quoteCcy": ccy,
    })
    label = tx.label or tx.kind.value.title()

    def refund_credit(what: str) -> dict:
        return cash(accounts.portfolio, "CREDIT", 0, refund, f"Tax refund on {what} of {tx.name}{ref}",
                    subtype="TAX_REFUND")

    def sweep_to_cash(text: str) -> None:
        out.append(cash(accounts.portfolio, "TRANSFER_OUT", 1, tx.net, f"{text} -> Cash{ref}", source_group=gid))
        out.append(cash(accounts.cash, "TRANSFER_IN", 2, tx.net, f"{text} from Portfolio{ref}", source_group=gid))

    if tx.kind == Kind.DIVIDEND:
        out.append(_clean({
            "accountId": accounts.portfolio,
            "activityType": "DIVIDEND",
            "activityDate": iso(tx.datetime),
            "currency": ccy,
            "quantity": fmt(tx.shares),
            "amount": fmt(tx.net - refund),
            "tax": tax_field,
            "comment": f"{label} {tx.name}{ref}",
            "asset": asset,
        }))
        if refund:
            out.append(refund_credit("dividend"))
        sweep_to_cash(f"{label} {tx.name}")
        return out

    quantity = fmt(tx.shares)
    unit_price = fmt(round6(tx.gross / tx.shares))
    fee = fmt(tx.fee)
    trade = _clean({
        "accountId": accounts.portfolio,
        "activityType": tx.kind.value,
        "activityDate": iso(tx.datetime),
        "currency": ccy,
        "quantity": quantity,
        "unitPrice": unit_price,
        "fee": fee,
        "tax": tax_field,
        "amount": trade_final_cash(tx.kind, quantity, unit_price, fee, tax_field or "0"),
        "comment": f"{label} {tx.name}{ref}",
        "asset": asset,
    })

    if tx.kind == Kind.BUY and tx.bonus_funded:
        # As the addon's STOCKPERK-funded buy: the broker's money arrives on the
        # securities account as a bonus and pays the buy there.
        out.append(cash(accounts.portfolio, "CREDIT", -1, tx.net, f"{label} bonus for {tx.isin} ({tx.name}){ref}",
                        subtype="BONUS"))
        out.append(trade)
        return out

    if tx.kind == Kind.BUY:
        out.append(cash(accounts.cash, "TRANSFER_OUT", -2, tx.net,
                        f"Funds for {tx.isin} ({tx.name}) buy -> Portfolio{ref}", source_group=gid))
        out.append(cash(accounts.portfolio, "TRANSFER_IN", -1, tx.net, f"Funds from Cash for {tx.isin} buy{ref}",
                        source_group=gid))
        out.append(trade)
        if refund:
            out.append(refund_credit("purchase"))
        return out

    out.append(trade)
    if refund:
        out.append(refund_credit("sale"))
    sweep_to_cash(f"{tx.isin} ({tx.name}) sale")
    return out


def _name(tx: Transaction, sep: str = " ") -> str:
    name = tx.name or tx.counterparty
    if not name:
        return ""
    return f"{sep}{name})" if sep == " (" else f"{sep}{name}"


def _text(tx: Transaction) -> str:
    # Purpose of a bank transaction, shortened: Wealthfolio shows the comment in one line.
    text = " ".join(tx.text.split())
    return f": {text[:120]}" if text else ""


def _clean(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not None}
