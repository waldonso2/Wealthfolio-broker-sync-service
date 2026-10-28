"""Temporary probe round 10: L&G API, HANetf, ARK Europe, UBS, WisdomTree."""
import html
import json
import re
from pathlib import Path
import httpx

OUT = Path("probe-files"); OUT.mkdir(exist_ok=True)
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
http = httpx.Client(timeout=90, follow_redirects=True, headers={"User-Agent": UA, "Accept-Language": "de-DE,de;q=0.9,en;q=0.8"})
KEY = re.compile(r'(?:https?:)?//[^"\'\s<>]*?(?:holding|constituent|composition|xlsx|\.csv|/api/|download|export)[^"\'\s<>]*|"/[^"\s<>]*?(?:holding|constituent|composition|xlsx|\.csv|/api/|download|export)[^"\s<>]*', re.I)

def save(name, url, **kw):
    try:
        r = http.request(kw.pop("method", "GET"), url, **kw)
    except httpx.HTTPError as e:
        print("##", name, url, type(e).__name__); return None
    print("##", name, r.status_code, r.headers.get("content-type", ""), len(r.content), str(r.url)[:200])
    if name: (OUT / name).write_bytes(r.content)
    if "html" in r.headers.get("content-type", ""):
        for l in sorted(set(m.group(0).strip('"') for m in KEY.finditer(html.unescape(r.text))))[:40]:
            print("   link", l[:200])
    elif r.status_code == 200:
        print("   body", r.content[:300])
    return r

r = save("ark-arki.html", "https://europe.ark-funds.com/de/funds/active-etfs/artificial-intelligence-robotics/")
t = html.unescape(r.text)
for w in ("NVIDIA", "Tesla", "TESLA", "IE000"):
    for m in list(re.finditer(w, t))[:2]: print("ARK..", t[max(0, m.start() - 250):m.end() + 150].replace("\n", " "))
save("hanetf-list.html", "https://hanetf.com/product-list/")
sm = save("hanetf-sitemap.xml", "https://hanetf.com/sitemap_index.xml")
for u in re.findall(r"<loc>([^<]+)</loc>", sm.text if sm is not None else "")[:30]:
    print("   sitemap", u)
    if "product" in u or "fund" in u or "etf" in u:
        sub = save("", u)
        if sub is not None:
            locs = re.findall(r"<loc>([^<]+)</loc>", sub.text)
            print("   ", len(locs), [l for l in locs if "defence" in l or "emqq" in l][:5])
