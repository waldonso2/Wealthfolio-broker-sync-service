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
- ``:25D::MOVE//REVE`` - a reversal.

``structure`` describes an answer without values or numbers (tags, qualifiers and
the standard codes above), so a bank's format can be checked from the log.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from .mt535 import clauses

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
        elif current is None:
            continue
        elif f.startswith(":36B::PSTA//"):
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
        elif f.startswith((":98A::ESET//", ":98C::ESET//")):
            current.trade_date = _day(f[12:])
        elif f.startswith((":98A::SETT//", ":98C::SETT//")):
            current.settle_date = _day(f[12:])
        elif f.startswith(":22H::REDE//"):
            current.direction = f[12:16]
        elif f.startswith(":22H::PAYM//"):
            current.payment = f[12:16]
        elif f.startswith(":22F::TRAN//"):
            current.indicator = f[12:16]
        elif f.startswith(":25D::MOVE//"):
            current.reversal = f[12:16] == "REVE"
        elif f.startswith(":20C::RELA//"):
            current.reference = f[12:]
        elif f.startswith(":70E::TRDE//"):
            current.text = " ".join(f[12:].split("|"))
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
                tag += re.match(r"[A-Z]*", f[5:]).group(0)
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
