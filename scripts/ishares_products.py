"""Writes ``src/brokersync/ishares_products.json``: ISIN -> iShares product number, from the country fund finders.

Usage: python3 scripts/ishares_products.py             # asks the fund finders (lookthrough.ISHARES_SCREENERS)
       python3 scripts/ishares_products.py FILE ...    # or reads answers saved before (curl -o FILE <finder>)

Every number already in the file stays (a product's number never changes; a closed fund only stops answering).
Fails when no answer named a product, so the weekly workflow says so instead of writing nothing.
"""

import json
import sys
from pathlib import Path

import httpx

from brokersync import lookthrough


def answers(paths: list[str]):
    if paths:
        for path in paths:
            yield path, Path(path).read_bytes()
        return
    with httpx.Client(timeout=300, follow_redirects=True, headers={"User-Agent": lookthrough.BROWSER}) as http:
        for url in lookthrough.ISHARES_SCREENERS:
            try:
                r = http.get(url)
            except httpx.HTTPError as e:
                print(f"{url}: {type(e).__name__}", file=sys.stderr)
                continue
            print(f"{url[:60]}…: HTTP {r.status_code}, {len(r.content)} bytes", file=sys.stderr)
            if r.status_code == 200:
                yield url, r.content


products = dict(lookthrough.KNOWN)
named = 0
for source, data in answers(sys.argv[1:]):
    try:
        rows = json.loads(data)
    except ValueError:
        print(f"{source}: no JSON", file=sys.stderr)
        continue
    for isin, (number, _), _ in lookthrough.finder_rows(rows):
        products.setdefault(isin, number)
        named += 1
if not named:
    sys.exit("No fund finder answer named a product - nothing written.")
lookthrough.PRODUCTS_FILE.write_text(json.dumps(dict(sorted(products.items())), indent=0) + "\n")
print(f"{len(products)} products ({len(products) - len(lookthrough.KNOWN)} new) in {lookthrough.PRODUCTS_FILE}")
