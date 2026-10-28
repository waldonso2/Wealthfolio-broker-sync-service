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

for fc, aud in ((48, 132), (48, 117), (47, 79)):
    r = get(f"lg-fc-{fc}-{aud}.json", f"{B}/srp/api/fund-centre/{fc}?audience={aud}&language=1")
    if r is None or r.status_code != 200:
        continue
    t = r.text
    print("   ", fc, aud, "ETF", t.count("UCITS ETF"), "isin", "IE00BYPLS672" in t, "fundholdings", t.lower().count("fundholdings"),
          "docid", "9f3a4ebd" in t)
    i = t.lower().find("fundholdings")
    if i >= 0:
        print("   ..", t[max(0, i - 600):i + 200])
