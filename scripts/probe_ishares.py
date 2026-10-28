"""Temporary probe round 27: UBS ETF page -> where the constituents download comes from."""
import re
from pathlib import Path
from urllib.parse import urljoin
import httpx

OUT = Path("probe-files"); OUT.mkdir(exist_ok=True)
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
http = httpx.Client(timeout=90, follow_redirects=True, headers={"User-Agent": UA, "Accept-Language": "de-CH,de;q=0.9",
                                                               "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"})
WORDS = ("Constituents", "constituents", "Holdings", "holdings", "UBSFunds", "/api/", "\\.json", "fundgate", "xls")


def get(name, url):
    try:
        r = http.get(url)
    except httpx.HTTPError as e:
        print("##", name, url, type(e).__name__); return None
    print("##", name, r.status_code, r.headers.get("content-type", ""), len(r.content), str(r.url)[:200])
    if name: (OUT / name).write_bytes(r.content)
    return r


def scan(t):
    for w in WORDS:
        ms = list(re.finditer(w, t))
        print("  ", w, len(ms))
        for m in ms[:5]:
            print("     ..", t[max(0, m.start() - 250):m.end() + 300].replace("\n", " "))


r = get("ubs-page.html", "https://www.ubs.com/ch/de/assetmanagement/funds/etf/"
        "lu0629459743-ubs-msci-world-socially-responsible-ucits-etf-pd001.html")
if r is not None and r.status_code == 200:
    scan(r.text)
    scripts = sorted(set(re.findall(r'<script[^>]+src="([^"]+)"', r.text)))
    print("scripts", scripts)
    for n, s in enumerate(scripts[:25]):
        j = get(f"ubs-js-{n}.js", urljoin(str(r.url), s))
        if j is not None and j.status_code == 200 and any(w in j.text for w in ("onstituents", "oldings")):
            print("== script", s); scan(j.text)
else:
    print(r.text[:1500] if r is not None else "")
