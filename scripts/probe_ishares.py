"""Temporary probe round 29: where each issuer's answers carry the TER."""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, "src")
import httpx  # noqa: E402

from brokersync import lookthrough as l  # noqa: E402

OUT = Path("probe-files"); OUT.mkdir(exist_ok=True)
UA = l.BROWSER
http = httpx.Client(timeout=90, follow_redirects=True, headers={"User-Agent": UA, "Accept-Language": "de-DE,de;q=0.9"})
PAT = re.compile(r"ter\b|fee|charge|expense|ocf|kosten|ongoing|tcr|gesamtkosten", re.I)


def get(name, url, **kw):
    try:
        r = http.request("POST" if "json" in kw else "GET", url, **kw)
    except httpx.HTTPError as e:
        print("##", name, type(e).__name__); return None
    print("##", name, r.status_code, r.headers.get("content-type", ""), len(r.content), str(r.url)[:160])
    (OUT / name).write_bytes(r.content)
    return r


def keys(obj, path=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}"
            if PAT.search(str(k)):
                print("   KEY", p, "=", json.dumps(v)[:200])
            keys(v, p)
    elif isinstance(obj, list):
        for v in obj[:2]:
            keys(v, path + "[]")


def text(t, n=4):
    for m in list(re.finditer(r"TER|Gesamtkostenquote|Laufende Kosten|Ongoing charges|Total expense", t))[:n]:
        print("   TXT ..", re.sub(r"<[^>]+>", " ", t[max(0, m.start() - 150):m.end() + 250]).replace("\n", " ")[:400])


# iShares screener: the first row's TER-like keys
r = get("ish.json", l.ISHARES_SCREENERS[0])
try:
    rows = r.json()
    first = next(iter(rows.values())) if isinstance(rows, dict) else rows[0]
    print("   ishares keys", sorted(first)[:80]); keys(first)
    for code, product, row in l.finder_rows(rows):
        if code == "IE00B4L5Y983":
            keys(row); print("   ter()", l._ter(row)); break
except Exception as e:
    print("   ishares", type(e).__name__, e)
# SPDR finder
r = get("spdr.json", l.SPDR_FINDER)
try:
    d = r.json()["data"]["funds"]["etfs"]["datas"]
    print("   spdr keys", sorted(d[0])); keys(d[0])
except Exception as e:
    print("   spdr", type(e).__name__, e)
# Amundi with likely characteristics
for ch in (["TER"], ["ONGOING_CHARGES"], ["TER", "ONGOING_CHARGES", "MANAGEMENT_FEE", "TOTAL_EXPENSE_RATIO", "FEES"]):
    body = {"context": {"countryCode": "DEU", "languageCode": "de", "userProfileName": "RETAIL"},
            "productIds": ["LU1681043599"], "productType": "PRODUCT", "characteristics": ch}
    r = get("amundi.json", l.AMUNDI, json=body)
    if r is not None:
        print("   ", r.text[:600])
# Invesco shareclass
for u in ("https://dng-api.invesco.com/cache/v1/accounts/en_GB/shareclasses/IE00B60SX394?idType=isin",
          "https://dng-api.invesco.com/cache/v1/accounts/en_GB/shareclasses/IE00B60SX394/details?idType=isin"):
    r = get("invesco.json", u)
    if r is not None and r.status_code == 200:
        try:
            keys(r.json())
        except ValueError:
            print("   ", r.text[:300])
# Vanguard GraphQL: guess fields, errors name valid ones
for q in ('{ funds(portIds: ["9505"]) { portId profile { ocf expenseRatio } } }',
          '{ funds(portIds: ["9505"]) { portId profile { fundFeesAndCharges { ocf } } } }',
          '{ funds(portIds: ["9505"]) { portId costs { ocf } } }',
          '{ __type(name: "FundProfile") { fields { name } } }',
          '{ __type(name: "Fund") { fields { name } } }'):
    r = get("vg.json", l.VANGUARD, json={"query": q}, headers=l.VANGUARD_HEADERS)
    if r is not None:
        print("   ", r.text[:900])
# Xtrackers: the xlsx's first rows, and the product page
r = get("dws.xlsx", l.DWS.format(isin="IE00BJ0KDQ92"))
if r is not None and r.content[:2] == b"PK":
    for row in l.xlsx_rows(r.content)[:6]:
        print("   dws", row[:8])
r = get("dws.html", "https://etf.dws.com/de-de/IE00BJ0KDQ92-msci-world-ucits-etf-1c/")
if r is not None: text(r.text)
# VanEck / ARK pages
r = get("vaneck.html", "https://www.vaneck.com/de/de/anlagen/gold-miners-etf/ueberblick/")
if r is not None: text(r.text)
r = get("ark.html", l.ARK + "/de/funds/active-etfs/artificial-intelligence-robotics/")
if r is not None: text(r.text)
r = get("hanetf.html", "https://hanetf.com/wp-content/assets/upload/productlist-all-all-all-all.xlsx")
