"""Temporary probe round 15: L&G fund centre code."""
import html
import re
from pathlib import Path
import httpx

OUT = Path("probe-files"); OUT.mkdir(exist_ok=True)
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
http = httpx.Client(timeout=90, follow_redirects=True, headers={"User-Agent": UA, "Accept-Language": "en-GB,en;q=0.9"})

def save(name, url, **kw):
    try:
        r = http.request(kw.pop("method", "GET"), url, **kw)
    except httpx.HTTPError as e:
        print("##", name, url, type(e).__name__); return None
    print("##", name, r.status_code, r.headers.get("content-type", ""), len(r.content), str(r.url)[:200])
    if name: (OUT / name).write_bytes(r.content)
    return r

base = "https://fundcentres.landg.com"
p = save("lg-cyber.html", base + "/en/uk/institutional/fund-centre/ETF/Cyber-Security/")
for s in sorted(set(re.findall(r'<script[^>]+src="([^"]+)"', p.text))):
    u = s if s.startswith("http") else base + s
    r = save("", u)
    if r is not None and "landg" in u:
        (OUT / ("lg-" + re.sub(r"[^a-z0-9.]", "_", u.lower())[-70:])).write_bytes(r.content)
        for m in re.findall(r'import\("?\.?/?([^")]+\.js)', r.text)[:40]:
            print("   import", m)
ex = save("lg-explorer.js", base + "/assets/bundles/versions/26.10-195-4963d446/lgim/js/fundExplorer.js")
# the chunks the explorer imports
for c in sorted(set(re.findall(r'["\'](\./[A-Za-z0-9_.-]+\.js)["\']', ex.text if ex is not None else ""))):
    save("lg-chunk-" + c[2:], base + "/assets/bundles/versions/26.10-195-4963d446/lgim/js/" + c[2:])
