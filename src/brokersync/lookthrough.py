"""A fund's constituents from its issuer's holdings file (waldonso2/wealthfolio-importer-addon#139).

``parse`` reads a CSV as the issuers publish it - iShares (a few lines of fund details above the header,
weights in percent, no ISIN), or any list with a name and a weight column, in English or German, with
``,``/``;``/tab and decimal point or comma. Each line becomes a ``xray.Constituent``; its key is the ISIN
when there is a valid one, else ticker and exchange, else the name. Weights are a share of the fund
(0..1); what they leave open stays open (``xray.UNASSIGNED``).

``fetch`` gets the file itself (always, #144; ``Syncer.update_funds`` after a run, at most every
``REFRESH_DAYS`` per fund) - for now only iShares (Xtrackers by ISIN, an XLSX, is there but not called), by
its product id - from iShares' fund finder (``ISHARES_SCREENER``, every product with its ISIN, read once per
pass), from the product page's address the user saves, or, while the fund finder is down, ``ISHARES``. Only
the ISIN or that address leaves the container; only ``HOSTS`` are asked, answers are read up to
``MAX_BYTES`` and must look like a holdings list. Amundi and Vanguard: upload only for now (their
interfaces are unverified, and a swap ETF's list would be its collateral basket, not the index).
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
import time
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import quote
from xml.etree import ElementTree

import httpx

from . import costs, meta_keys, xray
from .assets import valid_isin
from .logos import isin_of
from .model import when
from .state import State, now
from .wealthfolio import WealthfolioError
from .web.i18n import N_
from .xray import Constituent

log = logging.getLogger(__name__)

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
    raise ValueError(N_("Unbekannte Zeichenkodierung - bitte die CSV-Datei des Anbieters hochladen."))


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
        raise ValueError(N_("Keine Positionsliste: Spalten für Name und Gewichtung fehlen."))
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
        raise ValueError(N_("Die Positionsliste enthält keine Zeile mit Gewichtung."))
    # Weights in percent (iShares, most issuers) or already as a share.
    scale = Decimal(100) if sum(w for _, _, w, _ in found) > Decimal("1.5") else Decimal(1)
    return tuple(Constituent(key, label, w / scale, cell(row, country), cell(row, sector), cell(row, currency))
                 for key, label, w, row in found)


# ── fetching ─────────────────────────────────────────────────────────────────
HOSTS = ("www.ishares.com", "etf.dws.com")
BROWSER = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
MAX_BYTES = 20 * 1024 * 1024
MAX_COLUMN = 16384  # Excel's last column, XFD
REFRESH_DAYS = 0.75  # daily: the timer runs once a day, so the next day's run must find a list due
NONE_DAYS = 30  # an ISIN no source knows is asked again after this
DWS = "https://etf.dws.com/etfdata/export/GBR/ENG/excel/product/constituent/{isin}/"
# The UK site's CSV export; its last path part is a constant of the domain, not of the fund.
ISHARES_CSV = ("https://www.ishares.com/uk/individual/en/products/{id}/{slug}/1506575576011.ajax"
               "?fileType=csv&fileName=holdings&dataType=fund")
# The fund finder's data: {portfolioId: {isin, productPageUrl, fundName, ...}} for every iShares product.
ISHARES_SCREENER = ("https://www.ishares.com/uk/individual/en/product-screener/product-screener-v3.1.jsn"
                    "?dcrPath=/templatedata/config/product-screener-v3/data/en/uk/product-screener/"
                    "ishares-product-screener-backend-config&siteEntryPassthrough=true")
# The site's search box: [{category, id, label}]; a product's id holds its page or its number. Used when the fund
# finder fails - it answers per ISIN.
# The UK site's first, then the German one's (it lists funds the UK site doesn't); a product is the same number on both.
ISHARES_SEARCH = tuple(f"https://www.ishares.com/{site}/autoComplete.search"
                       "?type=autocomplete&siteEntryPassthrough=true&term={isin}"
                       for site in ("uk/individual/en", "de/privatanleger/de"))
# The page's name after the number is optional: links often end at the number (.../produkte/308858/).
PRODUCT_PATH = re.compile(r"/(?:products|produkte)/(\d{4,8})(?:/([a-z0-9-]{3,120}))?")
ISHARES_PAGE = re.compile(r"^https://www\.ishares\.com/[a-z]{2}/[a-z-]+/[a-z]{2}/(?:products|produkte)/(\d{4,8})"
                          r"(?:/([a-z0-9-]{3,120}))?/?(?:[?#].*)?$")
# Only while the fund finder is down: product ids confirmed by another project's live check (dmxch/openfolio,
# 2026-06). Never a guess - a wrong id can be another fund's list.
ISHARES = {
    "IE00B4L5Y983": ("251882", "ishares-msci-world-ucits-etf-acc-fund"),
    "IE00BKM4GZ66": ("264659", "ishares-msci-emerging-markets-imi-ucits-etf"),
}


class SourceError(Exception):
    """The source answered, but not with a usable holdings list; the text is shown on the page."""


class NoSource(Exception):
    """No source knows this ISIN (a stock, or a fund of another issuer): upload only."""


def ishares_page(url: str) -> tuple[str, str] | None:
    """Product id and name in the address of an iShares product page, if it is one."""
    m = ISHARES_PAGE.match(url.strip())
    return _product(m) if m else None


def _product(m: re.Match) -> tuple[str, str]:
    # Any name serves: the export finds the list by the number (``holdings`` like the reference implementation).
    return m.group(1), m.group(2) or "holdings"


def xlsx_rows(data: bytes) -> list[list[str]]:
    """The first sheet of an XLSX as rows of text - enough for a holdings export, no styles or formulas."""
    try:
        book = zipfile.ZipFile(io.BytesIO(data))
        if sum(i.file_size for i in book.infolist()) > 2 * MAX_BYTES:  # a zip bomb, not a holdings list
            raise SourceError(N_("Die XLSX-Datei ist zu groß."))
        names = book.namelist()
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            # Python's expat refuses entity expansion attacks; ElementTree resolves no external entities.
            root = ElementTree.fromstring(book.read("xl/sharedStrings.xml"))  # noqa: S314 - see above
            shared = ["".join(t.text or "" for t in si.iter() if t.tag.endswith("}t")) for si in root
                      if si.tag.endswith("}si")]
        sheet = next((n for n in sorted(names) if n.startswith("xl/worksheets/sheet")), None)
        if sheet is None:
            raise SourceError(N_("Die XLSX-Datei enthält keine Tabelle."))
        root = ElementTree.fromstring(book.read(sheet))  # noqa: S314 - see above
    except (zipfile.BadZipFile, ElementTree.ParseError, KeyError) as e:
        raise SourceError(N_("Keine lesbare XLSX-Datei.")) from e
    rows = []
    for row in (e for e in root.iter() if e.tag.endswith("}row")):
        cells: dict[int, str] = {}
        for c in (e for e in row if e.tag.endswith("}c")):
            column = 0
            for ch in re.match(r"[A-Z]*", c.get("r", "")).group(0)[:3]:  # type: ignore[union-attr]
                column = column * 26 + ord(ch) - 64
            if column > MAX_COLUMN:  # beyond Excel's last column: no list of ours, and a row that big is a bomb
                continue
            value = next((v.text or "" for v in c.iter() if v.tag.endswith("}v")), "")
            if c.get("t") == "s":
                value = shared[int(value)] if value.isdigit() and int(value) < len(shared) else ""
            elif c.get("t") == "inlineStr":
                value = "".join(t.text or "" for t in c.iter() if t.tag.endswith("}t"))
            cells[(column or len(cells) + 1) - 1] = value
        rows.append([cells.get(i, "") for i in range(max(cells) + 1)] if cells else [])
    return rows


def parse_file(data: bytes) -> tuple[Constituent, ...]:
    """A holdings file as CSV or XLSX."""
    if data[:2] != b"PK":
        return parse(data)
    out = io.StringIO()
    csv.writer(out).writerows(xlsx_rows(data))
    return parse(out.getvalue().encode())


def _usable(lines: tuple[Constituent, ...], source: str) -> tuple[Constituent, ...]:
    total = sum((c.weight for c in lines), Decimal(0))
    if len(lines) < 5 or not Decimal("0.5") <= total <= Decimal("1.1"):
        raise SourceError(f"{source}: unvollständige Positionsliste ({len(lines)} Zeilen, {total * 100:.0f} %).")
    return lines


# The fund finder's fields that may carry the TER (#142); unverified - a pass logs a product's field names.
TER_FIELDS = ("ter", "fees", "ongoingCharges", "ongoingChargesRatio", "netExpenseRatio", "totalExpenseRatio")


def _ter(row: dict) -> Decimal | None:
    """The TER of a fund finder row as a share, if one of ``TER_FIELDS`` holds a plausible percentage."""
    for key in TER_FIELDS:
        value = row.get(key)
        if isinstance(value, dict):  # {"r": 0.2, "d": "0.20"}: raw and display value
            value = value.get("r", value.get("raw", value.get("d")))
        try:
            number = Decimal(str(value).replace("%", "").replace(",", ".").strip())
        except InvalidOperation:
            continue
        if number.is_finite() and 0 <= number <= 3:
            return number / 100
    return None


def _raw(value):
    """The fund finder's value: plain, or {"d": shown, "r": raw} like its numbers."""
    return (value.get("r") or value.get("d")) if isinstance(value, dict) else value


def _only_sources(request: httpx.Request) -> None:
    if request.url.scheme != "https" or request.url.host not in HOSTS:
        raise httpx.UnsupportedProtocol(f"Anfrage an {request.url.host} abgelehnt")


class Sources:
    def __init__(self, *, transport: httpx.BaseTransport | None = None, timeout: float = 60.0):
        # Redirects only to the sources themselves; a browser's name, or the issuers' CDN refuses.
        self._http = httpx.Client(timeout=timeout, transport=transport, follow_redirects=True,
                                  headers={"User-Agent": BROWSER, "Accept": "*/*"},
                                  event_hooks={"request": [_only_sources]})
        self._ishares: dict[str, tuple[str, str]] | None = None
        self._ter: dict[str, Decimal] = {}
        self.finder_error = ""  # why the fund finder gave no products this pass: an unknown ISIN may still be iShares'

    @property
    def finder_down(self) -> bool:
        return bool(self.finder_error)

    def ishares_product(self, isin: str) -> tuple[str, str] | None:
        """Product id and page name of the iShares fund with ``isin``, from the fund finder (read once)."""
        if self._ishares is None:
            self._ishares = {}
            rows: object = None
            try:
                data = self._get(ISHARES_SCREENER, "iShares-Fondsfinder")
                rows = json.loads(data) if data is not None else None
                if data is None:
                    self.finder_error = "iShares-Fondsfinder: HTTP 404"
            except SourceError as e:
                self.finder_error = str(e)
            except ValueError:
                self.finder_error = "iShares-Fondsfinder: Antwort ist kein JSON (vermutlich eine Zwischenseite)."
            for row in rows.values() if isinstance(rows, dict) else ():
                if not isinstance(row, dict):
                    continue
                m = PRODUCT_PATH.search(str(_raw(row.get("productPageUrl")) or ""))
                code = _raw(row.get("isin"))
                if m and isinstance(code, str):
                    if not self._ishares:  # names only, no values: which field holds the TER (#142)
                        log.info("iShares fund finder: fields of a product: %s", ", ".join(sorted(map(str, row))))
                    key = code.strip().upper()
                    self._ishares.setdefault(key, _product(m))
                    if (ter := _ter(row)) is not None:
                        self._ter.setdefault(key, ter)
            if not self._ishares and not self.finder_error:
                # names and types only, never values: enough to see how iShares changed the layout
                first = next(iter(rows.values()), None) if isinstance(rows, dict) else None
                log.warning("iShares fund finder: no products; answer %s with %d entries, first %s: %s",
                            type(rows).__name__, len(rows) if isinstance(rows, dict | list) else 0,
                            type(first).__name__, ", ".join(sorted(map(str, first)))[:500]
                            if isinstance(first, dict) else "-")
                self.finder_error = ("iShares-Fondsfinder: Antwort ohne Produkte mit ISIN und Produktseite "
                                     f"({len(rows) if isinstance(rows, dict | list) else 0} Einträge).")
        if isin in self._ishares or not self.finder_down:
            return self._ishares.get(isin)
        return ISHARES.get(isin) or self._search(isin)

    def _search(self, isin: str) -> tuple[str, str] | None:
        """The product of ``isin`` from the site's search box - only an unambiguous hit, never a guess. The UK
        site first, then the German one; an error only when neither answered."""
        error = None
        for url in ISHARES_SEARCH:
            try:
                found = self._hits(url.format(isin=quote(isin, safe="")))
            except SourceError as e:
                error = e
                continue
            if found:
                return found.pop() if len({p for p, _ in found}) == 1 else None
            error = None
        if error:
            raise error
        return None

    def _hits(self, url: str) -> set[tuple[str, str]]:
        data = self._get(url, "iShares-Suche")
        try:
            rows = json.loads(data or b"[]")
        except ValueError as e:
            raise SourceError("iShares-Suche: Antwort ist kein JSON (vermutlich eine Zwischenseite).") from e
        found = set()
        for row in rows if isinstance(rows, list) else ():
            if isinstance(row, dict) and row.get("category") == "productAutocomplete":
                text = str(row.get("id") or "")
                if m := PRODUCT_PATH.search(text):
                    found.add(_product(m))
                elif re.fullmatch(r"\d{4,8}", text):
                    found.add((text, "holdings"))
        return found

    def ter(self, isin: str) -> Decimal | None:
        """The TER the fund finder lists for ``isin`` (after ``ishares_product``)."""
        return self._ter.get(isin)

    def close(self) -> None:
        self._http.close()

    def _get(self, url: str, source: str) -> bytes | None:
        """The body, or None for 404; read no more than ``MAX_BYTES``."""
        try:
            with self._http.stream("GET", url) as r:
                if r.status_code == 404:
                    return None
                if r.status_code >= 400:
                    raise SourceError(f"{source}: HTTP {r.status_code}")
                data = b""
                for chunk in r.iter_bytes():
                    data += chunk
                    if len(data) > MAX_BYTES:
                        raise SourceError(f"{source}: Antwort größer als {MAX_BYTES // 1024 // 1024} MB")
                return data
        except httpx.HTTPError as e:
            raise SourceError(f"{source} ist nicht erreichbar ({type(e).__name__}).") from e

    def ishares(self, product: str, slug: str) -> tuple[Constituent, ...]:
        data = self._get(ISHARES_CSV.format(id=product, slug=slug), "iShares")
        # The export answers a wrong id with an HTML page and status 200.
        if not data or data.lstrip()[:1] == b"<":
            raise SourceError("iShares: keine Positionsliste unter dieser Produktseite.")
        try:
            return _usable(parse(data), "iShares")
        except ValueError as e:
            raise SourceError(f"iShares: {e}") from e

    def xtrackers(self, isin: str) -> tuple[Constituent, ...] | None:
        data = self._get(DWS.format(isin=isin), "Xtrackers")
        if not data or data[:2] != b"PK":  # no XLSX: not one of theirs
            return None
        try:
            return _usable(parse_file(data), "Xtrackers")
        except ValueError as e:
            raise SourceError(f"Xtrackers: {e}") from e


def fetch(sources: Sources, isin: str, page: str = "") -> tuple[tuple[Constituent, ...], str]:
    """The holdings of the fund with ``isin`` (or at the iShares product ``page``) and the source's name."""
    product = ishares_page(page) if page else sources.ishares_product(isin) if isin else None
    if product:
        return sources.ishares(*product), "iShares"
    # shortcut: only iShares for now (#144); Sources.xtrackers works - call it here again to bring Xtrackers back
    raise NoSource(isin)


# ── the depot ────────────────────────────────────────────────────────────────
@dataclass
class Depot:
    # {id, isin, name, currency, value, cost, parts: {account id: value}, trades: [costs.Trade]} per asset,
    # largest first
    positions: list[dict] = field(default_factory=list)
    cash: dict[str, Decimal] = field(default_factory=dict)  # per account name, in the base currency
    base: str = "EUR"

    @property
    def total(self) -> Decimal:
        return sum((p["value"] for p in self.positions), Decimal(0)) + sum(self.cash.values(), Decimal(0))

    @property
    def gain_share(self) -> Decimal | None:
        """Part of the securities' value that is gain (for the tax of a sale), from Wealthfolio's cost basis."""
        known = [p for p in self.positions if p.get("cost")]
        value = sum((p["value"] for p in known), Decimal(0))
        return max(Decimal(0), 1 - sum((p["cost"] for p in known), Decimal(0)) / value) if value else None


def _dec(value) -> Decimal:
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return Decimal(0)
    return number if number.is_finite() else Decimal(0)


def read_depot(wf, learned: dict[str, str] | None = None) -> Depot:
    """Every open position of every Wealthfolio account, added up per asset, values in the base currency.
    ``learned``: asset id -> ISIN the sync learned, for assets Wealthfolio keeps without one."""
    depot = Depot()
    isins = {a.get("id"): isin_of(a) or (learned or {}).get(a.get("id") or "", "") for a in wf.list_assets()}
    positions: dict[str, dict] = {}
    accounts = wf.list_accounts()
    for account in accounts:
        for h in wf.holdings(account.id):
            inst = h.get("instrument") or {}
            value = _dec((h.get("marketValue") or {}).get("base"))
            depot.base = h.get("baseCurrency") or depot.base
            kind = (h.get("holdingType") or "").lower()
            if kind == "cash":
                amount = value or _dec(h.get("quantity"))
                depot.cash[account.name] = depot.cash.get(account.name, Decimal(0)) + amount
            if kind != "security" or h.get("isClosed") or not inst.get("id"):
                continue
            isin = isins.get(inst["id"]) or isin_of({"instrumentSymbol": inst.get("symbol")}) or ""
            p = positions.setdefault(inst["id"], {"id": inst["id"], "isin": isin,
                                                  "name": inst.get("name") or inst.get("symbol") or inst["id"],
                                                  "currency": h.get("localCurrency") or "", "value": Decimal(0),
                                                  "cost": Decimal(0), "parts": {}, "trades": []})
            p["value"] += value
            p["parts"][account.id] = p["parts"].get(account.id, Decimal(0)) + value
            p["cost"] += _dec((h.get("costBasis") or {}).get("base"))
    depot.positions = sorted((p for p in positions.values() if p["value"] > 0), key=lambda p: -p["value"])
    if depot.positions:
        # shortcut: fees in the activity's currency, taken as the base currency; convert with fxRate if needed.
        for a in wf.search_activities([acc.id for acc in accounts], date(1970, 1, 1), date(2100, 1, 1)):
            held = positions.get(a.get("assetId") or "")
            if held is not None and a.get("activityType") in ("BUY", "SELL") and a.get("date"):
                held["trades"].append(costs.Trade(when(a["date"]).date(), a["activityType"],
                                               _dec(a.get("quantity")) * _dec(a.get("unitPrice")),
                                               _dec(a.get("fee")), _dec(a.get("tax")), a.get("accountId") or ""))
    return depot


# ── after a run ──────────────────────────────────────────────────────────────
BUDGET = 300  # seconds a pass may fetch; it runs under the sync lock, after the brokers


@dataclass
class Result:
    fetched: int = 0
    none: int = 0
    errors: list[str] = field(default_factory=list)


def status(state: State, asset_id: str) -> dict:
    """Where an asset's holdings come from: {status: ok|upload|none|error, source, url, at, error, lines}."""
    return json.loads(state.meta(meta_keys.FUND(asset_id)) or "{}")


def set_status(state: State, asset_id: str, **values) -> None:
    state.set_meta(meta_keys.FUND(asset_id), json.dumps({**status(state, asset_id), **values, "at": now()}))


def _days(at: str | None) -> float:
    return (datetime.now(UTC) - datetime.fromisoformat(at)).total_seconds() / 86400 if at else 1e9


def is_ishares(position: dict, st: dict) -> bool:
    """An iShares fund, as far as we can tell: by its name in Wealthfolio, or a list or page from iShares."""
    return "ishares" in position["name"].lower() or st.get("source") == "iShares" or bool(st.get("url"))


def update(state: State, depot: Depot, sources: Sources, *, force: bool = False, budget: float = BUDGET,
           only: str = "") -> Result:
    """Fetch the holdings of the depot's positions that are due; an uploaded file is kept unless the user
    saved an iShares page for the fund. ``force``: everything now (the button on *X-Ray*); ``only``: just
    this asset (its own button). Why an iShares fund got no list is stored at the fund, for the page."""
    result = Result()
    deadline = time.monotonic() + budget
    for p in [p for p in depot.positions if p["id"] == only] if only else depot.positions:
        if time.monotonic() > deadline:  # the rest is due again next run
            log.info("look-through: time budget used up, the remaining funds next run")
            break
        st = status(state, p["id"])
        if st.get("status") == "upload" and not st.get("url"):
            continue
        due = {"ok": REFRESH_DAYS, "none": NONE_DAYS, "error": REFRESH_DAYS}.get(st.get("status", ""), 0)
        if not force and _days(st.get("at")) < due:
            continue
        try:
            if not (p["isin"] or st.get("url")):
                if not is_ishares(p, st):
                    continue
                raise SourceError("Wealthfolio kennt keine ISIN zu diesem ETF - trag unter „Einstellungen dieses "
                                  "ETFs“ seine Produktseite bei iShares ein.")
            lines, source = fetch(sources, p["isin"], st.get("url", ""))
        except NoSource:
            if is_ishares(p, st):
                e = SourceError(f"iShares kennt die ISIN {p['isin']} nicht (Fondsfinder und Suche) - trag unter "
                                "„Einstellungen dieses ETFs“ seine Produktseite bei iShares ein.")
                set_status(state, p["id"], status="error", error=str(e))
                result.errors.append(f"{p['name']}: {e}")
                continue
            set_status(state, p["id"], status="none", source="", error="")
            result.none += 1
        except SourceError as e:
            set_status(state, p["id"], status="error", error=str(e))
            if is_ishares(p, st):  # a stock the search couldn't check isn't shown, so it isn't counted either
                result.errors.append(f"{p['name']}: {e}")
        else:
            state.set_fund(p["id"], p["name"], rows(lines))
            ter = sources.ter(p["isin"]) if source == "iShares" else None
            set_status(state, p["id"], status="ok", source=source, error="", lines=len(lines),
                       **({"ter": str(ter), "ter_source": source, "ter_at": now()} if ter is not None else {}))
            result.fetched += 1
    record(state, fetched=result.fetched, none=result.none, failed=len(result.errors),
           finder=sources.finder_error)
    return result


def record(state: State, **values) -> None:
    """The last pass, for the page's header: {at, fetched, none, failed, finder, error}."""
    state.set_meta(meta_keys.FUNDS_LAST(), json.dumps({**values, "at": now()}))


def last_pass(state: State) -> dict:
    return json.loads(state.meta(meta_keys.FUNDS_LAST()) or "{}")


def ter(st: dict) -> tuple[Decimal | None, str]:
    """A position's TER and where it is from: the user's own first, then the issuer's."""
    for key, source in (("ter_manual", "manual"), ("ter", st.get("ter_source", ""))):
        if st.get(key) not in (None, ""):
            return Decimal(st[key]), source
    return None, ""


def model(saved: dict[str, str]) -> costs.Model:
    return costs.Model(*(Decimal(saved[k]) if saved.get(k) else None for k in ("fixed", "percent", "spread",
                                                                                "custody")))


def cost(state: State, depot: Depot, cfg) -> costs.DepotCost:
    """The depot's costs from buy to sale, with the brokers' fee models (``Config.cost_models``)."""
    broker = {a: key for key, b in cfg.brokers.items()
              for a in (b.cash_account_id, b.portfolio_account_id, b.savings_account_id) if a}
    positions = [costs.Position(p["id"], p["name"], p["value"], p["cost"], p.get("parts", {}),
                                tuple(p.get("trades", ())), ter(status(state, p["id"]))[0]) for p in depot.positions]
    return costs.depot(positions, datetime.now(UTC).date(), broker,
                       {k: model(v) for k, v in cfg.cost_models.items()})


def rows(lines: tuple[Constituent, ...]) -> list[list[str]]:
    return [[c.key, c.name, str(c.weight), c.country, c.sector, c.currency] for c in lines]


def holdings(state: State, depot: Depot) -> list[xray.Holding]:
    """The depot for ``xray``: a position with a holdings list is a fund, any other one stock of its own."""
    funds = state.funds()
    out = []
    for p in depot.positions:
        f = funds.get(p["id"])
        if f:
            lines = tuple(Constituent(k, n, Decimal(w), co, se, cu) for k, n, w, co, se, cu in f["constituents"])
            out.append(xray.Holding(p["id"], p["name"], p["value"], lines))
        else:
            out.append(xray.stock(p["id"], p["isin"] or p["id"], p["name"], p["value"], currency=p["currency"]))
    return xray.unify(out)


def limits(saved: dict[str, str]) -> dict[str, Decimal]:
    out = dict(xray.LIMITS)
    for k, v in saved.items():
        if k in out:
            try:
                out[k] = Decimal(v)
            except InvalidOperation:
                continue
    return out


def accepted(state: State) -> set[str]:
    return set(json.loads(state.meta(meta_keys.XRAY_ACCEPTED()) or "[]"))


def new_risks(state: State, found: list[xray.Finding]) -> list[xray.Finding]:
    """The risks neither accepted nor notified before; remembers the current ones as notified (one that goes
    away and comes back is new again)."""
    current = [f for f in found if f.key not in accepted(state)]
    told = set(json.loads(state.meta(meta_keys.XRAY_NOTIFIED()) or "[]"))
    state.set_meta(meta_keys.XRAY_NOTIFIED(), json.dumps(sorted(f.key for f in current)))
    return [f for f in current if f.key not in told]


# ── into Wealthfolio's taxonomies ────────────────────────────────────────────
SOURCE = "BROKERSYNC"  # the assignments' source: ours; AUTO is Yahoo's, anything else the user's
# The issuers' names that differ from Wealthfolio's category names (all lower case).
ALIASES = {"usa": "united states", "united states of america": "united states", "us": "united states",
           "uk": "united kingdom", "great britain": "united kingdom", "korea (south)": "south korea",
           "korea, republic of": "south korea", "korea": "south korea", "netherlands (the)": "netherlands",
           "russian federation": "russia", "taiwan, province of china": "taiwan", "hong kong sar": "hong kong",
           "communication": "communication services", "telecommunication services": "communication services",
           "telecommunications": "communication services", "it": "information technology"}
# Taxonomy, the line's attribute, and which of its categories count (countries; the GICS sectors).
TAXONOMIES = (("regions", "country", lambda c: str(c.get("id", "")).startswith("country_")),
              ("industries_gics", "sector", lambda c: not c.get("parentId")))


@dataclass
class Written:
    written: int = 0  # taxonomies of assets replaced
    manual: int = 0  # left alone: the user assigned them
    unmatched: list[str] = field(default_factory=list)  # names Wealthfolio has no category for


def _bp(lines: list[list[str]], attr: int, categories: dict[str, str], unmatched: set[str]) -> list[tuple[str, int]]:
    """Weight per category in basis points, rounded down; a name without a category stays out (unassigned)."""
    out: dict[str, Decimal] = {}
    for line in lines:
        name = line[attr].strip().lower()
        if not name:
            continue
        category = categories.get(ALIASES.get(name, name))
        if category is None:
            unmatched.add(line[attr].strip())
            continue
        out[category] = out.get(category, Decimal(0)) + Decimal(line[2])
    # Negative cash or derivative lines can push the rest above 100 %; Wealthfolio refuses more than 10000 bp.
    total = sum(out.values(), Decimal(0))
    scale = 1 / total if total > 1 else Decimal(1)
    return sorted((c, int(w * scale * 10000)) for c, w in out.items() if int(w * scale * 10000) >= 1)


def write_taxonomies(state: State, wf, depot: Depot) -> Written:
    """Countries and sectors of every fund with a holdings list into Wealthfolio, so its own allocation views
    see through the funds. Only Yahoo's (``AUTO``) and our own assignments are replaced; unchanged ones are
    not written again."""
    result, unmatched = Written(), set[str]()
    categories = {tax: {str(c.get("name", "")).lower(): c["id"] for c in wf.taxonomy_categories(tax) if counts(c)}
                  for tax, _, counts in TAXONOMIES}
    funds = state.funds()
    for p in depot.positions:
        fund = funds.get(p["id"])
        if not fund:
            continue
        try:
            _write_asset(wf, p["id"], fund["constituents"], categories, unmatched, result)
        except WealthfolioError:  # one fund Wealthfolio refuses never keeps the others out
            log.exception("look-through: countries and sectors of %s not written", p["id"])
    result.unmatched = sorted(unmatched)[:20]
    state.set_meta(meta_keys.TAXONOMIES_LAST(), json.dumps({"written": result.written, "manual": result.manual,
                                                            "unmatched": result.unmatched, "at": now()}))
    return result


def _write_asset(wf, asset_id: str, constituents: list[list[str]], categories: dict[str, dict[str, str]],
                 unmatched: set[str], result: Written) -> None:
    existing = wf.asset_assignments(asset_id)
    for tax, attr, _ in TAXONOMIES:
        new = _bp(constituents, 3 if attr == "country" else 4, categories[tax], unmatched)
        current = [a for a in existing if a.get("taxonomyId") == tax]
        if any(str(a.get("source", "")).upper() not in ("AUTO", SOURCE) for a in current):
            result.manual += 1
            continue
        if not new:  # the list no longer names countries or sectors: ours go, Yahoo's stay
            for a in current:
                if a.get("source") == SOURCE:
                    wf.delete_assignment(a["id"])
            continue
        if sorted((a.get("categoryId"), a.get("weight")) for a in current) == new and all(
                a.get("source") == SOURCE for a in current):
            continue
        wf.replace_assignments(asset_id, tax, [{"assetId": asset_id, "taxonomyId": tax, "categoryId": c,
                                                "weight": w, "source": SOURCE} for c, w in new])
        result.written += 1


def remove_taxonomies(wf) -> int:
    """Take our assignments out of Wealthfolio again (Yahoo fills its own back in), also of assets sold
    since; returns how many."""
    removed = 0
    for asset in wf.list_assets():
        for a in wf.asset_assignments(asset["id"]):
            if a.get("source") == SOURCE:
                wf.delete_assignment(a["id"])
                removed += 1
    return removed
