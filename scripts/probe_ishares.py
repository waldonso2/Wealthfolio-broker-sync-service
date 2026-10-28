"""Temporary probe round 30: TER at Invesco, Xtrackers, Vanguard, VanEck."""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, "src")
import httpx  # noqa: E402

from brokersync import lookthrough as l  # noqa: E402

OUT = Path("probe-files"); OUT.mkdir(exist_ok=True)
http = httpx.Client(timeout=90, follow_redirects=True, headers={"User-Agent": l.BROWSER, "Accept-Language": "de-DE,de;q=0.9"})
PAT = re.compile(r"ter\b|fee|charge|expense|ocf|kosten|ongoing|tcr", re.I)


def get(name, url, **kw):
    try:
        r = http.request("POST" if "json" in kw else "GET", url, **kw)
    except httpx.HTTPError as e:
        print("##", name, type(e).__name__); return None
    print("##", name, r.status_code, r.headers.get("content-type", ""), len(r.content), str(r.url)[:160])
    (OUT / name).write_bytes(r.content)
    return r


def keys(obj, path=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}"
            if PAT.search(str(k)):
                print("   KEY", p, "=", json.dumps(v)[:200])
            keys(v, p)
    elif isinstance(obj, list):
        for v in obj[:2]:
            keys(v, path + "[]")


def text(t, n=4):
    for m in list(re.finditer(r"TER\b|Gesamtkostenquote|Laufende Kosten|Ongoing charges|Total expense|OCF", t))[:n]:
        print("   TXT ..", re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", t[max(0, m.start() - 100):m.end() + 200]))[:300])


r = get("invesco.json", "https://dng-api.invesco.com/cache/v1/accounts/en_GB/shareclasses/IE00B60SX394?idType=isin")
if r is not None:
    print("   ", r.text[:3000])
for u in ("https://etf.dws.com/api/pdp/de-de/etf/IE00BJ0KDQ92", "https://etf.dws.com/api/pdp/de-de/IE00BJ0KDQ92",
          "https://etf.dws.com/api/de-de/products/IE00BJ0KDQ92", "https://etf.dws.com/api/products/IE00BJ0KDQ92"):
    r = get("dws.json", u)
    if r is not None and r.status_code == 200:
        print("   ", r.text[:500])
        try:
            keys(r.json())
        except ValueError:
            pass
r = get("dwsnuxt.html", "https://etf.dws.com/de-de/IE00BJ0KDQ92-msci-world-ucits-etf-1c/")
if r is not None:
    print("   urls", sorted(set(re.findall(r'https://etf\.dws\.com/api/[^"\s]+', r.text)))[:20])
    print("   js", sorted(set(re.findall(r'/_nuxt/[\w.-]+\.js', r.text)))[:10])
for q in ('{ funds(portIds: ["9505"]) { portId profile { fundFact { ocf } } } }',
          '{ funds(portIds: ["9505"]) { portId profile { ocfValue } } }',
          '{ funds(portIds: ["9505"]) { portId profile { totalExpenseRatio } } }',
          '{ funds(portIds: ["9505"]) { portId profile { fees { ocf } } } }',
          '{ funds(portIds: ["9505"]) { portId profile { primaryExchange expenseRatioDate ongoingCharge ocfFee } } }',
          '{ funds(portIds: ["9505"]) { portId fees { ocf } } }',
          '{ funds(portIds: ["9505"]) { portId ocf } }'):
    r = get("vg.json", l.VANGUARD, json={"query": q}, headers=l.VANGUARD_HEADERS)
    if r is not None:
        print("   ", re.sub(r"\s+", " ", r.text)[:500])
r = get("vgpage.html", "https://www.vanguard.co.uk/professional/product/etf/equity/9505/ftse-all-world-ucits-etf-usd-accumulating")
if r is not None: text(r.text)
fund = l.PRODUCTS.get("VanEck", {}).get("IE00BQQP9F84") or next(iter(l.PRODUCTS.get("VanEck", {}).values()), "")
print("   vaneck id", fund)
r = get("vanecklist.html", l.VANECK_LIST)
if r is not None:
    links = sorted(set(re.findall(r'/de/de/anlagen/[\w-]+/', r.text)))[:3]
    print("   links", links)
    for link in links[:1]:
        p = get("vaneckpage.html", l.VANECK + link + "ueberblick/")
        if p is not None: text(p.text)
        p = get("vaneckpage2.html", l.VANECK + link)
        if p is not None: text(p.text)
