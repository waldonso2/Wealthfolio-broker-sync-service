"""Temporary probe: where other issuers publish an ETF's holdings."""
import re
import httpx

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
http = httpx.Client(timeout=60, follow_redirects=True, headers={"User-Agent": UA, "Accept-Language": "de-DE,de;q=0.9,en;q=0.8"})
KEY = re.compile(r'(?:https?:)?//[^"\'\s<>]*?(?:holding|constituent|composition|xlsx|\.csv|/api/|graphql|mapi|fund-data|download)[^"\'\s<>]*|"/[^"\s<>]*?(?:holding|constituent|composition|xlsx|\.csv|/api/|graphql|mapi|fund-data|download)[^"\s<>]*', re.I)

def show(label, method="GET", url="", **kw):
    try:
        r = http.request(method, url, **kw)
    except httpx.HTTPError as e:
        print(f"## {label}: {type(e).__name__}")
        return None
    ct = r.headers.get("content-type", "")
    print(f"## {label}: {r.status_code} {ct} {len(r.content)} -> {str(r.url)[:160]}")
    if "html" in ct:
        links = sorted(set(m.group(0).strip('"') for m in KEY.finditer(r.text)))
        for l in links[:40]:
            print("   link", l[:220])
    else:
        print("   body", r.content[:400])
    return r

# SPDR
show("spdr finder de", url="https://www.ssga.com/bin/v1/ssmp/fund/fundfinder?country=de&language=de&role=individual&product=etfs&ui=fund-finder")
for t in ("spy5-gy", "spy5", "spy5-ln", "spyl-gy"):
    show(f"spdr xlsx {t}", url=f"https://www.ssga.com/library-content/products/fund-data/etfs/emea/holdings-daily-emea-en-{t}.xlsx")
show("spdr page", url="https://www.ssga.com/de/de/individual/etfs/state-street-spdr-sp-500-ucits-etf-acc-spy5-gy")
# Vanguard
show("vanguard uk list", url="https://www.vanguard.co.uk/professional/product")
show("vanguard uk page", url="https://www.vanguard.co.uk/professional/product/etf/equity/9505/ftse-all-world-ucits-etf-usd-accumulating")
show("vanguard de page", url="https://www.de.vanguard/professionell/anlageprodukte/etf/aktien/9505/ftse-all-world-ucits-etf-usd-thesaurierend")
# Xtrackers
show("dws xlsx", url="https://etf.dws.com/etfdata/export/GBR/ENG/excel/product/constituent/IE00BMFKG444/")
show("dws xlsx DEU", url="https://etf.dws.com/etfdata/export/DEU/DEU/excel/product/constituent/IE00BJ0KDQ92/")
# Amundi
body = {"context": {"countryCode": "DEU", "languageCode": "de", "userProfileName": "RETAIL"}, "productIds": ["LU1681043599"],
        "productType": "PRODUCT", "composition": {"compositionFields": ["date", "type", "bbg", "isin", "name", "weight", "quantity", "currency", "sector", "country", "countryOfRisk"]}}
show("amundi mapi", "POST", "https://www.amundietf.de/mapi/ProductAPI/getProductsData", json=body)
body["productIds"] = ["IE000BI8OT95"]
show("amundi mapi core", "POST", "https://www.amundietf.de/mapi/ProductAPI/getProductsData", json=body)
show("amundi page", url="https://www.amundietf.de/de/privatanleger/products/equity/amundi-msci-world-ucits-etf-eur-c/lu1681043599")
# Invesco
for kind in ("index", "fund"):
    show(f"invesco {kind}", url=f"https://dng-api.invesco.com/cache/v1/accounts/en_GB/shareclasses/IE0032077012/holdings/{kind}?idType=isin&productType=ETF")
show("invesco page", url="https://www.invesco.com/uk/en/financial-products/etfs/invesco-eqqq-nasdaq-100-ucits-etf-dist.html")
# L&G
show("lgim page", url="https://www.lgim.com/uk/en/capabilities/etfs/fund-centre/?isin=IE00BYPLS672")
show("lgim fundcentre", url="https://fundcentres.lgim.com/uk/en/fund-centre/ETF/Cyber-Security/")
# VanEck
show("vaneck page", url="https://www.vaneck.com/de/de/investments/semiconductor-etf/holdings/")
# WisdomTree
show("wisdomtree page", url="https://www.wisdomtree.eu/de-de/etfs/equity/wisdomtree-global-quality-dividend-growth-ucits-etf-usd-acc")
# HSBC
show("hsbc page", url="https://www.assetmanagement.hsbc.de/de/privatanleger/fund-centre/IE00B4X9L533")
# extraETF / justETF
show("extraetf", url="https://extraetf.com/api-v3/etf/holdings/?isin=IE00B8GKDB10")
show("justetf", url="https://www.justetf.com/de/etf-profile.html?isin=IE00B8GKDB10")
