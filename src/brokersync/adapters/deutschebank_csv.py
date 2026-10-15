"""The Deutsche Bank's depot transactions from the CSV export of its online
banking ("Umsätze" of the depot) - FinTS doesn't deliver them.

The user uploads the file in the broker settings; the rows are kept (without
depot or account numbers) in the encrypted sync state, and every run turns
them into transactions like any other broker's:

- **Dividende/Ausschüttung** → DIVIDEND: net = *Ausmachender Betrag*, tax =
  capital gains tax + withholding tax - refunded taxes; the money goes to the
  giro account, whose booking (``SECURITIES_CASH``) is matched against it.
- **Kauf** / **Verkauf** → BUY / SELL, paid from / to the giro account.
- **Eingang** with an amount → BUY whose money came from outside (employee
  shares, a delivery against payment): ``external_cash``.
- Purchases and sales older than the giro history the service read are booked
  with ``external_cash`` too: their giro booking was never read. Dividends never
  are: their money stays on the cash account.
- Everything else (**Kapitaltransaktion**, **Ausgang**, an *Eingang* without
  amount, …) is ``UNKNOWN``: corporate actions and transfers are booked by hand.

Ids: ``dbcsv-<date>-<hash>-<n>`` from the row's content - uploading an export
again, or one that overlaps, adds nothing twice. Keep that scheme.
"""

from __future__ import annotations

import csv
import hashlib
import io
from collections import Counter
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from ..model import Kind, Transaction

BERLIN = ZoneInfo("Europe/Berlin")
REQUIRED = ("Buchungsdatum", "Umsatzart", "Nominal / Stück", "Bezeichnung", "ISIN", "Ausmachender Betrag")
# Kept per row; depot and account numbers and the portfolio name are dropped.
KEEP = ("Buchungsdatum", "Schlusstag / Zahltag", "Valuta", "Umsatzart", "Nominal / Stück", "Bezeichnung", "WKN",
        "ISIN", "Handelswährung", "Kurs", "Devisenkurs", "Stückzinsen",
        "Kapitalertragssteuer inkl. Solidaritätszuschlag", "Ausländische Quellensteuer", "Vergütete Steuern",
        "Eigene Spesen", "Fremde Spesen", "Ausmachender Betrag")


def parse(data: bytes) -> list[dict]:
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError("Das ist kein Depotumsatz-Export der Deutschen Bank (unbekannte Zeichenkodierung).")
    try:
        reader = csv.DictReader(io.StringIO(text), delimiter=";")
        if not reader.fieldnames or any(c not in reader.fieldnames for c in REQUIRED):
            raise ValueError("Das ist kein Depotumsatz-Export der Deutschen Bank (Spalten fehlen).")
        rows = []
        for row in reader:
            if not (row.get("Buchungsdatum") or "").strip():
                continue
            rows.append({k: (row.get(k) or "").strip() for k in KEEP})
    except csv.Error as e:  # e.g. NUL bytes: not a CSV export
        raise ValueError("Das ist kein Depotumsatz-Export der Deutschen Bank (keine lesbare CSV-Datei).") from e
    return rows


def _num(value: str) -> Decimal:
    value = (value or "").strip().replace(".", "").replace(",", ".")
    if not value:
        return Decimal(0)
    try:
        number = Decimal(value)
    except InvalidOperation as e:
        raise ValueError(f"Keine Zahl: {value!r}") from e
    if not number.is_finite():
        raise ValueError(f"Keine Zahl: {value!r}")
    return number


def _date(value: str) -> date:
    """ISO (2024-03-15) or German (15.03.2024)."""
    try:
        return date.fromisoformat(value)
    except ValueError:
        try:
            return datetime.strptime(value, "%d.%m.%Y").date()
        except ValueError as e:
            raise ValueError(f"Kein Datum: {value!r}") from e


def row_id(row: dict, seen: Counter) -> str:
    key = "|".join(row.get(k, "") for k in ("Buchungsdatum", "Umsatzart", "ISIN", "Nominal / Stück",
                                             "Ausmachender Betrag"))
    digest = hashlib.sha256(key.encode()).hexdigest()[:16]
    seen[digest] += 1
    return f"dbcsv-{row['Buchungsdatum']}-{digest}-{seen[digest]}"


def transactions(rows: list[dict], history_start: date | None) -> list[Transaction]:
    seen: Counter = Counter()
    out = []
    for row in rows:
        tx_id = row_id(row, seen)
        day = _date(row.get("Schlusstag / Zahltag") or row["Buchungsdatum"])
        when = datetime(day.year, day.month, day.day, 12, tzinfo=BERLIN).astimezone(UTC)
        kind_text = row["Umsatzart"]
        shares = _num(row["Nominal / Stück"])
        amount = _num(row["Ausmachender Betrag"])
        tax = (-_num(row["Kapitalertragssteuer inkl. Solidaritätszuschlag"]) - _num(row["Ausländische Quellensteuer"])
               - _num(row["Vergütete Steuern"]))
        fee = abs(_num(row["Eigene Spesen"])) + abs(_num(row["Fremde Spesen"]))
        before_history = history_start is None or day < history_start
        common = dict(id=tx_id, datetime=when, currency="EUR", isin=row["ISIN"] or None, name=row["Bezeichnung"],
                      label=kind_text)
        kind = kind_text.lower()
        if kind.startswith("dividende") or kind.startswith("ausschüttung") or kind == "ertrag":
            if amount <= 0 or not row["ISIN"]:
                out.append(_unknown(row, common))
                continue
            out.append(Transaction(kind=Kind.DIVIDEND, net=amount, shares=shares or None, gross=amount + tax, tax=tax,
                                   **common))
        elif kind in ("kauf", "eingang") and amount < 0 and shares > 0 and row["ISIN"]:
            net = -amount
            out.append(Transaction(kind=Kind.BUY, net=net, shares=shares, gross=net - fee - tax, fee=fee, tax=tax,
                                   external_cash=kind == "eingang" or before_history, **common))
        elif kind == "verkauf" and amount > 0 and shares > 0 and row["ISIN"]:
            out.append(Transaction(kind=Kind.SELL, net=amount, shares=shares, gross=amount + fee + tax, fee=fee,
                                   tax=tax, external_cash=before_history, **common))
        else:
            out.append(_unknown(row, common))
    return out


def _unknown(row: dict, common: dict) -> Transaction:
    raw = {k: row[k] for k in ("Buchungsdatum", "Umsatzart", "Nominal / Stück", "Bezeichnung", "ISIN",
                               "Ausmachender Betrag") if row.get(k)}
    return Transaction(kind=Kind.UNKNOWN, net=abs(_num(row["Ausmachender Betrag"])), raw_type=row["Umsatzart"],
                       raw=raw, **common)
