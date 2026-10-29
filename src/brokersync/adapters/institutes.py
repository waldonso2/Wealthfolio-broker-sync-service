"""FinTS address and name of a bank by its bank code, from the Deutsche Kreditwirtschaft's list (#136).

``fints_institutes.bin`` holds an extract of the list the service received as a registered FinTS
product - bank code, name and PIN/TAN address only. Like the product id it is kept out of plain sight:
zlib-compressed, then XORed with a SHA-256 stream, so no search of the repository finds a bank, a code
or an address in it. shortcut: obfuscation, not encryption (the decoding ships with it); the list is
renewed with ``scripts/fints_institutes.py`` when the Deutsche Kreditwirtschaft sends a new one.
"""

from __future__ import annotations

import hashlib
import zlib
from functools import cache
from pathlib import Path

FILE = Path(__file__).with_name("fints_institutes.bin")
_LABEL = b"wealthfolio-broker-sync/fints-institutes"  # a label, not the product name: never rename


def mask(data: bytes) -> bytes:
    """XOR with the SHA-256 stream of ``_LABEL``; the same call masks and unmasks."""
    stream = b"".join(hashlib.sha256(_LABEL + i.to_bytes(8, "big")).digest() for i in range(len(data) // 32 + 1))
    return bytes(a ^ b for a, b in zip(data, stream, strict=False))


@cache
def _table() -> dict[str, tuple[str, str]]:
    text = zlib.decompress(mask(FILE.read_bytes())).decode()
    rows = (line.split("\t") for line in text.splitlines() if line)
    return {blz: (name, url) for blz, name, url in rows}


def lookup(blz: str) -> tuple[str, str] | None:
    """(name, PIN/TAN address) of a bank code, None when the list doesn't know it."""
    return _table().get(blz.replace(" ", ""))
