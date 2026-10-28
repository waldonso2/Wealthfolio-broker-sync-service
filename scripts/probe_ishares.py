"""Temporary probe round 5."""
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

js = save("vanguard-92755.js", "https://www.vanguard.co.uk/92755.74374ac7c10596a7.js")
rt = http.get("https://www.vanguard.co.uk/runtime.46f27ca601d8f023.js").text
chunks = dict(re.findall(r'(\d+):"([0-9a-f]{16})"', rt))
# chunks the app names together with holdings
hits = 0
for cid, h in chunks.items():
    try:
        c = http.get(f"https://www.vanguard.co.uk/{cid}.{h}.js")
    except httpx.HTTPError:
        continue
    if re.search(r"(?i)fund-?holdings|holdingDetails|borHoldings|FundHoldings", c.text):
        (OUT / f"vanguard-chunk-{cid}.js").write_bytes(c.content); hits += 1
print("vanguard chunks with holdings", hits)
for isin in ("IE0032077012", "IE00B3YCGJ38"):
    for kind in ("fund", "index"):
        save(f"invesco-{isin}-{kind}.json", f"https://dng-api.invesco.com/cache/v1/accounts/en_GB/shareclasses/{isin}/holdings/{kind}?idType=isin")
save("vaneck-bundle.js", "https://www.vaneck.com/static/ReactApps/FundPagesNewDesignEU/dist/bundle.js")
save("vaneck-common.js", "https://www.vaneck.com/Static/us/js/dist/fund-pages-common.js")
lg = save("lgim-page.html", "https://fundcentres.landg.com/en/uk/institutional/fund-centre/ETF/Cyber-Security/")
for s in sorted(set(re.findall(r'<script[^>]+src="([^"]+)"', lg.text)))[:15]:
    u = s if s.startswith("http") else "https://fundcentres.landg.com" + s
    r = save("", u)
    if r is not None and ("srp/api" in r.text or "holding" in r.text.lower()):
        (OUT / ("lgim-" + re.sub(r"[^a-z0-9.]", "_", s.lower())[-60:])).write_bytes(r.content)
