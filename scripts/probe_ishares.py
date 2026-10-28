"""Temporary probe round 4: Vanguard JS (GraphQL query), Invesco holdings variants, L&G API, VanEck holdings."""
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
    if name: (OUT / name).write_bytes(r.content)
    return r

main = save("vanguard-main.js", "https://www.vanguard.co.uk/main.a20141f28595d378.js")
save("vanguard-runtime.js", "https://www.vanguard.co.uk/runtime.46f27ca601d8f023.js")
for loc in ("en_GB", "de_DE", "en_IE"):
    for path in ("holdings/fund", "holdings/index"):
        for extra in ("", "&audienceType=Investor", "&productType=ETF&audienceType=Investor"):
            save("", f"https://dng-api.invesco.com/cache/v1/accounts/{loc}/shareclasses/IE0032077012/{path}?idType=isin{extra}")
save("invesco-de-page.html", "https://www.invesco.com/de/de/financial-products/etfs/invesco-eqqq-nasdaq-100-ucits-etf-dist.html")
for q in ("/srp/api/fund-centre?audience_id=79&language_id=1", "/srp/api/fund-centre/?audience=79&language=1&litStructureId=1",
          "/srp/api/fund-centre/funds?audience=79&language=1"):
    save("", "https://fundcentres.landg.com" + q)
save("vaneck-holdings.html", "https://www.vaneck.com/de/de/anlagen/semiconductor-etf/positionen/?tab=holdings")
save("vaneck-overview.html", "https://www.vaneck.com/de/de/anlagen/semiconductor-etf/ueberblick/")
save("hsbc-retail.html", "https://www.assetmanagement.hsbc.de/de/retail-investors/fund-centre")
