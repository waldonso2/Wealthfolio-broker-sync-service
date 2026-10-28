"""Temporary probe round 2."""
import json
import re
import httpx

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
http = httpx.Client(timeout=60, follow_redirects=True, headers={"User-Agent": UA, "Accept-Language": "de-DE,de;q=0.9,en;q=0.8"})

def get(url, **kw):
    try:
        r = http.request(kw.pop("method", "GET"), url, **kw)
        print(f"## {url[:150]}: {r.status_code} {r.headers.get('content-type','')} {len(r.content)}")
        return r
    except httpx.HTTPError as e:
        print(f"## {url[:150]}: {type(e).__name__}")

def around(text, pat, n=3, w=500):
    for m in list(re.finditer(pat, text))[:n]:
        print("   ..", text[max(0, m.start() - w):m.end() + w].replace("\n", " "))

# SPDR finder structure
r = get("https://www.ssga.com/bin/v1/ssmp/fund/fundfinder?country=de&language=de&role=individual&product=etfs&ui=fund-finder")
d = r.json()["data"]["funds"]["etfs"]
print("keys", list(d.keys()))
around(r.text, "IE00B6YX5C33", 2, 700)
# Vanguard: JS bundles with the holdings query
r = get("https://www.vanguard.co.uk/professional/product/etf/equity/9505/ftse-all-world-ucits-etf-usd-accumulating")
around(r.text, r"(?i)holding", 4, 300)
srcs = sorted(set(re.findall(r'<script[^>]+src="([^"]+)"', r.text)))
print("scripts", srcs[:30])
for s in srcs:
    u = s if s.startswith("http") else "https://www.vanguard.co.uk" + s
    js = get(u)
    if js is not None and "olding" in js.text:
        around(js.text, r"(?i)query\s+\w*holding\w*|holdings\s*\(|fundHoldings|borHoldings", 4, 400)
r = get("https://www.vanguard.co.uk/content/uk-aem/gb/en/professional/product/etf-equity/jcr:content/root/responsivegrid/jump_links_list_1089442245/fund_holdings.html")
if r is not None: print(r.text[:1500])
# Invesco download API
for path in ("holdings/index", "holdings/fund", "holdings"):
    for loc in ("en_GB", "de_DE"):
        r = get(f"https://dng-api.invesco.com/download/v1/accounts/{loc}/shareclasses/IE0032077012/{path}?idType=isin")
        if r is not None and r.status_code == 200: print("   body", r.content[:300])
r = get("https://www.invesco.com/uk/en/financial-products/etfs/invesco-eqqq-nasdaq-100-ucits-etf-dist.html")
around(r.text, r"dng-api\.invesco\.com[^\"&]*", 12, 120)
# L&G
r = get("https://fundcentres.landg.com/en/uk/institutional/fund-centre/ETF/Cyber-Security/")
around(r.text, r"(?i)srp/api[^\"']*", 6, 200)
around(r.text, r"(?i)holdings", 3, 300)
# VanEck
r = get("https://www.vaneck.com/de/de/anlagen/semiconductor-etf/positionen")
around(r.text, r"(?i)HoldingsBlock|GetContent|\.xlsx|\.csv|download", 6, 250)
# Amundi: one row fully, and whether an index composition exists
body = {"context": {"countryCode": "DEU", "languageCode": "de", "userProfileName": "RETAIL"}, "productIds": ["LU1681043599"],
        "productType": "PRODUCT", "composition": {"compositionFields": ["date", "type", "bbg", "isin", "name", "weight", "quantity", "currency", "sector", "country", "countryOfRisk"]},
        "characteristics": ["REPLICATION_METHOD", "INDEX_NAME", "FUND_REPLICATION_METHODOLOGY", "REPLICATION_TYPE"]}
r = get("https://www.amundietf.de/mapi/ProductAPI/getProductsData", method="POST", json=body)
p = r.json()["products"][0]
print("characteristics", p.get("characteristics"))
print("row", p["composition"]["compositionData"][:2])
# WisdomTree / HSBC
get("https://www.wisdomtree.eu/de-de/products")
r = get("https://www.assetmanagement.hsbc.de/de/privatanleger")
if r is not None: around(r.text, r"(?i)fund-centre[^\"']*", 4, 100)
