"""Temporary probe round 36: does Finanzfluss' ETF page carry a country and sector breakdown? Snippets only."""
import re
import sys

sys.path.insert(0, "src")
import httpx  # noqa: E402

from brokersync import lookthrough as l  # noqa: E402

http = httpx.Client(timeout=60, follow_redirects=True, headers={"User-Agent": l.BROWSER, "Accept-Language": "de-DE"})
for isin in ("ie00byplS672".lower(), "lu0629459743", "ie00bmc38736"):
    t = http.get(l.FINANZFLUSS.format(isin=isin)).text
    print("##", isin, len(t))
    for w in ("Länder", "Branchen", "Sektoren", "Regionen", "countries", "sectors", "Vereinigte Staaten", "Technologie"):
        ms = list(re.finditer(w, t))
        print("  ", w, len(ms))
        for m in ms[:3]:
            print("     ..", re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", t[max(0, m.start() - 150):m.end() + 300]))[:320])
