"""Scalable's ``sc`` runs apart from the service's data and key (#93)."""

import subprocess

import pytest

from brokersync import sandbox
from brokersync.adapters.scalable import ScCli

needs_landlock = pytest.mark.skipif(not sandbox.supported(), reason="kernel without Landlock")


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"}, timeout=30)


@needs_landlock
def test_the_program_reads_and_writes_only_what_it_may(tmp_path):
    data, home = tmp_path / "data", tmp_path / "home"
    data.mkdir()
    home.mkdir()
    (data / "secrets.enc").write_text("vault")
    (home / "config.toml").write_text("ok")
    cat = sandbox.command("/bin/cat", [str(home / "config.toml")], read=[], write=[str(home)])
    assert run(cat).stdout == "ok"
    r = run(sandbox.command("/bin/cat", [str(data / "secrets.enc")], read=[], write=[str(home)]))
    assert r.returncode != 0 and "Permission denied" in r.stderr
    r = run(sandbox.command("/bin/sh", ["-c", f"echo x > {home}/session.json && echo y > {data}/x"], read=[],
                            write=[str(home)]))
    assert (home / "session.json").read_text() == "x\n" and not (data / "x").exists()
    # The system stays readable: certificates, resolver.
    assert run(sandbox.command("/bin/cat", ["/etc/hostname"], read=[], write=[])).returncode == 0


def test_sc_gets_no_key_and_no_service_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", "/run/credentials/wealthfolio-broker-sync.service")
    monkeypatch.setenv("BROKERSYNC_DATA", str(tmp_path))
    monkeypatch.setenv("BROKERSYNC_KEY_SOURCE", "systemd-creds")
    monkeypatch.setenv("LANG", "de_DE.UTF-8")
    cli = ScCli({})
    env = cli._env()
    assert "CREDENTIALS_DIRECTORY" not in env and not [k for k in env if k.startswith("BROKERSYNC")]
    assert env["LANG"] == "de_DE.UTF-8" and env["HOME"] == str(cli.home) and env["TMPDIR"] == str(cli.home)
