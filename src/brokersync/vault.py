"""Encrypted storage for everything secret: the Wealthfolio password, broker
credentials and sessions, the session key and the hash of the web UI password.

One JSON document, AES-256-GCM with the data-tier key (``brokersync.crypto``),
in ``<data>/secrets.enc``. Nothing from here is ever logged; every string in it
is registered with the log redaction (``brokersync.redact``). Writes are atomic
and serialised with a lock file, because the web UI and the timer run in
separate processes. Files of versions before 0.9.0 (Fernet with the key in
``data/``) are re-encrypted on the first read.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from . import crypto, redact

PURPOSE = "secrets"


class VaultError(Exception):
    pass


class Vault:
    def __init__(self, data_dir: Path):
        self.dir = Path(data_dir)
        self.file = self.dir / "secrets.enc"

    @contextmanager
    def _locked(self) -> Iterator[None]:
        with crypto.file_lock(self.dir, "secrets.lock"):
            yield

    def load(self) -> dict:
        if not self.file.exists():
            return {}
        raw = self.file.read_bytes()
        key = crypto.data_key(self.dir)
        if not crypto.is_sealed(raw):
            data = self._migrate(raw)
        else:
            try:
                data = json.loads(crypto.unseal(key, raw, PURPOSE))
            except crypto.DecryptError as e:
                raise VaultError(f"{self.file} can't be decrypted with the service's key") from e
        _register(data)
        return data

    def _migrate(self, raw: bytes) -> dict:
        """A Fernet file from before 0.9.0: decrypt with the old key, write it as AES-256-GCM."""
        try:
            data = json.loads(Fernet(crypto.legacy_fernet_key(self.dir)).decrypt(raw))
        except (InvalidToken, ValueError) as e:
            raise VaultError(f"{self.file} can't be decrypted with the service's key") from e
        # No lock here (update() may hold it): two processes migrating at once write the same content.
        self._save(data)
        return data

    def _save(self, data: dict, key: bytes | None = None) -> None:
        key = key or crypto.data_key(self.dir)
        crypto.write_atomic(self.file, crypto.seal(key, json.dumps(data).encode(), PURPOSE))

    def update(self, change: Callable[[dict], None]) -> dict:
        """Read, change and write back under the lock."""
        with self._locked():
            data = self.load()
            change(data)
            self._save(data)
            return data

    # ── convenience ─────────────────────────────────────────────────────────
    def broker(self, key: str) -> dict:
        return self.load().get("brokers", {}).get(key, {})

    def set_broker_credentials(self, key: str, credentials: dict[str, str]) -> None:
        def change(d: dict) -> None:
            b = d.setdefault("brokers", {}).setdefault(key, {})
            b["credentials"] = credentials
            b["session"] = {}
        self.update(change)

    def set_broker_session(self, key: str, session: dict) -> None:
        self.update(lambda d: d.setdefault("brokers", {}).setdefault(key, {}).__setitem__("session", session))


def _register(data: dict) -> None:
    """Credentials, passwords and keys are secrets for the log; of the sessions, the long strings (tokens,
    cookies, serialized state) - not short ones like a status, which would mask ordinary words."""
    for k, v in data.items():
        if k != "brokers":
            redact.register(v)
    for b in data.get("brokers", {}).values():
        redact.register(b.get("credentials", {}))
        redact.register_long(b.get("session", {}))


# ── web UI password ─────────────────────────────────────────────────────────
# argon2id, RFC 9106's second recommendation (64 MiB, t=3); tests lower it.
PASSWORD_HASH = {"time_cost": 3, "memory_cost": 64 * 1024, "parallelism": 4}


def _hasher():
    from argon2 import PasswordHasher

    return PasswordHasher(**PASSWORD_HASH)


def hash_password(password: str) -> str:
    return _hasher().hash(password)


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    if stored.startswith("$argon2"):
        from argon2.exceptions import VerificationError, VerifyMismatchError

        try:
            return _hasher().verify(stored, password)
        except (VerifyMismatchError, VerificationError, ValueError):
            return False
    try:  # scrypt, before 0.9.0
        _, salt, digest = stored.split("$")
    except ValueError:
        return False
    candidate = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1)
    return hmac.compare_digest(candidate.hex(), digest)


def needs_rehash(stored: str) -> bool:
    """True for hashes weaker than the current argon2id parameters (scrypt of older versions)."""
    return not stored.startswith("$argon2") or _hasher().check_needs_rehash(stored)
