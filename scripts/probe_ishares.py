"""Temporary probe round 35: iShares screener's country/region fields per fund."""
import sys

sys.path.insert(0, "src")
import httpx  # noqa: E402

from brokersync import lookthrough as l  # noqa: E402

rows = httpx.get(l.ISHARES_SCREENERS[0], headers={"User-Agent": l.BROWSER}, timeout=90).json()
want = {"IE00BZCQB185", "IE00B4L5Y983", "IE00B3WJKG14", "IE00B53SZB19", "IE00B4ND3602", "DE0005933931"}
for code, product, row in l.finder_rows(rows):
    if code in want:
        print(code, {k: row.get(k) for k in ("fundName", "aladdinCountry", "aladdinCountryCode", "aladdinRegion",
                                              "aladdinRegionCode", "aladdinMarketType")})
