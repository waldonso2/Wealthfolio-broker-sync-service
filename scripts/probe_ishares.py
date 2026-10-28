"""Temporary probe round 16: L&G ISIN -> share class -> documents."""
import json
import re
from pathlib import Path
import httpx

OUT = Path("probe-files"); OUT.mkdir(exist_ok=True)
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
http = httpx.Client(timeout=90, follow_redirects=True, headers={"User-Agent": UA, "Accept-Language": "en-GB,en;q=0.9"})
B = "https://fundcentres.landg.com"

def get(name, url):
    try:
        r = http.get(url)
    except httpx.HTTPError as e:
        print("##", name, url, type(e).__name__); return None
    print("##", name, r.status_code, r.headers.get("content-type", ""), len(r.content), url[:200])
    if name: (OUT / name).write_bytes(r.content)
    return r

import html as H
p = get("lg-cyber.html", B + "/en/uk/institutional/fund-centre/ETF/Cyber-Security/")
for m in re.finditer(r"data-config='([^']+)'", p.text):
    try:
        c = json.loads(H.unescape(m.group(1)))
    except ValueError:
        continue
    print("config", {k: c.get(k) for k in ("type", "productType", "id", "fundCentreId", "fundId", "routeId", "umbrellaId",
                                          "audienceId", "audience_id", "audience", "languageId", "language", "url",
                                          "litStructureId", "shareClassId")})
for m in re.finditer(r'<script[^>]*type="application/json"[^>]*>(.{0,4000})', p.text, re.S):
    print("json", m.group(1)[:800])
for m in list(re.finditer(r"fundId|fund_id|umbrellaId|routeId", p.text))[:6]:
    print("..", p.text[max(0, m.start() - 200):m.end() + 200].replace("\n", " "))
raise SystemExit
