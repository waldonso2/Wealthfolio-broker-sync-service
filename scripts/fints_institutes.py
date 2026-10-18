"""Writes ``src/brokersync/adapters/fints_institutes.bin`` from the Deutsche Kreditwirtschaft's FinTS list.

Usage: python3 scripts/fints_institutes.py "fints_institute ... Master.csv"

Keeps only bank code, name and PIN/TAN address (the first row of a bank code with one) - no IP
addresses, data centres or other columns - and stores them masked (``brokersync.adapters.institutes``).
"""

import csv
import sys
import zlib

from brokersync.adapters import institutes

with open(sys.argv[1], encoding="latin-1", newline="") as f:
    rows = list(csv.reader(f, delimiter=";"))
head = rows[0]
blz, name, url = head.index("BLZ"), head.index("Institut"), head.index("PIN/TAN-Zugang URL")
table: dict[str, str] = {}
for row in rows[1:]:
    code, address = row[blz].strip(), row[url].strip()
    if len(code) == 8 and code.isdigit() and address.startswith("https://") and code not in table:
        table[code] = f"{code}\t{' '.join(row[name].split())}\t{address}"
institutes.FILE.write_bytes(institutes.mask(zlib.compress("\n".join(table.values()).encode(), 9)))
print(f"{len(table)} banks written to {institutes.FILE}")
