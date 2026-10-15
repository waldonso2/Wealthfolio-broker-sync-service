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

import copy
import functools
import hashlib
import hmac
import json
import os
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
        # The decrypted file, by its identity on disk: decrypted again only when it changed (#104).
        self._cache: tuple[tuple, dict] | None = None

    @contextmanager
    def _locked(self) -> Iterator[None]:
        with crypto.file_lock(self.dir, "secrets.lock"):
            yield

    def load(self) -> dict:
        if not self.file.exists():
            return {}
        with open(self.file, "rb") as f:
            st = os.fstat(f.fileno())
            stamp = (st.st_ino, st.st_mtime_ns, st.st_size)
            if self._cache and self._cache[0] == stamp:
                return copy.deepcopy(self._cache[1])
            raw = f.read()
        key = crypto.data_key(self.dir)
        if not crypto.is_sealed(raw):
            return self._migrate(raw)
        try:
            data = json.loads(crypto.unseal(key, raw, PURPOSE))
        except crypto.DecryptError as e:
            raise VaultError(f"{self.file} can't be decrypted with the service's key") from e
        _register(data)
        self._cache = (stamp, copy.deepcopy(data))
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

    def _save(self, data: dict) -> None:
        crypto.write_atomic(self.file, crypto.seal(crypto.data_key(self.dir), json.dumps(data).encode(), PURPOSE))

    def update(self, change: Callable[[dict], None]) -> dict:
        """Read, change and write back under the lock."""
        with self._locked():
            data = self.load()
            change(data)
            self._save(data)
            _register(data)  # a secret just stored is masked from now on, not only after the next read
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

    def store_session(self, key: str, adapter) -> None:
        """Write the adapter's session back. If someone else wrote one since the adapter read it (the web
        login and the timer's run at the same time), the higher lock-out counters win - a bank must never
        see more TAN requests than it allows. Credentials saved anew meanwhile (session emptied): theirs
        wins, the old login's session is dropped."""
        session = adapter.session_state()

        def change(d: dict) -> None:
            b = d.setdefault("brokers", {}).setdefault(key, {})
            stored = b.get("session") or {}
            new = dict(session)
            if stored != adapter.stored_session:
                if not stored:
                    return
                for k in ("open_challenges", "wrong_tans"):
                    if k in stored or k in new:
                        new[k] = max(int(stored.get(k) or 0), int(new.get(k) or 0))
                if stored.get("pin_rejected"):
                    new["pin_rejected"] = True
            b["session"] = new
            adapter.stored_session = dict(new)

        self.update(change)


def _register(data: dict) -> None:
    """Credentials, passwords and keys are secrets for the log; of the sessions, the long strings (tokens,
    cookies, serialized state) - not short ones like a status, which would mask ordinary words."""
    for k, v in data.items():
        if k != "brokers":
            redact.register(v)
    for key, b in data.get("brokers", {}).items():
        redact.register(b.get("credentials", {}))
        redact.register_long(b.get("session", {}), scope=f"session:{key}")


# ── web UI password ─────────────────────────────────────────────────────────
# argon2id, RFC 9106's second recommendation (64 MiB, t=3); tests lower it.
PASSWORD_HASH = {"time_cost": 3, "memory_cost": 64 * 1024, "parallelism": 4}


def _hasher():
    from argon2 import PasswordHasher

    return PasswordHasher(**PASSWORD_HASH)


def hash_password(password: str) -> str:
    return _hasher().hash(password)


def session_tag(data: dict) -> str:
    """Goes into the session at login: a session of an earlier password (before a reset or a change) or of an
    earlier session version (logout, "Alle Sitzungen beenden", #96) is no longer valid."""
    stored = f"{data.get('ui_password_hash') or ''}:{data.get('ui_session_gen', 0)}"
    return hashlib.sha256(stored.encode()).hexdigest()[:16]


def end_ui_sessions(d: dict) -> None:
    """For ``Vault.update``: every session signed in so far signs in nobody from now on."""
    d["ui_session_gen"] = int(d.get("ui_session_gen", 0)) + 1


def weak_password(password: str) -> bool:
    """One of the most common passwords, also with digits or signs around it ("passwort1234", #96)."""
    core = password.lower().strip("0123456789!?.,;:-_+*#@$%&/()=<> ")
    return len(set(password)) < 5 or not core or core in _common() or password.lower() in _common()


@functools.cache
def _common() -> frozenset[str]:
    text = (Path(__file__).parent / "common-passwords.txt").read_text(encoding="utf-8")
    return frozenset(line.strip().lower() for line in text.splitlines() if line.strip() and not line.startswith("#"))


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


# ── one-time setup code for the first password (#89) ────────────────────────
# Without it anyone in the LAN - or any web page open in a browser there - could set the UI password
# before its owner. The code is shown on the container's console only (install, ``brokersync-cli
# setup-code``, ``brokersync-reset-password``), never logged; the vault keeps only its hash.
SETUP_CODE_HOURS = 24


def new_setup_code(vault: Vault) -> str:
    import secrets
    from datetime import UTC, datetime, timedelta

    code = secrets.token_urlsafe(16)
    expires = (datetime.now(UTC) + timedelta(hours=SETUP_CODE_HOURS)).isoformat()
    vault.update(lambda d: d.__setitem__("setup_code", {"sha256": _code_hash(code), "expires": expires}))
    return code


def check_setup_code(vault: Vault, code: str) -> bool:
    import hmac
    from datetime import UTC, datetime

    entry = vault.load().get("setup_code") or {}
    try:
        expires = datetime.fromisoformat(entry["expires"])
        expected = str(entry["sha256"])
    except (KeyError, TypeError, ValueError):  # missing or damaged: like an expired code, never an error page
        return False
    if expires < datetime.now(UTC):
        return False
    return hmac.compare_digest(_code_hash(code.strip()), expected)


def _code_hash(code: str) -> str:
    import hashlib

    return hashlib.sha256(code.encode()).hexdigest()
