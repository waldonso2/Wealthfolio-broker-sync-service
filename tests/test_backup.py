"""companion-cli backup / restore (#109)."""

import fcntl
import io
import tarfile

import pytest

from brokersync import backup, security
from brokersync import config as config_mod
from brokersync.state import State
from brokersync.vault import Vault

from .fakes import FakeWealthfolio
from .test_sync import setup


def _archive(data) -> bytes:
    out = io.BytesIO()
    backup.write(data, out)
    return out.getvalue()


def test_a_backup_comes_back_and_everything_opens(tmp_path):
    syncer, _ = setup(tmp_path, FakeWealthfolio())
    syncer.run()
    syncer.state.close()
    archive = _archive(tmp_path)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        names = {m.name for m in tar}
        assert "data/state.db" in names and "data/secrets.enc" in names
        assert all(m.mode == 0o600 for m in tar) and not [n for n in names if n.endswith(".lock")]
        assert "data/secret.key" not in names  # never the key
    runs = len(State(tmp_path).runs())
    # Changed after the backup: the restore brings back the state of then.
    Vault(tmp_path).update(lambda d: d.__setitem__("wealthfolio_password", "changed"))
    config_mod.update(tmp_path, lambda c: setattr(c, "ntfy_topic", "changed"))
    syncer.run()
    message = backup.restore(tmp_path, io.BytesIO(archive))
    assert "Restored" in message
    assert Vault(tmp_path).load()["wealthfolio_password"] != "changed"
    assert config_mod.load(tmp_path).ntfy_topic != "changed" and len(State(tmp_path).runs()) == runs
    kept = list(tmp_path.glob("before-restore-*.tar.gz"))
    assert len(kept) == 1 and (kept[0].stat().st_mode & 0o077) == 0
    # A backup of a container with a passphrase opens once it is unlocked.
    security.change_passphrase(tmp_path, "eine sehr lange passphrase")
    archive = _archive(tmp_path)
    assert "Restored" in backup.restore(tmp_path, io.BytesIO(archive))


def test_an_archive_of_another_container_is_refused_and_nothing_changes(tmp_path, monkeypatch):
    other = tmp_path / "other"
    syncer, _ = setup(other, FakeWealthfolio())
    syncer.run()
    archive = _archive(other)
    mine = tmp_path / "mine"
    syncer, _ = setup(mine, FakeWealthfolio())
    syncer.run()
    before = {p.name: p.read_bytes() for p in mine.iterdir() if p.is_file() and not p.name.endswith(".lock")}
    with pytest.raises(backup.RestoreError, match="key"):
        backup.restore(mine, io.BytesIO(archive))
    assert {p.name: p.read_bytes() for p in mine.iterdir() if p.is_file() and not p.name.endswith(".lock")} == before
    assert not list(mine.glob(".restore-*"))


def test_restore_refuses_odd_archives_and_a_running_ui(tmp_path):
    syncer, _ = setup(tmp_path, FakeWealthfolio())
    syncer.run()
    for name in ("../etc/passwd", "data/../../x", "data/sub/file"):
        out = io.BytesIO()
        with tarfile.open(fileobj=out, mode="w:gz") as tar:
            info = tarfile.TarInfo(name)
            info.size = 1
            tar.addfile(info, io.BytesIO(b"x"))
        with pytest.raises(backup.RestoreError, match="Unexpected"):
            backup.restore(tmp_path, io.BytesIO(out.getvalue()))
    with pytest.raises(backup.RestoreError, match="Not a backup"):
        backup.restore(tmp_path, io.BytesIO(b"garbage"))
    lock = backup.hold_ui_lock(tmp_path)
    try:
        with pytest.raises(backup.RestoreError, match="web UI is running"):
            backup.restore(tmp_path, io.BytesIO(_archive(tmp_path)))
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def test_the_commands(tmp_path, monkeypatch, capsysbinary):
    from brokersync import cli

    syncer, _ = setup(tmp_path, FakeWealthfolio())
    syncer.run()
    monkeypatch.setenv("COMPANION_DATA", str(tmp_path))
    assert cli.main(["backup"]) == 0
    archive = capsysbinary.readouterr().out
    assert archive[:2] == b"\x1f\x8b"  # gzip
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(archive)))
    assert cli.main(["restore"]) == 0
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(b"nope")))
    assert cli.main(["restore"]) == 1

