"""Temporary probe round 37: Finanzfluss' country and sector blocks (raw HTML around them, and as text)."""
import re
import sys

sys.path.insert(0, "src")
import httpx  # noqa: E402

from brokersync import lookthrough as l  # noqa: E402

http = httpx.Client(timeout=60, follow_redirects=True, headers={"User-Agent": l.BROWSER, "Accept-Language": "de-DE"})
for isin in ("lu0629459743", "ie00bmc38736"):
    t = http.get(l.FINANZFLUSS.format(isin=isin)).text
    print("##", isin)
    for w in (">\\s*Länder\\s*<", ">\\s*Branchen\\s*<", "US Vereinigte Staaten"):
        m = re.search(w, t)
        if not m:
            print("  no", w); continue
        raw = t[m.start():m.start() + 2500]
        print("  RAW", w, raw[:1800].replace("\n", " "))
        print("  TXT", re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " | ", t[m.start():m.start() + 9000]))[:1800])
    print("  sonstige", [t[m.start() - 80:m.end() + 80] for m in re.finditer("Sonstige", t)][:3])
