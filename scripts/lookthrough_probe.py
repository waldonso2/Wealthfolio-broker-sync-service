"""Phase 0 of the ETF look-through (waldonso2/wealthfolio-importer-addon#139): which issuer sources answer?

Usage (in the container, no venv or key needed - only the standard library):
    python3 -I lookthrough_probe.py [ISIN ...]        # without ISINs: a list of common UCITS ETFs

For each ISIN it asks every source that is keyed by ISIN (Xtrackers/DWS, Amundi) and iShares when the
product id is known, plus Vanguard, OpenFIGI and GLEIF once, and prints one Markdown table row per
request: HTTP status, redirect, type, size, rows, whether an ISIN column is there, and what the body is.
Nothing but the ISINs is sent; nothing is written. Paste the table into the issue.
"""

import csv
import json
import sys
import time
import urllib.error
import urllib.request

BROWSER = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
TIMEOUT = 30

# iShares product ids can't be derived from the ISIN. The first three are verified by
# dmxch/openfolio (27.6.2026); the others are guesses - the probe shows whether they hold.
ISHARES = {
    "IE00B4L5Y983": ("251882", "ishares-msci-world-ucits-etf-acc-fund", "SWDA"),
    "IE00BKM4GZ66": ("264659", "ishares-msci-emerging-markets-imi-ucits-etf", "EIMI"),
    "IE00B5BMR087": ("253743", "ishares-sp-500-b-ucits-etf-acc-fund", "CSPX"),
    "IE00B53SZB19": ("253741", "ishares-nasdaq-100-ucits-etf", "CNDX"),
    "IE00B6R52259": ("251850", "ishares-msci-acwi-ucits-etf", "SSAC"),
}
DEFAULT = [
    *ISHARES,
    "IE00BJ0KDQ92",  # Xtrackers MSCI World 1C (physical)
    "LU0274208692",  # Xtrackers MSCI World Swap 1C (synthetic: holdings = swap basket)
    "IE00BTJRMP35",  # Xtrackers MSCI Emerging Markets 1C
    "LU1681043599",  # Amundi MSCI World (synthetic)
    "IE000BI8OT95",  # Amundi Core MSCI World (physical)
    "IE00BK5BQT80",  # Vanguard FTSE All-World Acc
    "IE00BFY0GT14",  # SPDR MSCI World
]
SAMPLE_STOCK = "US0378331005"  # a constituent, for OpenFIGI and GLEIF


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # report the redirect instead of following it


OPENER = urllib.request.build_opener(_NoRedirect)


def classify(body: bytes, content_type: str) -> dict:
    """What a body is: xlsx, json (non-empty?), csv (rows, ISIN column), html or other."""
    if body[:2] == b"PK":
        return {"kind": "xlsx/zip"}
    text = body.decode("utf-8-sig", errors="replace").lstrip()
    if text[:1] == "<" or "html" in content_type:
        return {"kind": "html"}
    if text[:1] in "{[":
        try:
            data = json.loads(text)
        except ValueError:
            return {"kind": "json?"}
        return {"kind": "json" if data else "json (empty)"}
    lines = [line for line in text.splitlines() if line.strip()]
    # iShares puts a few lines of fund details above the header: the header is the first wide line.
    start = next((i for i, line in enumerate(lines) if line.count(",") >= 5 or line.count(";") >= 5), None)
    if start is None:
        return {"kind": "other"}
    delimiter = ";" if lines[start].count(";") > lines[start].count(",") else ","
    rows = list(csv.reader(lines[start:], delimiter=delimiter))
    header = [h.strip().lower() for h in rows[0]]
    return {"kind": "csv", "rows": len(rows) - 1, "isin": any("isin" in h for h in header),
            "header": ",".join(rows[0])[:120]}


def probe(name: str, url: str, *, data: dict | None = None, agent: str = BROWSER) -> dict:
    headers = {"User-Agent": agent, "Accept": "*/*"}
    body = json.dumps(data).encode() if data is not None else None
    if body is not None:
        headers["Content-Type"] = "application/json"
    started = time.monotonic()
    try:
        request = urllib.request.Request(url, data=body, headers=headers)  # noqa: S310 - fixed https URLs only
        with OPENER.open(request, timeout=TIMEOUT) as r:
            status, ctype, location, payload = r.status, r.headers.get("Content-Type", ""), "", r.read()
    except urllib.error.HTTPError as e:
        status, ctype, payload = e.code, e.headers.get("Content-Type", ""), e.read()
        location = e.headers.get("Location", "")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return {"source": name, "status": f"error: {getattr(e, 'reason', e)}"}
    return {"source": name, "status": status, "redirect": location, "type": ctype.split(";")[0],
            "bytes": len(payload), "seconds": round(time.monotonic() - started, 1), **classify(payload, ctype)}


def ishares(isin: str) -> list[dict]:
    product, slug, ticker = ISHARES[isin]
    url = (f"https://www.ishares.com/uk/individual/en/products/{product}/{slug}/1506575576011.ajax"
           f"?fileType=csv&fileName={ticker}_holdings&dataType=fund")
    return [probe("iShares", url), probe("iShares (python UA)", url, agent="Python-urllib")]


def amundi(isin: str) -> dict:
    return probe("Amundi", "https://www.amundietf.fr/mapi/ProductAPI/getProductsData", data={
        "context": {"countryCode": "FRA", "languageCode": "fr", "userProfileName": "RETAIL"},
        "productIds": [isin],
        "productType": "PRODUCT",
        "characteristics": ["ISIN", "INDEX_BREAKDOWNS_AS_OF_DATE", "IS_NO_INDEX_BREAKDOWN"],
        "breakDown": {"aggregationFields": ["INDEX_COUNTRIES", "INDEX_SECTORS"]},
        "composition": {"compositionFields": ["date", "isin", "name", "weight", "currency", "sector", "country"]},
    })


def checks(isin: str) -> list[dict]:
    found = ishares(isin) if isin in ISHARES else []
    found.append(probe("DWS xlsx", f"https://etf.dws.com/etfdata/export/GBR/ENG/excel/product/constituent/{isin}/"))
    found.append(amundi(isin))
    return found


def once() -> list[dict]:
    return [
        probe("Vanguard gpx", "https://www.vanguard.co.uk/gpx/graphql", data={"query": "{__typename}"}),
        probe("OpenFIGI", "https://api.openfigi.com/v3/mapping", data=[{"idType": "ID_ISIN", "idValue": SAMPLE_STOCK}]),
        probe("GLEIF", f"https://api.gleif.org/api/v1/lei-records?filter[isin]={SAMPLE_STOCK}",
              agent="brokersync-probe"),
    ]


COLUMNS = ["isin", "source", "status", "redirect", "type", "bytes", "seconds", "kind", "rows", "isin_col", "header"]


def row(isin: str, result: dict) -> str:
    result = {**result, "isin": isin, "isin_col": result.get("isin", "")}
    return "| " + " | ".join(str(result.get(c, "")).replace("|", "/") for c in COLUMNS) + " |"


def main(isins: list[str]) -> None:
    print("| " + " | ".join(COLUMNS) + " |")
    print("|" + "---|" * len(COLUMNS))
    for result in once():
        print(row("-", result), flush=True)
    for isin in isins:
        for result in checks(isin.strip().upper()):
            print(row(isin, result), flush=True)


if __name__ == "__main__":
    main(sys.argv[1:] or DEFAULT)
