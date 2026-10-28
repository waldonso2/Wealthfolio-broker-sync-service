"""Temporary probe round 33: iShares' sheet "Exposure Breakdowns" (rows only)."""
import sys

sys.path.insert(0, "src")
import httpx  # noqa: E402

from brokersync import lookthrough as l  # noqa: E402

http = httpx.Client(timeout=90, follow_redirects=True, headers={"User-Agent": l.BROWSER})
for pid in ("280510", "251882"):
    r = http.get(l.ISHARES_DOC.format(id=pid))
    rows = l.workbook_rows(r.content, "Exposure Breakdowns")
    print("##", pid, len(rows))
    for row in rows[:70]:
        print("  ", row[:8])
