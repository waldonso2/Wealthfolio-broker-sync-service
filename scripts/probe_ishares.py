"""Temporary probe round 3: save raw public answers to probe-files/ for offline reading."""
import re
from pathlib import Path
import httpx

OUT = Path("probe-files"); OUT.mkdir(exist_ok=True)
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
http = httpx.Client(timeout=60, follow_redirects=True, headers={"User-Agent": UA, "Accept-Language": "de-DE,de;q=0.9,en;q=0.8"})

def save(name, url, **kw):
    try:
        r = http.request(kw.pop("method", "GET"), url, **kw)
    except httpx.HTTPError as e:
        print(name, url, type(e).__name__); return None
    print(name, r.status_code, r.headers.get("content-type", ""), len(r.content), str(r.url)[:200])
    (OUT / name).write_bytes(r.content)
    return r

save("spdr-finder.json", "https://www.ssga.com/bin/v1/ssmp/fund/fundfinder?country=de&language=de&role=individual&product=etfs&ui=fund-finder")
save("spdr-spy5.xlsx", "https://www.ssga.com/library-content/products/fund-data/etfs/emea/holdings-daily-emea-en-spy5-gy.xlsx")
save("dws.xlsx", "https://etf.dws.com/etfdata/export/DEU/DEU/excel/product/constituent/IE00BMFKG444/")
for isin in ("LU1681043599", "IE000BI8OT95"):
    body = {"context": {"countryCode": "DEU", "languageCode": "de", "userProfileName": "RETAIL"}, "productIds": [isin],
            "productType": "PRODUCT", "composition": {"compositionFields": ["date", "type", "bbg", "isin", "name", "weight", "quantity", "currency", "sector", "country", "countryOfRisk"]},
            "characteristics": ["FUND_REPLICATION_METHODOLOGY", "SHARE_MARKETING_NAME", "TER"]}
    save(f"amundi-{isin}.json", "https://www.amundietf.de/mapi/ProductAPI/getProductsData", method="POST", json=body)
r = save("vanguard-page.html", "https://www.vanguard.co.uk/professional/product/etf/equity/9505/ftse-all-world-ucits-etf-usd-accumulating")
for i, s in enumerate(sorted(set(re.findall(r'<script[^>]+src="([^"]+)"', r.text)))):
    u = s if s.startswith("http") else "https://www.vanguard.co.uk/" + s.lstrip("/")
    try:
        js = http.get(u)
    except httpx.HTTPError:
        continue
    if "olding" in js.text:
        (OUT / f"vanguard-{i}.js").write_bytes(js.content); print("js", u, len(js.content))
save("vanguard-model.json", "https://www.vanguard.co.uk/content/uk-aem/gb/en/professional/product/etf-equity.model.json")
save("invesco-page.html", "https://www.invesco.com/uk/en/financial-products/etfs/invesco-eqqq-nasdaq-100-ucits-etf-dist.html")
save("invesco-holdings-cache.json", "https://dng-api.invesco.com/cache/v1/accounts/en_GB/shareclasses/IE0032077012/holdings/index?idType=isin&productType=ETF&productCode=ETF")
save("lgim-page.html", "https://fundcentres.landg.com/en/uk/institutional/fund-centre/ETF/Cyber-Security/")
save("lgim-api.json", "https://fundcentres.landg.com/srp/api/fund-centre?search=IE00BYPLS672&audience_id=79&language_id=1&litStructureId=1")
save("vaneck-page.html", "https://www.vaneck.com/de/de/anlagen/semiconductor-etf/positionen")
save("hsbc-home.html", "https://www.assetmanagement.hsbc.de/")
