"""Temporary probe round 23: HANetf holdings and product list on etp.hanetf.com."""
from pathlib import Path
import httpx

OUT = Path("probe-files"); OUT.mkdir(exist_ok=True)
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
http = httpx.Client(timeout=90, follow_redirects=True, headers={"User-Agent": UA, "Accept-Language": "de-DE,de;q=0.9,en;q=0.8"})

def get(name, url):
    try:
        r = http.get(url)
    except httpx.HTTPError as e:
        print("##", name, url, type(e).__name__); return None
    print("##", name, r.status_code, r.headers.get("content-type", ""), len(r.content), str(r.url)[:160],
          r.headers.get("content-disposition", ""), r.content[:8])
    if name and r.status_code == 200: (OUT / name).write_bytes(r.content)
    return r

for code in ("NATO", "EMQQ", "EMQQ-Holdings-xxx"):
    get(f"hanetf-{code}.bin", f"https://etp.hanetf.com/{code}-Holdings")
for u in ("https://etp.hanetf.com/productlist-all-all-all-all", "https://etp.hanetf.com/productlist",
          "https://etp.hanetf.com/product-list", "https://etp.hanetf.com/"):
    get("hanetf-list-" + u.rsplit("/", 1)[-1] + ".bin", u)
