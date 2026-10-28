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

for fc, aud in ((1, 117), (4, 132), (39, 146), (40, 130), (41, 78), (710, 309), (149, 311), (1238, 123)):
    r = get("", f"{B}/srp/api/fund-centre/{fc}?audience={aud}&language=1")
    if r is None or r.status_code != 200:
        continue
    t = r.text
    print("   ", fc, "ETF names", t.count("UCITS ETF"), "isin", "IE00BYPLS672" in t, "fundholdings", t.lower().count("fundholdings"))
    if "IE00BYPLS672" in t:
        (OUT / f"lg-fc-{fc}.json").write_bytes(r.content)
