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


r = get("lg-fe.js", "https://fundcentres.landg.com/assets/bundles/latest-pointer/lgim/js/fundExplorer.js")
u = re.search(r'var u="([^"]+)"', r.text).group(1)
seen, todo = set(), [u]
WORDS = WORDS + ("documents/", "api/", "Holdings", "inline-document", "documentType")
while todo and len(seen) < 40:
    u = todo.pop(0)
    if u in seen: continue
    seen.add(u)
    j = get(f"lg-b{len(seen)}.js", u)
    if j is None or j.status_code != 200: continue
    print("== bundle", u); scan(j.text)
    for m in re.findall(r'["\'(](\.{0,2}/[\w./-]+\.js)["\')]', j.text):
        todo.append(urljoin(u, m))
