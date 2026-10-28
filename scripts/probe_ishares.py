"""Temporary probe round 28: L&G fund page -> page sections -> parts -> holdings document."""
import html
import json
import re
from pathlib import Path
from urllib.parse import urljoin
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
    print("##", name, r.status_code, r.headers.get("content-type", ""), len(r.content), str(r.url)[:250])
    if name: (OUT / name).write_bytes(r.content)
    return r


def hits(t):
    return sorted(set(re.findall(r"[^\"'\s]*(?:documents-id|Fundholdings|fundholdings)[^\"'\s]*", t)))[:10]


fc = get("lg-fc47.json", f"{B}/srp/api/fund-centre/47?audience=79&language=1")
t = fc.text
for m in list(re.finditer("Cyber", t))[:3]:
    print("  ..", t[max(0, m.start() - 600):m.end() + 600])
urls = sorted(set(u for u in re.findall(r'"fundPageURL":\s*"([^"]+)"', t) if "yber" in u))
print("fund pages", urls)
for u in urls[:1]:
    p = get("lg-fundpage.html", urljoin(B, u))
    page = p.text
    route = re.search(r'data-route-id="(\d+)"', page)
    print("route", route and route.group(1), "hits", hits(page))
    for m in re.finditer(r"data-config='(.*?)'\s", page, re.S):
        try:
            c = json.loads(html.unescape(m.group(1)))
        except ValueError:
            continue
        if "pageSections" not in c:
            continue
        print("config keys", sorted(c))
        print("fund", {k: c.get(k) for k in ("fundId", "banner", "shareClassesPart", "infoPartId", "routeId")})
        for s in c["pageSections"]:
            print("  section", json.dumps(s)[:300])
        fund = c.get("fundId")
        for s in c["pageSections"]:
            pid = s.get("data") if isinstance(s.get("data"), int) else s.get("partId")
            if not pid:
                continue
            for sc in ("", "&share_class_id=893"):
                q = f"{B}/srp/api/part?id={pid}&audience=79&route={route and route.group(1)}&version=live&languageId=1&fund_id={fund}{sc}"
                r = get(f"lg-part-{pid}{'-sc' if sc else ''}.txt", q)
                if r is not None:
                    print("     ", s.get("key"), "hits", hits(r.text), r.text[:200].replace("\n", " "))
