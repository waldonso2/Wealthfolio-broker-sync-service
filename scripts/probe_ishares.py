"""Temporary probe round 6."""
import json
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

# Vanguard: port ids from the product list, ISINs and holdings over GraphQL
page = http.get("https://www.vanguard.co.uk/professional/product").text
ports = sorted(set(re.findall(r"/product/(?:etf|mf)/[a-z-]+/(\d{4})/", page)))
print("vanguard ports", len(ports), ports[:20])
GQL = "https://www.vanguard.co.uk/gpx/graphql"
ids_q = 'query($portIds: [String!]!) { funds(portIds: $portIds) { portId profile { fundFullName identifiers(altIds: ["ISIN"]) { altIdCode altIdValue } } } }'
hold_q = ('query($portIds: [String!], $lastItemKey: String) { borHoldings(portIds: $portIds) { holdings(limit: 1500, lastItemKey: $lastItemKey) '
          '{ items { issuerName securityLongDescription marketValuePercentage ticker sedol1 securityType bloombergIsoCountry gicsSectorDescription } totalHoldings lastItemKey } } }')
daily_q = ('query($portIds: [String!], $lastItemKey: String) { borHoldings(portIds: $portIds) { dailyHoldings(limit: 1500, lastItemKey: $lastItemKey) '
           '{ items { securityLongDescription securityType isin marketValuePercentage } totalHoldings lastItemKey } } }')
for hdr in ({}, {"X-Consumer-ID": "uk2"}):
    save(f"vanguard-ids-{len(hdr)}.json", GQL, method="POST", json={"query": ids_q, "variables": {"portIds": ports[:300] or ["9505"]}}, headers=hdr)
save("vanguard-hold.json", GQL, method="POST", json={"query": hold_q, "variables": {"portIds": ["9505"]}}, headers={"X-Consumer-ID": "uk2"})
save("vanguard-daily.json", GQL, method="POST", json={"query": daily_q, "variables": {"portIds": ["9505"]}}, headers={"X-Consumer-ID": "uk2"})
save("vanguard-de-ids.json", "https://www.de.vanguard/gpx/graphql", method="POST", json={"query": ids_q, "variables": {"portIds": ["9505", "9510"]}}, headers={"X-Consumer-ID": "de2"})
# VanEck: chunks of the fund-page app, and the holdings block on the page
b = http.get("https://www.vaneck.com/static/ReactApps/FundPagesNewDesignEU/dist/bundle.js").text
cids = sorted(set(re.findall(r"(\d{3,4}):1", b)))
for cid in cids:
    try:
        c = http.get(f"https://www.vaneck.com/static/ReactApps/FundPagesNewDesignEU/dist/{cid}.bundle.js")
    except httpx.HTTPError:
        continue
    if c.status_code == 200 and re.search(r"(?i)holding", c.text):
        (OUT / f"vaneck-{cid}.js").write_bytes(c.content)
p = save("vaneck-holdings.html", "https://www.vaneck.com/de/de/anlagen/semiconductor-etf/positionen/")
for m in re.finditer(r"<ve-holdingsblock[^>]*>|<ve-indexholdingsblock[^>]*>", p.text):
    print("block", m.group(0)[:400])
# L&G scripts
save("lgim-fund_centre.js", "https://fundcentres.landg.com/static//cms_resources/fund_centre_42.js?version=69558")
save("lgim-fundExplorer.js", "https://fundcentres.landg.com/assets/bundles/latest-pointer/lgim/js/fundExplorer.js")
