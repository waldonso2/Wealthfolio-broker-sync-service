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

for u in ("/en/uk/institutional/fund-centre/ETF/Cyber-Security/IE00BYPLS672/", "/en/uk/institutional/fund-centre/ETF/Cyber-Security/"):
    r = get("lg-scpage.html", B + u)
    t = r.text
    for w in ("9f3a4ebd", "Fundholdings", "fundholdings", "holdings", "pageSections", "partId"):
        ms = list(re.finditer(w, t))
        print("  ", w, len(ms))
        for m in ms[:2]:
            print("     ..", t[max(0, m.start() - 300):m.end() + 300].replace("\n", " "))
    break
raise SystemExit
