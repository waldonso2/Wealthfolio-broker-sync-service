"""Temporary probe round 14: L&G holdings CSV by document id."""
import html
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
        print("##", name, url, type(e).__name__); return None
    print("##", name, r.status_code, r.headers.get("content-type", ""), len(r.content), str(r.url)[:200],
          {k: v for k, v in r.headers.items() if k.lower() in ("last-modified", "content-disposition", "etag")})
    if name: (OUT / name).write_bytes(r.content)
    return r

r = save("lgim-holdings.csv", "https://fundcentres.lgim.com/srp/documents-id/9f3a4ebd-0038-4a92-8edd-5256eab14b58/Fundholdings.csv")
if r is not None:
    print(r.content[:1500].decode(errors="replace"))
save("lgim-holdings2.csv", "https://fundcentres.landg.com/srp/documents-id/9f3a4ebd-0038-4a92-8edd-5256eab14b58/Fundholdings.csv")
# where the id is named: fund pages of the ETF fund centre
for u in ("https://fundcentres.lgim.com/en/uk/institutional/fund-centre/ETF/Cyber-Security/",
          "https://fundcentres.landg.com/en/de/private/fund-centre/etf/"):
    p = save("", u)
    if p is not None:
        t = html.unescape(p.text)
        print("   ids", sorted(set(re.findall(r"documents-id/([0-9a-f-]{36})/([^\"'?\s]+)", t)))[:20])
