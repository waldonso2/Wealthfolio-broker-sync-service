"""Temporary probe round 32: which columns iShares' holdings workbook has (header and two rows only)."""
import sys

sys.path.insert(0, "src")
import httpx  # noqa: E402

from brokersync import lookthrough as l  # noqa: E402

http = httpx.Client(timeout=90, follow_redirects=True, headers={"User-Agent": l.BROWSER})
for pid in ("280510", "251882"):
    r = http.get(l.ISHARES_DOC.format(id=pid))
    print("##", pid, r.status_code, r.headers.get("content-type"), len(r.content))
    try:
        rows = l.workbook_rows(r.content, "Holdings")
    except Exception as e:
        print("  ", type(e).__name__, e); continue
    head = next((i for i, row in enumerate(rows) if any("ewicht" in c or "eight" in c for c in row)), 0)
    for row in rows[head:head + 3]:
        print("  ", row[:14])
    sheets = sorted(set(__import__("re").findall(r'ss:Name="([^"]+)"', r.text)))
    print("   sheets", sheets[:10])
    lines = l.parse(l._rows_to_csv(rows))
    print("   parsed", len(lines), "with country", sum(1 for c in lines if c.country), "sector", sum(1 for c in lines if c.sector))
