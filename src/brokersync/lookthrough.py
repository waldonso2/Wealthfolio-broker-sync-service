"""A fund's constituents from its issuer's holdings file (waldonso2/wealthfolio-importer-addon#139).

``parse`` reads a CSV as the issuers publish it - iShares (a few lines of fund details above the header,
weights in percent, no ISIN), or any list with a name and a weight column, in English or German, with
``,``/``;``/tab and decimal point or comma. Each line becomes a ``xray.Constituent``; its key is the ISIN
when there is a valid one, else ticker and exchange, else the name. Weights are a share of the fund
(0..1); what they leave open stays open (``xray.UNASSIGNED``).
"""

from __future__ import annotations

import csv
import io
from decimal import Decimal, InvalidOperation

from .assets import valid_isin
from .xray import Constituent

WEIGHT = ("weight (%)", "gewichtung (%)", "weighting", "gewichtung", "weight", "anteil (%)", "% of net assets")
NAME = ("name", "bezeichnung", "emittent", "security name")
ISIN = ("isin",)
TICKER = ("ticker", "emittententicker")
EXCHANGE = ("exchange", "börse")
COUNTRY = ("location", "standort", "country", "land", "sitz")
SECTOR = ("sector", "sektor", "industry classification", "branche")
CURRENCY = ("market currency", "marktwährung", "currency", "währung")


def _text(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("Unbekannte Zeichenkodierung - bitte die CSV-Datei des Anbieters hochladen.")


def _column(header: list[str], names: tuple[str, ...]) -> int | None:
    return next((header.index(n) for n in names if n in header), None)


def _number(value: str) -> Decimal | None:
    value = value.strip().replace("%", "").replace("\xa0", "").replace(" ", "")
    if "," in value and "." in value:  # the later one is the decimal mark
        comma_last = value.rfind(",") > value.rfind(".")
        value = value.replace(".", "").replace(",", ".") if comma_last else value.replace(",", "")
    else:
        value = value.replace(",", ".")
    try:
        number = Decimal(value)
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def _header(line: str) -> tuple[str, list[str]] | None:
    """Delimiter and column names, if the line is the header (it names a name and a weight column)."""
    delimiter = max(";,\t", key=line.count)
    header = [h.strip().lower() for h in next(csv.reader([line], delimiter=delimiter), [])]
    return (delimiter, header) if _column(header, WEIGHT) is not None and _column(header, NAME) is not None else None


def parse(data: bytes) -> tuple[Constituent, ...]:
    text = _text(data)
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if _header(line)), None)
    if start is None:
        raise ValueError("Keine Positionsliste: Spalten für Name und Gewichtung fehlen.")
    delimiter, header = _header(lines[start]) or (",", [])
    weight, name = _column(header, WEIGHT), _column(header, NAME)
    isin, ticker, exchange = _column(header, ISIN), _column(header, TICKER), _column(header, EXCHANGE)
    country, sector, currency = _column(header, COUNTRY), _column(header, SECTOR), _column(header, CURRENCY)

    def cell(row: list[str], i: int | None) -> str:
        return row[i].strip() if i is not None and i < len(row) else ""

    found: list[tuple[str, str, Decimal, list[str]]] = []
    for row in csv.reader(io.StringIO("\n".join(lines[start + 1:])), delimiter=delimiter):
        w = _number(cell(row, weight))
        if not w or not cell(row, name):  # footer, empty and zero lines
            continue
        code = cell(row, isin).upper()
        # shortcut: without an ISIN, funds of different issuers only meet by ticker or name; OpenFIGI later.
        key = code if len(code) == 12 and code.isalnum() and valid_isin(code) else (
            f"{cell(row, ticker)}:{cell(row, exchange)}".upper() if cell(row, ticker) else cell(row, name).upper())
        found.append((key, cell(row, name), w, row))
    if not found:
        raise ValueError("Die Positionsliste enthält keine Zeile mit Gewichtung.")
    # Weights in percent (iShares, most issuers) or already as a share.
    scale = Decimal(100) if sum(w for _, _, w, _ in found) > Decimal("1.5") else Decimal(1)
    return tuple(Constituent(key, label, w / scale, cell(row, country), cell(row, sector), cell(row, currency))
                 for key, label, w, row in found)
