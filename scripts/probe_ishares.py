"""Temporary probe round 7."""
import json
import re
from pathlib import Path
import httpx

OUT = Path("probe-files"); OUT.mkdir(exist_ok=True)
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
http = httpx.Client(timeout=90, follow_redirects=True, headers={"User-Agent": UA, "Accept-Language": "de-DE,de;q=0.9,en;q=0.8"})

def save(name, url, **kw):
    try:
        r = http.request(kw.pop("method", "GET"), url, **kw)
    except httpx.HTTPError as e:
        print(name, url, type(e).__name__); return None
    print(name, r.status_code, r.headers.get("content-type", ""), len(r.content), str(r.url)[:200])
    if name: (OUT / name).write_bytes(r.content)
    return r

GQL = "https://www.vanguard.co.uk/gpx/graphql"
ids_q = 'query($portIds: [String!]!) { funds(portIds: $portIds) { portId profile { fundFullName identifiers(altIds: ["ISIN"]) { altIdCode altIdValue } } } }'
found = {}
for lo in range(0, 10000, 1000):
    r = save("", GQL, method="POST", json={"query": ids_q, "variables": {"portIds": [f"{n:04d}" for n in range(lo, lo + 1000)]}},
             headers={"X-Consumer-ID": "uk2"})
    try:
        for f in (r.json().get("data") or {}).get("funds") or []:
            for i in (f.get("profile") or {}).get("identifiers") or []:
                found[i["altIdValue"]] = (f["portId"], f["profile"]["fundFullName"])
    except Exception as e:
        print("err", lo, e, r.text[:200] if r is not None else "")
print("vanguard isins", len(found), list(found.items())[:5])
(OUT / "vanguard-isins.json").write_text(json.dumps(found))
for isin in ("IE00B8GKDB10",):
    print("high div", found.get(isin))
# VanEck holdings block and the fund list
save("vaneck-holdings.json", "https://www.vaneck.com/Main/HoldingsBlock/GetContent/?blockid=194732&pageid=233164&ticker=UCTSMH")
save("vaneck-fundlisting.html", "https://www.vaneck.com/de/de/fundlisting/")
# L&G fund explorer module
js = save("lgim-explorer.js", "https://fundcentres.landg.com/assets/bundles/versions/26.10-195-4963d446/lgim/js/fundExplorer.js")
if js is not None:
    print(sorted(set(re.findall(r'["\'`](/srp/[^"\'`]+|/api/[^"\'`]+)["\'`]', js.text)))[:40])
