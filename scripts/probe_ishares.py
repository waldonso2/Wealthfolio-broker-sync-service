"""Temporary probe round 34: which country and sector values each issuer's list carries (counts only)."""
import collections
import sys

sys.path.insert(0, "src")
from brokersync import lookthrough as l  # noqa: E402

s = l.Sources()
LGIM = "https://fundcentres.lgim.com/srp/documents-id/9f3a4ebd-0038-4a92-8edd-5256eab14b58/Fundholdings.csv"
cases = [("VanEck Semi", "IE00BMC38736", "", "VanEck"), ("Xtrackers GSD", "LU0292096186", "", "Xtrackers"),
         ("SPDR World", "IE00BFY0GT14", "", "SPDR"), ("L&G Cyber", "IE00BYPLS672", LGIM, ""),
         ("iShares India", "IE00BZCQB185", "", "iShares"), ("Xtrackers World", "IE00BJ0KDQ92", "", "Xtrackers")]
for label, isin, page, by in cases:
    try:
        lines, source = l.fetch(s, isin, page, by=by)
    except Exception as e:
        print("##", label, type(e).__name__, str(e)[:200]); continue
    print("##", label, source, len(lines), "regions", s.regions[:5])
    for attr in ("country", "sector"):
        c = collections.Counter(getattr(x, attr) for x in lines)
        print("  ", attr, c.most_common(14))
