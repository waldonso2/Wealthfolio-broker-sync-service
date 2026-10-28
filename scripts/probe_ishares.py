"""Temporary probe round 8: VanEck ISIN -> page, L&G fund page data."""
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
        print(name, url, type(e).__name__); return None
    print(name, r.status_code, r.headers.get("content-type", ""), len(r.content), str(r.url)[:200])
    if name: (OUT / name).write_bytes(r.content)
    return r

r = save("vaneck-factsheets.html", "https://www.vaneck.com/de/de/factsheets/etfs/")
t = html.unescape(r.text)
print("isins", len(set(re.findall(r"\b(?:IE|NL|LU)[A-Z0-9]{9}\d\b", t))))
for m in re.finditer(r"<ve-[a-z-]+[^>]*data-blockid[^>]*>", t):
    print("block", m.group(0)[:300])
    b = re.search(r'data-blockid="(\d+)"', m.group(0)); p = re.search(r'data-pageid="(\d+)"', m.group(0))
    tag = re.match(r"<ve-([a-z-]+?)(?:block)?\b", m.group(0)).group(1)
    if b and p:
        for name in ("FactSheetsBlock", "DocumentsBlock", "FundListingBlock", "FundsListBlock"):
            save(f"vaneck-{name}.json", f"https://www.vaneck.com/Main/{name}/GetContent/?blockid={b.group(1)}&pageid={p.group(1)}")
save("vaneck-overview.html", "https://www.vaneck.com/de/de/anlagen/semiconductor-etf/ueberblick/")
lg = save("lgim-page.html", "https://fundcentres.landg.com/en/uk/institutional/fund-centre/ETF/Cyber-Security/")
lt = html.unescape(lg.text)
for m in re.finditer(r'data-(?:fund|share|isin|part_id|part_type|placement)[a-z_-]*="[^"]+"', lt):
    pass
print(sorted(set(re.findall(r'data-part_type="([^"]+)"', lt))))
for m in list(re.finditer(r"IE00BYPLS672|ISPY|fundId|fund_id|shareClassId", lt))[:8]:
    print("LG..", lt[max(0, m.start() - 200):m.end() + 200].replace("\n", " "))
