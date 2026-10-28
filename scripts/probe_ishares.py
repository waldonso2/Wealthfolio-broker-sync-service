"""Temporary probe round 31: TER at Finanzfluss, Vanguard and VanEck pages. Prints snippets only, saves no page."""
import json
import re
import sys

sys.path.insert(0, "src")
import httpx  # noqa: E402

from brokersync import lookthrough as l  # noqa: E402

http = httpx.Client(timeout=60, follow_redirects=True, headers={"User-Agent": l.BROWSER, "Accept-Language": "de-DE,de;q=0.9"})
PAT = r"TER\b|Gesamtkostenquote|OCF|[Ll]aufende Kosten|expense|ongoingCharge|\"ter\""


def get(url):
    try:
        r = http.get(url)
    except httpx.HTTPError as e:
        print("##", url, type(e).__name__); return None
    print("##", r.status_code, r.headers.get("content-type", ""), len(r.content), str(r.url)[:180])
    return r


def snip(t, n=5):
    for m in list(re.finditer(PAT, t))[:n]:
        print("   ..", re.sub(r"\s+", " ", t[max(0, m.start() - 120):m.end() + 160]).replace("\n", " ")[:300])


r = get("https://www.finanzfluss.de/robots.txt")
if r is not None:
    print(r.text[:1500])
for isin in ("lu0629459743", "ie00bk5bqt80", "ie00bqqp9f84", "ie00bj0kdq92", "ie00b60sx394"):
    r = get(f"https://www.finanzfluss.de/informer/etf/{isin}/")
    if r is not None and r.status_code == 200:
        snip(r.text, 4)
        print("   ld+json", len(re.findall(r"application/ld\+json", r.text)), "next", "__NEXT_DATA__" in r.text,
              "nuxt", "__NUXT" in r.text)
# Vanguard: page by port id without slug
for u in ("https://www.vanguard.co.uk/professional/product/etf/equity/9505",
          "https://www.vanguard.co.uk/professional/product/etf/equity/9505/x",
          "https://www.vanguard.co.uk/professional/product/9505"):
    r = get(u)
    if r is not None and r.status_code == 200:
        m = re.search(r"OCF/TER\s*([\d.,]+)\s*%", r.text)
        print("   vanguard ter", m and m.group(1))
# Vanguard GraphQL guesses on Fund / profile
for q in ('{ funds(portIds: ["9505"]) { portId profile { fundFullName ocfDetails { ocf } } } }',
          '{ funds(portIds: ["9505"]) { portId profile { ongoingCharge } } }',
          '{ funds(portIds: ["9505"]) { portId profile { ocfAsOfDate managementFee } } }',
          '{ funds(portIds: ["9505"]) { portId profile { expenseRatio: ocf } } }',
          '{ funds(portIds: ["9505"]) { portId fundCharges { ocf } } }',
          '{ funds(portIds: ["9505"]) { portId costsAndCharges { ocf } } }',
          '{ funds(portIds: ["9505"]) { portId profile { costsAndCharges { ocf } } } }'):
    try:
        r = http.post(l.VANGUARD, json={"query": q}, headers=l.VANGUARD_HEADERS)
        print("   vg", re.sub(r"\s+", " ", r.text)[:300])
    except httpx.HTTPError as e:
        print("   vg", type(e).__name__)
# VanEck: does the holdings page (whose slug vaneck_ids sees) name the TER?
r = get(l.VANECK_LIST)
links = sorted(set(re.findall(r'/de/de/anlagen/[\w-]+/', r.text))) if r is not None else []
print("   vaneck links", len(links), links[:3])
for link in [x for x in links if "gold-miners" in x][:1] + links[:1]:
    for suffix in ("positionen/", ""):
        p = get(l.VANECK + link + suffix)
        if p is not None and p.status_code == 200:
            m = re.search(r"Gesamtkostenquote \(TER\)\s*(?:<[^>]+>\s*)*([\d.,]+)\s*%", p.text)
            print("   vaneck ter", suffix or "page", m and m.group(1))
            if not m:
                snip(p.text, 2)
