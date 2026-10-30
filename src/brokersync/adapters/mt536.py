"""Depot transactions over FinTS: the request HKWDU (version 5) and its answer, MT536.

python-fints knows neither, so the segments are defined here (``segments()``;
python-fints finds a segment class by its name, e.g. ``HIWDU5``). MT536 lists,
per security (``:16R:FIN`` … ``:16S:FIN``), its movements (``:16R:TRAN`` …
``:16S:TRAN``) - securities moving in or out of the depot: purchases, sales,
deliveries, corporate actions. Payouts aren't in it (no securities move).

Per movement (``TRANSDET``):

- ``:36B::PSTA//UNIT/…`` (or ``FAMT``) - shares or nominal,
- ``:19A::PSTA//[N]EUR…`` - the amount posted,
- ``:22H::REDE//RECE|DELI`` - received / delivered,
- ``:22H::PAYM//APMT|FREE`` - against payment / free of payment,
- ``:22F::TRAN//SETT|CORP|…`` - settlement, corporate action, …,
- ``:98A::ESET//`` / ``:98C::ESET//`` - trade date, ``SETT`` - settlement date,
- ``:25D::MOVE//REVE`` - a reversal,
- ``:90B::DEAL//ACTU/EUR…`` - the price per share, if the bank sends it.

``to_transactions`` books them (DKB, #130): received/delivered against payment →
BUY/SELL with their money on the giro account; reversals, corporate actions,
free deliveries and anything incomplete are ``UNKNOWN``.

``structure`` describes an answer without values or numbers (tags, qualifiers and
the standard codes above), so a bank's format can be checked from the log.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from ..model import SAVINGS, Kind, Transaction
from .mt535 import clauses

BERLIN = ZoneInfo("Europe/Berlin")
# The giro booking that settles a trade comes 1 day before to 6 days after the trade day; a trade without one
# waits this long (it may not be booked yet), then counts on its posted amount.
SETTLEMENT_DAYS = (-1, 6)
UNSETTLED_DAYS = 7

_SEGMENTS: dict = {}


def segments() -> dict:
    """``HKWDU5`` / ``HIWDU5`` as python-fints segment classes (defined once, on first use)."""
    if _SEGMENTS:
        return _SEGMENTS
    from fints.fields import DataElementField, DataElementGroupField
    from fints.formals import Account3, DataElementGroup
    from fints.segments.base import FinTS3Segment

    class SecurityReference(DataElementGroup):
        """Wertpapierreferenz (not sent: all securities)"""

        kind = DataElementField(type='num', max_length=1, _d="Art der Wertpapierreferenz")
        code = DataElementField(type='an', max_length=30, _d="Wertpapiercode")

    class HKWDU5(FinTS3Segment):
        """Depotumsätze anfordern, version 5"""

        account = DataElementGroupField(type=Account3, _d="Depot")
        all_accounts = DataElementField(type='jn', _d="Alle Depots")
        security = DataElementGroupField(type=SecurityReference, required=False, _d="Wertpapierreferenz")
        date_start = DataElementField(type='dat', required=False, _d="Von Datum")
        date_end = DataElementField(type='dat', required=False, _d="Bis Datum")
        max_number_responses = DataElementField(type='num', max_length=4, required=False, _d="Maximale Anzahl")
        touchdown_point = DataElementField(type='an', max_length=35, required=False, _d="Aufsetzpunkt")

    class HIWDU5(FinTS3Segment):
        """Depotumsätze rückmelden, version 5"""

        transactions = DataElementField(type='bin', _d="Depotumsätze (MT536)")

    _SEGMENTS.update(HKWDU5=HKWDU5, HIWDU5=HIWDU5)
    return _SEGMENTS


def fetch(client, account, start: date, end: date):
    """The MT536 texts for ``account`` (python-fints client, like its ``get_holdings``); a ``NeedTANResponse``
    if the bank wants a TAN."""
    seg = segments()
    with client._get_dialog() as dialog:
        hkwdu = client._find_highest_supported_command(seg["HKWDU5"])
        responses = client._fetch_with_touchdowns(
            dialog,
            lambda touchdown: hkwdu(account=hkwdu._fields['account'].type.from_sepa_account(account),
                                    all_accounts=False, date_start=start, date_end=end,
                                    touchdown_point=touchdown),
            lambda responses: responses,
            'HIWDU',
        )
    if type(responses).__name__ == "NeedTANResponse":
        return responses
    out = []
    for resp in responses:
        data = resp.transactions
        out.append(data.decode("latin-1") if isinstance(data, bytes) else str(data))
    return out


def needs_tan(client) -> bool:
    """Whether the bank's parameters (HIPINS) ask a TAN for HKWDU - then a scheduled run doesn't ask."""
    hkwdu = segments()["HKWDU5"]
    try:
        return bool(client._need_twostep_tan_for_segment(hkwdu()))
    except Exception:
        return False


@dataclass
class Movement:
    isin: str | None = None
    name: str = ""
    shares: Decimal | None = None
    amount: Decimal | None = None  # signed as the bank posts it
    currency: str | None = None
    trade_date: date | None = None
    settle_date: date | None = None
    direction: str = ""  # RECE (received) / DELI (delivered)
    payment: str = ""  # APMT (against payment) / FREE
    indicator: str = ""  # SETT, CORP, …
    reversal: bool = False
    reference: str = ""
    text: str = ""
    price: Decimal | None = None

    @property
    def what(self) -> str:
        """A German label for the log and for unknown events."""
        if self.reversal:
            return "Storno"
        if self.indicator == "CORP":
            return "Kapitalmaßnahme"
        if self.payment == "APMT":
            return {"RECE": "Kauf", "DELI": "Verkauf"}.get(self.direction, "sonstige")
        if self.payment == "FREE":
            return {"RECE": "Eingang ohne Zahlung", "DELI": "Ausgang ohne Zahlung"}.get(self.direction, "sonstige")
        return "sonstige"


def _number(text: str) -> Decimal | None:
    try:
        return Decimal(text.replace(",", "."))
    except InvalidOperation:
        return None


def _day(text: str) -> date | None:
    m = re.match(r"(\d{4})(\d{2})(\d{2})", text)
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None
    except ValueError:
        return None


# Fields of a movement read as they are (the 12-character prefix, then the value): attribute and how.
_FIELDS = {
    ":98A::ESET//": ("trade_date", _day), ":98C::ESET//": ("trade_date", _day),
    ":98A::SETT//": ("settle_date", _day), ":98C::SETT//": ("settle_date", _day),
    ":22H::REDE//": ("direction", lambda v: v[:4]),
    ":22H::PAYM//": ("payment", lambda v: v[:4]),
    ":22F::TRAN//": ("indicator", lambda v: v[:4]),
    ":25D::MOVE//": ("reversal", lambda v: v[:4] == "REVE"),
    ":20C::RELA//": ("reference", lambda v: v),
    ":70E::TRDE//": ("text", lambda v: " ".join(v.split("|"))),
}


def _read(current: Movement, f: str) -> None:
    """One field inside a movement (``:16R:TRAN`` … ``:16S:TRAN``)."""
    if f.startswith(":36B::PSTA//"):
        m = re.search(r"/(?:UNIT|FAMT)/(N?)(\d+(?:,\d*)?)", f)
        if m:
            value = _number(m.group(2))
            current.shares = -value if value is not None and m.group(1) else value
    elif f.startswith(":19A::PSTA//"):
        m = re.match(r"(N?)([A-Z]{3})(\d+(?:,\d*)?)", f[12:])
        if m:
            value = _number(m.group(3))
            current.amount = -value if value is not None and m.group(1) else value
            current.currency = m.group(2)
    elif f.startswith(":90B::DEAL//ACTU/"):
        m = re.match(r"[A-Z]{3}(\d+(?:,\d*)?)", f[17:])
        current.price = _number(m.group(1)) if m else None
    elif f[:12] in _FIELDS:
        attribute, convert = _FIELDS[f[:12]]
        setattr(current, attribute, convert(f[12:]))


def parse(text: str) -> list[Movement]:
    out: list[Movement] = []
    isin, name = None, ""
    current: Movement | None = None
    for f in clauses(text.splitlines()):
        if f.startswith(":16R:FIN"):
            isin, name = None, ""
        elif f.startswith(":35B:") and current is None:
            parts = f[5:].split("|")
            m = re.match(r"ISIN\s+([A-Z]{2}[A-Z0-9]{9}\d)", parts[0])
            isin = m.group(1) if m else None
            name = " ".join(p.strip() for p in parts[1:] if p.strip() and not p.strip().startswith("/"))
        elif f.startswith(":16R:TRAN") and not f.startswith(":16R:TRANSDET"):
            current = Movement(isin=isin, name=name)
        elif f.startswith(":16S:TRAN") and not f.startswith(":16S:TRANSDET"):
            if current is not None:
                out.append(current)
            current = None
        elif current is not None:
            _read(current, f)
    return out


# Tags whose 4-letter code after the qualifier is a standard value (no personal data): kept in the structure.
CODED = (":22F:", ":22H:", ":25D:", ":17B:")


def structure(texts: list[str]) -> str:
    """The answers' layout without values: how often each tag, qualifier and standard code occurs."""
    tags: Counter[str] = Counter()
    for text in texts:
        for f in clauses(text.splitlines()):
            m = re.match(r"(:\d{2}[A-Z]?:)(:?[A-Z0-9]{4}//)?", f)
            if not m:
                continue
            tag = "".join(g for g in m.groups() if g)
            if f.startswith((":16R:", ":16S:")):
                kind = re.match(r"[A-Z]*", f[5:])
                tag += kind.group(0) if kind else ""
            elif f.startswith(CODED) and m.group(2):
                code = re.match(r"[A-Z0-9]{4}", f[m.end():])
                tag += code.group(0) if code else ""
            elif m.group(2):
                kind = re.match(r"(?:[A-Z0-9]*/)?(UNIT|FAMT|ACTU|PRCT)\b", f[m.end():])
                tag += kind.group(1) if kind else ""
            tags[tag] += 1
    return ", ".join(f"{t}×{n}" for t, n in tags.items()) or "leer"


def summary(movements: list[Movement]) -> str:
    """What the movements would be booked as, and how complete they are - counts only."""
    if not movements:
        return "keine"
    kinds = Counter(m.what for m in movements)
    n = len(movements)
    complete = {
        "ISIN": sum(1 for m in movements if m.isin),
        "Stückzahl": sum(1 for m in movements if m.shares is not None),
        "Betrag": sum(1 for m in movements if m.amount is not None),
        "Handelstag": sum(1 for m in movements if m.trade_date),
    }
    signs = Counter("negativ" if m.amount < 0 else "positiv" for m in movements if m.amount is not None)
    return (", ".join(f"{k} {v}" for k, v in kinds.most_common()) + "; mit "
            + ", ".join(f"{k} {v}/{n}" for k, v in complete.items())
            + ("; Beträge " + ", ".join(f"{k} {v}" for k, v in signs.items()) if signs else ""))


def movement_id(m: Movement, seen: Counter) -> str:
    """``mt536-<day>-<hash>-<n>``: a hash of what the bank says about the movement, a counter for identical
    ones. No depot number. Never change it: every booked movement would look new."""
    key = "|".join(str(x) for x in (m.trade_date, m.settle_date, m.isin, m.shares, m.amount, m.currency,
                                     m.direction, m.payment, m.indicator, m.reversal, m.reference))
    digest = hashlib.sha256(key.encode()).hexdigest()[:16]
    seen[digest] += 1
    day = m.trade_date or m.settle_date
    return f"mt536-{day:%Y%m%d}-{digest}-{seen[digest]}" if day else f"mt536-0-{digest}-{seen[digest]}"


def _trade_kind(m: Movement) -> Kind | None:
    if m.reversal or m.indicator == "CORP" or m.payment != "APMT" or not m.isin or not m.shares or not m.amount \
            or not (m.trade_date or m.settle_date):
        return None
    return {"RECE": Kind.BUY, "DELI": Kind.SELL}.get(m.direction)


def _settlement(kind: Kind, m: Movement, day: date, base: Decimal, giro: list[Transaction],
                used: set[str]) -> Transaction | None:
    """The giro booking (``SECURITIES_CASH``) that paid a trade: right sign and currency, in the days around it,
    costs (fee, for a sale also tax) at least 0 and under half the value; one naming the ISIN first."""
    found = []
    for t in giro:
        if (t.kind != Kind.SECURITIES_CASH or t.account == SAVINGS or t.id in used or t.signed is None
                or t.currency != (m.currency or "EUR") or (t.signed < 0) != (kind == Kind.BUY)):
            continue
        gap = (t.datetime.astimezone(BERLIN).date() - day).days
        cost = abs(t.signed) - base if kind == Kind.BUY else base - abs(t.signed)
        if SETTLEMENT_DAYS[0] <= gap <= SETTLEMENT_DAYS[1] and 0 <= cost < base / 2:
            found.append(((m.isin or "") not in f"{t.label} {t.text}", cost, abs(gap), t.id, t))
    return min(found, key=lambda c: c[:4])[4] if found else None


def to_transactions(movements: list[Movement], giro: list[Transaction], history: date | None,
                    today: date) -> list[Transaction]:
    """Movements → transactions. A purchase or sale gets the amount of the giro booking that settled it (that
    booking stays ``SECURITIES_CASH`` and is matched against the trade's cash leg in the same run), else the
    posted amount; a fee only where a price says it (for a sale fee and tax together). Before ``history`` (the
    giro account's first day read) its money never showed up there: ``external_cash``. A trade within the
    history without its giro booking waits ``UNSETTLED_DAYS``. Everything else is ``UNKNOWN``."""
    seen: Counter = Counter()
    used: set[str] = set()
    out = []
    for m in movements:
        tx_id = movement_id(m, seen)
        day = m.trade_date or m.settle_date or today
        when = datetime(day.year, day.month, day.day, 12, tzinfo=BERLIN).astimezone(UTC)
        common: dict[str, Any] = dict(id=tx_id, datetime=when, currency=m.currency or "EUR", isin=m.isin,
                                      name=m.name, label=m.what)
        kind = _trade_kind(m)
        if kind is None or m.shares is None or m.amount is None:
            out.append(_unknown(m, common))
            continue
        shares = abs(m.shares)
        value = shares * m.price if m.price else None
        before = history is not None and day < history
        paid = None if before else _settlement(kind, m, day, value or abs(m.amount), giro, used)
        if paid is not None:
            used.add(paid.id)
        elif not before and (today - day).days < UNSETTLED_DAYS:
            continue  # its giro booking may still come: the next run reads the movement again
        net = abs(paid.signed) if paid is not None and paid.signed is not None else abs(m.amount)
        fee = (net - value if kind == Kind.BUY else value - net) if value is not None else Decimal(0)
        if not 0 <= fee < net:
            fee = Decimal(0)  # a price that doesn't fit the money: no fee rather than a wrong one
        gross = net - fee if kind == Kind.BUY else net + fee
        out.append(Transaction(kind=kind, net=net, shares=shares, gross=gross, fee=fee, external_cash=before,
                               **common))
    return out


def _unknown(m: Movement, common: dict[str, Any]) -> Transaction:
    """Listed on *Unbekannt*: what the bank says, without depot number or reference."""
    raw = {"buchungstext": m.what, "isin": m.isin or "", "Bezeichnung": m.name,
           "stück": str(m.shares) if m.shares is not None else "",
           "betrag": str(m.amount) if m.amount is not None else "",
           "handelstag": m.trade_date.isoformat() if m.trade_date else "",
           "hinweis": " ".join(f"{k}//{v}" for k, v in (("REDE", m.direction), ("PAYM", m.payment),
                                                          ("TRAN", m.indicator)) if v)
                      + (" MOVE//REVE" if m.reversal else "")}
    net = abs(m.amount) if m.amount is not None else Decimal(0)
    return Transaction(kind=Kind.UNKNOWN, net=net, raw_type=f"MT536 {m.what}",
                       raw={k: v for k, v in raw.items() if v}, **common)
