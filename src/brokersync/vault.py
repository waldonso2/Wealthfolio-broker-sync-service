"""Encrypted storage for everything secret: the Wealthfolio password, broker
credentials and sessions, the ntfy token and the hash of the web UI password.

The data is one JSON document encrypted with Fernet (AES-128-CBC + HMAC) in
``<data>/secrets.enc``; the key lives in ``<data>/secret.key`` (mode 0600, only
readable by the service user). Neither file is in the repository, and nothing
from here is ever logged. Writes are atomic and serialised with a lock file,
because the web UI and the timer run in separate processes.
"""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import secrets
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


class VaultError(Exception):
    pass


class Vault:
    def __init__(self, data_dir: Path):
        self.dir = Path(data_dir)
        self.key_file = self.dir / "secret.key"
        self.file = self.dir / "secrets.enc"
        self.lock_file = self.dir / "secrets.lock"

    def _fernet(self) -> Fernet:
        self.dir.mkdir(parents=True, exist_ok=True)
        if not self.key_file.exists():
            fd = os.open(self.key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(Fernet.generate_key())
        return Fernet(self.key_file.read_bytes().strip())

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.dir.mkdir(parents=True, exist_ok=True)
        with open(self.lock_file, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def load(self) -> dict:
        if not self.file.exists():
            return {}
        try:
            return json.loads(self._fernet().decrypt(self.file.read_bytes()))
        except InvalidToken as e:
            raise VaultError(f"{self.file} can't be decrypted with {self.key_file}") from e

    def _save(self, data: dict) -> None:
        token = self._fernet().encrypt(json.dumps(data).encode())
        tmp = self.file.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(token)
        os.replace(tmp, self.file)

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


# ── web UI password ─────────────────────────────────────────────────────────
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        _, salt, digest = stored.split("$")
    except ValueError:
        return False
    candidate = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1)
    return hmac.compare_digest(candidate.hex(), digest)
