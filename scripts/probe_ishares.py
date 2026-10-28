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

for ls in (38, 4):
    r = get(f"lg-lit-{ls}.json", f"{B}/srp/api/literature-structure/{ls}/?audience=79&share_classes=893&language=1")
    if r is None or r.status_code != 200:
        continue
    print("   ", r.text[:1500])
    try:
        rows = r.json().get("data") or []
    except ValueError:
        continue
    for row in rows:
        q = "&".join([f"lit_types={t}" for t in row.get("lit_types", [])])
        d = get("", f"{B}/srp/api/documents/?audience=79&share_classes=893&language=1&{q}&date_from=")
        if d is not None and d.status_code == 200:
            hit = [x for x in re.findall(r"documents-id/[0-9a-f-]{36}/[^\"']+", d.text)]
            print("     ", row.get("display_name"), row.get("lit_types"), hit[:4])
