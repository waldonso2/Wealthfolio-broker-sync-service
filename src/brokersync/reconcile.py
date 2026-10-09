"""Compares what the broker says with what Wealthfolio has - like the addon's
pre-import check (``reconcile.ts``), but against the live accounts.

- **Cash:** the broker's balance per currency vs. Wealthfolio's cash holding on
  the cash account.
- **Positions** (brokers that report them): shares per ISIN vs. Wealthfolio's
  holdings on the securities account, matched by the asset the ISIN is booked
  under (learned from its trades, ``brokersync.assets``), else by symbol.

Wealthfolio recalculates holdings in the background after new activities, so
a single deviation right after a sync may be a recalculation in progress; the
sync only reports deviations that show up in two runs in a row.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation

from .model import CashBalance, Position

CASH_TOLERANCE = Decimal("0.01")
SHARES_TOLERANCE = Decimal("0.0001")


@dataclass(frozen=True)
class Deviation:
    kind: str  # "cash" | "position"
    key: str  # currency or ISIN
    name: str
    broker: str
    wealthfolio: str

    def as_dict(self) -> dict:
        return asdict(self)


def _dec(v) -> Decimal:
    try:
        return Decimal(str(v)) if v is not None else Decimal(0)
    except InvalidOperation:
        return Decimal(0)


def _fmt(d: Decimal) -> str:
    s = format(d.normalize(), "f")
    return s.rstrip("0").rstrip(".") if "." in s else s


def compare(cash: list[CashBalance], positions: list[Position] | None, wf_cash: list[dict],
            wf_portfolio: list[dict], symbol_of: dict[str, str],
            asset_of: dict[str, str] | None = None) -> list[Deviation]:
    """Deviations between broker and Wealthfolio.

    A position is found by its asset (``asset_of``: ISIN → Wealthfolio asset id,
    see ``brokersync.assets``), else by the symbol it is booked under
    (``symbol_of``: ISIN → mapped symbol, else the ISIN).
    """
    out: list[Deviation] = []
    wf_cash_by_ccy: dict[str, Decimal] = {}
    for h in wf_cash:
        if (h.get("holdingType") or "").lower() == "cash":
            ccy = h.get("localCurrency") or (h.get("instrument") or {}).get("currency") or ""
            wf_cash_by_ccy[ccy] = wf_cash_by_ccy.get(ccy, Decimal(0)) + _dec(h.get("quantity"))
    for c in cash:
        have = wf_cash_by_ccy.get(c.currency, Decimal(0))
        if abs(have - c.amount) > CASH_TOLERANCE:
            out.append(Deviation("cash", c.currency, f"Cash {c.currency}", _fmt(c.amount), _fmt(have)))

    if positions is None:
        return out
    # Per asset: (shares, name); found by asset id or by symbol.
    wf_shares: dict[str, tuple[Decimal, str]] = {}
    by_symbol: dict[str, str] = {}
    for h in wf_portfolio:
        if (h.get("holdingType") or "").lower() != "security" or h.get("isClosed"):
            continue
        inst = h.get("instrument") or {}
        symbol = inst.get("symbol") or ""
        key = inst.get("id") or symbol
        qty, _ = wf_shares.get(key, (Decimal(0), ""))
        wf_shares[key] = (qty + _dec(h.get("quantity")), inst.get("name") or symbol)
        by_symbol.setdefault(symbol, key)
    seen: set[str] = set()
    for p in positions:
        key = (asset_of or {}).get(p.isin)
        if key not in wf_shares:
            symbol = symbol_of.get(p.isin, p.isin)
            key = by_symbol.get(symbol, symbol)
        seen.add(key)
        have, _ = wf_shares.get(key, (Decimal(0), ""))
        if abs(have - p.shares) > SHARES_TOLERANCE:
            out.append(Deviation("position", p.isin, p.name or key, _fmt(p.shares), _fmt(have)))
    for key, (qty, name) in wf_shares.items():
        if key not in seen and abs(qty) > SHARES_TOLERANCE:
            out.append(Deviation("position", key, name, "0", _fmt(qty)))
    return out
