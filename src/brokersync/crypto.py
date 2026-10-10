"""Keys and encryption for everything the service stores.

Two key tiers, both derived (HKDF-SHA256) from the **host key** - 32 random
bytes the service never keeps in ``data/``:

- **host tier** - host key only: ``notify.enc`` (ntfy settings), so that even a
  locked service can say it is locked.
- **data tier** - host key, plus the master passphrase when one is set (argon2id):
  ``secrets.enc`` (credentials, sessions, tokens), ``config.enc``, ``state.db``
  (SQLCipher). Without the passphrase, a copy of ``data/`` and of the host key
  together is still useless.

Where the host key comes from, in this order:

1. ``$CREDENTIALS_DIRECTORY/brokersync-key`` - systemd hands it to the service
   (``LoadCredentialEncrypted=`` from ``/etc/wealthfolio-broker-sync/key.cred``,
   encrypted with ``systemd-creds``; or ``LoadCredential=`` from a root-only file
   where systemd-creds can't encrypt). The service user can't read the source.
2. ``$BROKERSYNC_KEY_FILE``.
3. ``data/secret.key`` - only for development and tests, and for an
   installation not migrated yet (``deploy/setup.sh`` moves it out).

Files are AES-256-GCM: ``MAGIC | nonce (12) | ciphertext+tag``; the file's
purpose is authenticated data, so a file can't be swapped for another.

The passphrase never touches the disk. Unlocking keeps its argon2id result in
``$BROKERSYNC_RUNTIME/unlock.key`` (``/run`` - RAM, gone after a restart, not in
backups), shared by the web UI and the timer.
"""

from __future__ import annotations

import base64
import fcntl
import json
import os
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

MAGIC = b"BSE1"
KEY_NAME = "brokersync-key"
DEFAULT_RUNTIME = Path("/run/wealthfolio-broker-sync")
# argon2id for the master passphrase (RFC 9106's second recommendation: 64 MiB, t=3).
ARGON2 = {"time_cost": 3, "memory_cost": 64 * 1024, "parallelism": 4}
MIN_PASSPHRASE = 12

# The unlocked passphrase key per data dir, when there is no runtime dir (development).
_memory: dict[str, bytes] = {}


class KeyMissing(Exception):
    """The host key is missing or doesn't fit the data."""


class Locked(Exception):
    """A master passphrase is set and the service hasn't been unlocked since it started."""


class DecryptError(Exception):
    pass


# ── host key ────────────────────────────────────────────────────────────────
def host_key_source(data_dir: Path) -> str:
    """Where the host key comes from: systemd-creds, systemd, file, data (development/not migrated)."""
    cred = os.environ.get("CREDENTIALS_DIRECTORY")
    if cred and (Path(cred) / KEY_NAME).is_file():
        return os.environ.get("BROKERSYNC_KEY_SOURCE", "systemd")
    if os.environ.get("BROKERSYNC_KEY_FILE"):
        return "file"
    return "data"


def _host_material(data_dir: Path) -> bytes:
    cred = os.environ.get("CREDENTIALS_DIRECTORY")
    if cred and (Path(cred) / KEY_NAME).is_file():
        return (Path(cred) / KEY_NAME).read_bytes().strip()
    if os.environ.get("BROKERSYNC_KEY_FILE"):
        return Path(os.environ["BROKERSYNC_KEY_FILE"]).read_bytes().strip()
    local = Path(data_dir) / "secret.key"
    if local.exists():
        return local.read_bytes().strip()
    if any((Path(data_dir) / f).exists() for f in ("secrets.enc", "config.enc", "keyring.json")):
        raise KeyMissing(
            "Der Schlüssel des Dienstes fehlt. Den Dienst über systemd starten (er bekommt den Schlüssel als "
            "Credential) bzw. Befehle mit brokersync-cli ausführen."
        )
    # A new development or test setup: a key of its own.
    Path(data_dir).mkdir(mode=0o700, parents=True, exist_ok=True)
    material = base64.urlsafe_b64encode(secrets.token_bytes(32))
    fd = os.open(local, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(material)
    return material


def legacy_fernet_key(data_dir: Path) -> bytes:
    """The host key as Fernet key: versions before 0.9.0 encrypted ``secrets.enc`` with it directly."""
    return _host_material(data_dir)


def _hkdf(material: bytes, info: str) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info.encode()).derive(material)


def host_key(data_dir: Path) -> bytes:
    return _hkdf(_host_material(data_dir), "brokersync host v1")


# ── keyring and passphrase ──────────────────────────────────────────────────
def _keyring_file(data_dir: Path) -> Path:
    return Path(data_dir) / "keyring.json"


def keyring(data_dir: Path) -> dict:
    f = _keyring_file(data_dir)
    return json.loads(f.read_text()) if f.exists() else {"version": 1, "passphrase": None}


def passphrase_enabled(data_dir: Path) -> bool:
    return bool(keyring(data_dir).get("passphrase"))


def _runtime() -> Path | None:
    d = Path(os.environ.get("BROKERSYNC_RUNTIME", DEFAULT_RUNTIME))
    return d if d.is_dir() and os.access(d, os.W_OK) else None


def _cache_id(data_dir: Path) -> str:
    return str(Path(data_dir).resolve())


def _cached_pkey(data_dir: Path) -> bytes | None:
    rt = _runtime()
    if rt:
        f = rt / "unlock.key"
        return bytes.fromhex(f.read_text().strip()) if f.exists() else None
    return _memory.get(_cache_id(data_dir))


def _cache_pkey(data_dir: Path, pkey: bytes | None) -> None:
    rt = _runtime()
    if rt:
        f = rt / "unlock.key"
        if pkey is None:
            f.unlink(missing_ok=True)
        else:
            tmp = rt / "unlock.key.tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(pkey.hex())
            os.replace(tmp, f)
    else:
        if pkey is None:
            _memory.pop(_cache_id(data_dir), None)
        else:
            _memory[_cache_id(data_dir)] = pkey


def _argon2(passphrase: str, salt: bytes, params: dict) -> bytes:
    from argon2.low_level import Type, hash_secret_raw

    return hash_secret_raw(passphrase.encode(), salt, time_cost=params["time_cost"],
                           memory_cost=params["memory_cost"], parallelism=params["parallelism"], hash_len=32,
                           type=Type.ID)


def _data_key(data_dir: Path, pkey: bytes | None) -> bytes:
    material = _host_material(data_dir) + (pkey or b"")
    return _hkdf(material, "brokersync data v1")


def _check_blob(key: bytes) -> str:
    return base64.b64encode(seal(key, b"brokersync-unlock", "check")).decode()


def _check_ok(key: bytes, blob: str) -> bool:
    try:
        return unseal(key, base64.b64decode(blob), "check") == b"brokersync-unlock"
    except DecryptError:
        return False


def is_locked(data_dir: Path) -> bool:
    return passphrase_enabled(data_dir) and _cached_pkey(data_dir) is None


def data_key(data_dir: Path) -> bytes:
    """The data-tier key; raises ``Locked`` while a passphrase is set and not entered."""
    ring = keyring(data_dir)
    if not ring.get("passphrase"):
        return _data_key(data_dir, None)
    pkey = _cached_pkey(data_dir)
    if pkey is None:
        raise Locked("Der Dienst ist gesperrt: Bitte in der Weboberfläche die Master-Passphrase eingeben.")
    return _data_key(data_dir, pkey)


def unlock(data_dir: Path, passphrase: str) -> bool:
    ring = keyring(data_dir)
    p = ring.get("passphrase")
    if not p:
        return True
    pkey = _argon2(passphrase, bytes.fromhex(p["salt"]), p)
    if not _check_ok(_data_key(data_dir, pkey), p["check"]):
        return False
    _cache_pkey(data_dir, pkey)
    return True


def lock(data_dir: Path) -> None:
    _cache_pkey(data_dir, None)


def new_passphrase_key(data_dir: Path, passphrase: str | None) -> tuple[bytes, dict]:
    """The data key for a new passphrase (None: without one) and the keyring that goes with it."""
    if not passphrase:
        return _data_key(data_dir, None), {"version": 1, "passphrase": None}
    salt = secrets.token_bytes(16)
    pkey = _argon2(passphrase, salt, ARGON2)
    key = _data_key(data_dir, pkey)
    return key, {"version": 1, "passphrase": {"salt": salt.hex(), **ARGON2, "check": _check_blob(key)},
                 "_pkey": pkey.hex()}


def write_keyring(data_dir: Path, ring: dict) -> None:
    pkey = ring.pop("_pkey", None)
    write_atomic(_keyring_file(data_dir), json.dumps(ring, indent=2).encode())
    _cache_pkey(data_dir, bytes.fromhex(pkey) if pkey else None)


# ── encryption ──────────────────────────────────────────────────────────────
def subkey(key: bytes, purpose: str) -> bytes:
    return _hkdf(key, f"brokersync {purpose}")


def seal(key: bytes, plaintext: bytes, purpose: str) -> bytes:
    nonce = secrets.token_bytes(12)
    return MAGIC + nonce + AESGCM(subkey(key, purpose)).encrypt(nonce, plaintext, MAGIC + purpose.encode())


def unseal(key: bytes, blob: bytes, purpose: str) -> bytes:
    if not blob.startswith(MAGIC):
        raise DecryptError("unknown format")
    try:
        return AESGCM(subkey(key, purpose)).decrypt(blob[4:16], blob[16:], MAGIC + purpose.encode())
    except Exception as e:
        raise DecryptError(f"{purpose} can't be decrypted with this key") from e


def is_sealed(blob: bytes) -> bool:
    return blob.startswith(MAGIC)


def write_atomic(path: Path, content: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


@contextmanager
def file_lock(data_dir: Path, name: str) -> Iterator[None]:
    with open_lock(Path(data_dir) / name) as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def open_lock(path: Path):
    """A lock file, readable by the service user only."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    return os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "a")
