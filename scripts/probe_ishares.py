"""Temporary probe round 9: VanEck map, L&G ETF fund centre."""
import html
import json
import re
from pathlib import Path
import httpx

OUT = Path("probe-files"); OUT.mkdir(exist_ok=True)
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
http = httpx.Client(timeout=90, follow_redirects=True, headers={"User-Agent": UA, "Accept-Language": "de-DE,de;q=0.9,en;q=0.8"})

def get(url, **kw):
    try:
        return http.request(kw.pop("method", "GET"), url, **kw)
    except httpx.HTTPError as e:
        print(url, type(e).__name__)

nav = html.unescape(get("https://www.vaneck.com/de/de/factsheets/etfs/").text)
slugs = sorted(set(re.findall(r'href="/de/de/anlagen/([a-z0-9-]+)/"', nav)))
print("vaneck slugs", len(slugs))
found = {}
for slug in slugs:
    r = get(f"https://www.vaneck.com/de/de/anlagen/{slug}/positionen/")
    if r is None or r.status_code != 200:
        print(slug, r.status_code if r is not None else "-"); continue
    t = r.text
    isins = sorted(set(re.findall(r"\b(?:IE|NL|LU)[A-Z0-9]{9}\d\b", t)))
    b = re.search(r'<ve-holdingsblock[^>]*data-blockid="(\d+)"[^>]*data-pageid="(\d+)"', t)
    tick = re.search(r"<ve-fundticker[^>]*>([^<]+)<", t)
    found[slug] = {"isins": isins, "block": b.groups() if b else None, "ticker": tick.group(1) if tick else None}
print(json.dumps(dict(list(found.items())[:6]), indent=0)[:1500])
(OUT / "vaneck-map.json").write_text(json.dumps(found))
print("with holdings block", sum(1 for v in found.values() if v["block"]), "with isin", sum(1 for v in found.values() if v["isins"]))
# L&G: the ETF fund centre's config, then its list API
lg = get("https://fundcentres.landg.com/en/uk/institutional/fund-centre/etf/")
(OUT / "lgim-etf.html").write_bytes(lg.content)
cfg = re.search(r"data-config='([^']+)'", lg.text)
if cfg:
    c = json.loads(html.unescape(cfg.group(1)))
    print("lg config keys", sorted(c)[:60])
    print({k: c[k] for k in c if not isinstance(c[k], (list, dict))})
