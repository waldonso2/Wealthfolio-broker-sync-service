"""Temporary probe round 25: L&G share class page and its scripts -> where the holdings document comes from."""
import re
from pathlib import Path
from urllib.parse import urljoin
import httpx

OUT = Path("probe-files"); OUT.mkdir(exist_ok=True)
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
http = httpx.Client(timeout=90, follow_redirects=True, headers={"User-Agent": UA, "Accept-Language": "en-GB,en;q=0.9"})
WORDS = ("9f3a4ebd", "Fundholdings", "fundholdings", "documents-id", "holdings", "srp/api", "pageSections", "partId")


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
        for m in ms[:4]:
            print("     ..", t[max(0, m.start() - 250):m.end() + 350].replace("\n", " "))


for base in ("https://fundcentres.lgim.com", "https://fundcentres.landg.com"):
    for u in ("/en/uk/institutional/fund-centre/ETF/Cyber-Security/IE00BYPLS672/",
              "/en/uk/institutional/fund-centre/ETF/Cyber-Security/"):
        r = get("lg-page.html", base + u)
        if r is None or r.status_code != 200:
            continue
        scan(r.text)
        scripts = sorted(set(re.findall(r'<script[^>]+src="([^"]+)"', r.text)))
        print("scripts", scripts)
        for n, s in enumerate(scripts):
            j = get(f"lg-js-{n}.js", urljoin(str(r.url), s))
            if j is not None and j.status_code == 200:
                print("== script", s); scan(j.text)
        raise SystemExit
