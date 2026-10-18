"""The value for ``adapters/fints._REGISTERED`` from a FinTS product id, asked for without echo.

Usage: python3 scripts/obfuscate_product_id.py   (paste the id; only the obfuscated hex is printed)
"""

import getpass
import hashlib

value = getpass.getpass("FinTS-Produkt-ID: ").strip().encode()
pad = hashlib.sha256(b"wealthfolio-broker-sync/fints").digest()
if not 0 < len(value) <= len(pad):
    raise SystemExit("The id is empty or longer than the pad.")
print(bytes(a ^ b for a, b in zip(value, pad, strict=False)).hex())
