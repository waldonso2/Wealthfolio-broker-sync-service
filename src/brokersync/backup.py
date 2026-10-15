"""Backup and restore of ``data/`` (#109): ``brokersync-cli backup > file``, ``brokersync-cli restore < file``.

An archive holds ``data/<file>`` - the same layout as the backups ``update`` makes - and everything in it stays
encrypted: it opens only with this container's key (``/etc/wealthfolio-broker-sync``) and, if one is set, the
master passphrase. ``restore`` reads the archive with the current key **before** it touches ``data/``; one
that doesn't open is refused. The data it replaces is kept as ``data/before-restore-<time>.tar.gz``. The web
UI must be stopped (it holds ``ui.lock`` while it runs); a sync can't run meanwhile (sync lock).
"""

from __future__ import annotations

import fcntl
import io
import os
import re
import shutil
import tarfile
import tempfile
from datetime import datetime
from pathlib import Path
from typing import BinaryIO

from . import crypto
from . import state as state_mod
from .sync import run_lock

UI_LOCK = "ui.lock"
MAX_FILE = 512 * 1024 * 1024
# Lock files, leftovers of an interrupted write, earlier restores: not part of a backup. Never the key either
# (``secret.key`` of a development setup or an installation before 0.9.0).
SKIP = re.compile(r"(\.lock|\.tmp|\.rekey|-journal|-wal|-shm)$|^before-restore-|^\.|^secret\.key$")
MEMBER = re.compile(r"^(?:\./)?data/([^/]+)$")


class RestoreError(Exception):
    pass


def hold_ui_lock(data_dir: Path):
    """Taken by ``brokersync serve`` for its lifetime: a restore refuses to run under a running UI."""
    f = crypto.open_lock(Path(data_dir) / UI_LOCK)
    fcntl.flock(f, fcntl.LOCK_SH)
    return f


def write(data_dir: Path, out: BinaryIO) -> list[str]:
    """``data/`` as a tar.gz to ``out``; ``state.db`` as a consistent copy. Returns the files' names."""
    d = Path(data_dir)
    with run_lock(d, wait=60), crypto.file_lock(d, "secrets.lock"):
        return _write(d, out)


def _write(d: Path, out: BinaryIO) -> list[str]:
    with tempfile.TemporaryDirectory(dir=d, prefix=".backup-") as tmp:
        names = sorted(p.name for p in d.iterdir() if p.is_file() and not SKIP.search(p.name))
        with tarfile.open(fileobj=out, mode="w:gz") as tar:
            for name in names:
                path = d / name
                if name == "state.db":
                    path = Path(tmp) / name
                    _copy_db(d, path)
                info = tar.gettarinfo(str(path), arcname=f"data/{name}")
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                info.mode = 0o600
                with open(path, "rb") as f:
                    tar.addfile(info, f)
    return names


def _copy_db(d: Path, target: Path) -> None:
    """SQLite's backup: consistent even while the UI writes."""
    key = state_mod.db_key(d) if state_mod.ENCRYPTED else b""
    src = state_mod._connect(d / "state.db", key)
    dst = state_mod._connect(target, key)
    try:
        src.backup(dst)
    finally:
        src.close()
        dst.close()


def restore(data_dir: Path, archive: BinaryIO) -> str:
    d = Path(data_dir)
    with crypto.open_lock(d / UI_LOCK) as ui:
        try:
            fcntl.flock(ui, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            raise RestoreError("The web UI is running: systemctl stop wealthfolio-broker-sync first.") from e
        with run_lock(d, wait=60):
            tmp = Path(tempfile.mkdtemp(dir=d, prefix=".restore-"))
            try:
                names = _extract(archive, tmp)
                _check_readable(d, tmp)
                kept = d / f"before-restore-{datetime.now():%Y%m%d-%H%M%S-%f}.tar.gz"
                with crypto.file_lock(d, "secrets.lock"):
                    fd = os.open(kept, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, "wb") as f:
                        _write(d, f)
                    for old in d.iterdir():
                        if old.is_file() and not SKIP.search(old.name) and old.name not in names:
                            old.unlink()
                    for name in names:
                        os.replace(tmp / name, d / name)
                    for suffix in ("-journal", "-wal", "-shm"):
                        (d / f"state.db{suffix}").unlink(missing_ok=True)
                    crypto.fsync_dir(d)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
    return f"Restored {len(names)} files; the data before is kept in {kept.name}."


def _extract(archive: BinaryIO, target: Path) -> list[str]:
    """Only regular files ``data/<name>``, nothing else - no links, no paths outside, no huge files."""
    names: list[str] = []
    try:
        with tarfile.open(fileobj=io.BytesIO(archive.read()), mode="r:*") as tar:
            for member in tar:
                m = MEMBER.match(member.name)
                if member.isdir() and member.name.strip("./") == "data":
                    continue
                if not m or not member.isfile() or SKIP.search(m.group(1)) or member.size > MAX_FILE:
                    raise RestoreError(f"Unexpected entry in the archive: {member.name}")
                source = tar.extractfile(member)
                if source is None:
                    raise RestoreError(f"Unreadable entry in the archive: {member.name}")
                fd = os.open(target / m.group(1), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as f:
                    shutil.copyfileobj(source, f)
                names.append(m.group(1))
    except (tarfile.TarError, EOFError) as e:
        raise RestoreError(f"Not a backup archive: {e}") from e
    if "secrets.enc" not in names and "config.enc" not in names:
        raise RestoreError("Not a backup of this service: neither secrets.enc nor config.enc in it.")
    return names


def _check_readable(d: Path, tmp: Path) -> None:
    """Everything in the archive opens with this container's key (and unlocked passphrase) - else nothing is
    replaced."""
    try:
        key = crypto.data_key_for(d, crypto.keyring(tmp))
        for name, purpose in (("secrets.enc", "secrets"), ("config.enc", "config")):
            if (tmp / name).exists():
                crypto.unseal(key, (tmp / name).read_bytes(), purpose)
        if (tmp / "notify.enc").exists():
            crypto.unseal(crypto.host_key(d), (tmp / "notify.enc").read_bytes(), "notify")
        if (tmp / "state.db").exists() and state_mod.ENCRYPTED and not state_mod.is_plain(tmp / "state.db"):
            db = state_mod._connect(tmp / "state.db", state_mod.db_key(d, key))
            try:
                db.execute("SELECT count(*) FROM sqlite_master").fetchone()
            finally:
                db.close()
    except Exception as e:  # sqlcipher's own DatabaseError too
        raise RestoreError(f"The archive doesn't open with this container's key (or passphrase): {e}") from e
