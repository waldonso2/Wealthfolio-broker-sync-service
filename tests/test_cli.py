"""The command line the systemd units call: ``brokersync run`` (timer), ``serve`` (UI), password reset."""

import pytest

from brokersync import __version__, cli
from brokersync import sync as sync_mod
from brokersync.sync import AlreadyRunning, BrokerResult
from brokersync.vault import Vault, hash_password


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setenv("BROKERSYNC_DATA", str(tmp_path))
    return tmp_path


def fake_syncer(monkeypatch, results=None, error=None):
    calls = []

    class FakeSyncer:
        def __init__(self, data_dir):
            calls.append(("init", data_dir))

        def run(self, only=None):
            calls.append(("run", only))
            if error:
                raise error
            return results or []

    monkeypatch.setattr(sync_mod, "Syncer", FakeSyncer)
    return calls


def test_run_prints_a_line_per_broker_and_exits_0_when_all_went_well(data, monkeypatch, capsys):
    calls = fake_syncer(monkeypatch, [BrokerResult("fake", "ok", created=3, existing=1)])
    assert cli.main(["run"]) == 0
    assert calls == [("init", data), ("run", None)]
    assert capsys.readouterr().out == "fake: ok - 3 neu, 1 schon vorhanden, 0 fehlgeschlagen, 0 unbekannt\n"


def test_run_exits_1_when_a_broker_failed_so_systemd_shows_it(data, monkeypatch):
    fake_syncer(monkeypatch, [BrokerResult("dkb", "error", failed=1), BrokerResult("tr", "ok")])
    assert cli.main(["run"]) == 1


def test_run_for_named_brokers(data, monkeypatch):
    calls = fake_syncer(monkeypatch)
    assert cli.main(["run", "--broker", "dkb", "--broker", "tr"]) == 0
    assert calls[-1] == ("run", ["dkb", "tr"])


def test_run_while_another_sync_runs_is_not_an_error(data, monkeypatch, capsys):
    fake_syncer(monkeypatch, error=AlreadyRunning("A sync is already running."))
    assert cli.main(["run"]) == 0
    assert "already running" in capsys.readouterr().out


def test_run_with_an_empty_data_dir_does_nothing(data):
    assert cli.main(["run"]) == 0


def test_reset_ui_password_keeps_everything_else(data, capsys):
    vault = Vault(data)
    vault.update(lambda d: d.update(ui_password_hash=hash_password("geheim-123456"), wealthfolio_password="wf"))
    assert cli.main(["reset-ui-password"]) == 0
    assert vault.load() == {"wealthfolio_password": "wf"}
    assert "set a new one" in capsys.readouterr().out


def test_serve_starts_the_ui_on_the_given_port(data, monkeypatch):
    from brokersync.web import serve

    started = {}
    monkeypatch.setattr(serve, "serve", lambda data_dir, **kw: started.update(data_dir=data_dir, **kw))
    assert cli.main(["serve", "--port", "8443", "--certfile", "c.pem", "--keyfile", "k.pem",
                     "--redirect-port", "8090"]) == 0
    assert started == {"data_dir": data, "host": "0.0.0.0", "port": 8443, "certfile": "c.pem", "keyfile": "k.pem",
                       "redirect_port": 8090}


def test_version_and_missing_command(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["--version"])
    assert e.value.code == 0 and capsys.readouterr().out.strip() == __version__
    with pytest.raises(SystemExit) as e:
        cli.main([])
    assert e.value.code == 2


def test_install_sc_reports_and_fails_softly(tmp_path, monkeypatch, capsys):
    from brokersync import sc_install

    monkeypatch.setattr(sc_install, "install", lambda d: ("v1.1.0", True))
    assert cli.main(["install-sc", "--dir", str(tmp_path)]) == 0
    assert "Scalable CLI v1.1.0 installed" in capsys.readouterr().out

    def broken(d):
        raise sc_install.InstallError("Checksum doesn't match.")

    monkeypatch.setattr(sc_install, "install", broken)
    assert cli.main(["install-sc", "--dir", str(tmp_path)]) == 1
    assert "Checksum doesn't match" in capsys.readouterr().err
