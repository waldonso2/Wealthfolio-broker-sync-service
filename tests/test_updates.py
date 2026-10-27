"""The hint at a newer release: asked once a day, shown on the overview, notified once."""

import json
from datetime import UTC, datetime, timedelta

import httpx

from brokersync import meta_keys, updates
from brokersync.state import State
from brokersync.sync import Syncer

from .helpers import first_password
from .test_sync import RecordingNotifier
from .test_web import csrf, make


def github(tag: str | None, calls: list):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if tag is None:
            raise httpx.ConnectError("offline")
        return httpx.Response(302, headers={"location": f"{updates.RELEASES}/tag/{tag}"})

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_versions_compare_as_numbers():
    assert updates.newer("0.27.0", "0.26.1")
    assert updates.newer("0.10.0", "0.9.9")
    assert not updates.newer("0.26.1", "0.26.1")
    assert not updates.newer("0.26.0", "0.26.1")
    assert not updates.newer("garbage", "0.26.1")


def test_asked_once_a_day_and_a_new_version_reported_once(tmp_path, monkeypatch):
    monkeypatch.setenv("BROKERSYNC_UPDATE_CHECK", "1")
    state, calls = State(tmp_path), []
    assert updates.check(state, "0.26.1", http=github("v0.27.0", calls)) == "0.27.0"
    assert updates.available(state, "0.26.1") == "0.27.0"
    assert updates.available(state, "0.27.0") is None

    assert updates.check(state, "0.26.1", http=github("v0.27.0", calls)) is None  # within the day: not asked
    assert len(calls) == 1

    stored = json.loads(state.meta(meta_keys.UPDATE()))
    stored["checked"] = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    state.set_meta(meta_keys.UPDATE(), json.dumps(stored))
    assert updates.check(state, "0.26.1", http=github("v0.27.0", calls)) is None  # same version: no second notice
    assert len(calls) == 2


def test_github_not_reachable_keeps_what_was_known(tmp_path, monkeypatch):
    monkeypatch.setenv("BROKERSYNC_UPDATE_CHECK", "1")
    state = State(tmp_path)
    state.set_meta(meta_keys.UPDATE(), json.dumps({"version": "0.27.0", "checked": "2020-01-01T00:00:00+00:00"}))
    assert updates.check(state, "0.26.1", http=github(None, [])) is None
    assert updates.available(state, "0.26.1") == "0.27.0"


def test_turned_off_asks_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("BROKERSYNC_UPDATE_CHECK", "0")
    calls: list = []
    assert updates.check(State(tmp_path), "0.26.1", http=github("v9.0.0", calls)) is None
    assert calls == []


def test_a_run_notifies_a_new_release_once(tmp_path, monkeypatch):
    monkeypatch.setenv("BROKERSYNC_UPDATE_CHECK", "1")
    monkeypatch.setattr(updates, "latest", lambda http: "99.0.0")
    notifier = RecordingNotifier()
    syncer = Syncer(tmp_path, notifier=notifier)
    syncer.run()
    assert [(t, link) for t, _, link in notifier.sent] == [("Update verfügbar", updates.RELEASES)]
    syncer.run()
    assert len(notifier.sent) == 1


def test_the_overview_shows_a_newer_release(tmp_path):
    client, _, _ = make(tmp_path)
    first_password(client)
    assert "Update verfügbar" not in client.get("/").text
    State(tmp_path).set_meta(meta_keys.UPDATE(), json.dumps(
        {"version": "99.0.0", "checked": datetime.now(UTC).isoformat()}))
    page = client.get("/").text
    assert "Update verfügbar: Version 99.0.0" in page
    assert f"{updates.RELEASES}/tag/v99.0.0" in page


def test_the_updater_asks_only_when_switched_on_and_its_time_is_up():
    calls = []
    assert updates.due(False, 60, "0.26.1", 1e9, http=github("v0.27.0", calls)) == (False, None) and not calls
    assert updates.due(True, 300, "0.26.1", 299, http=github("v0.27.0", calls)) == (False, None) and not calls
    assert updates.due(True, 5, "0.26.1", 30, http=github("v0.27.0", calls)) == (False, None)  # at least a minute
    assert updates.due(True, 60, "0.26.1", 60, http=github("v0.27.0", calls)) == (True, "0.27.0")
    assert updates.due(True, 60, "0.27.0", 60, http=github("v0.27.0", calls)) == (True, None)
    assert updates.due(True, 60, "0.26.1", 60, http=github(None, calls)) == (True, None)  # offline: nothing


def test_the_updater_reads_its_switch_from_the_host_tier_and_reports_the_result(tmp_path, monkeypatch, capsys):
    from brokersync import cli, config

    monkeypatch.setenv("BROKERSYNC_DATA", str(tmp_path))
    config.update(tmp_path, lambda c: (setattr(c, "auto_update", True), setattr(c, "update_interval", 120)))
    assert config.load_notify(tmp_path).auto_update and config.load_notify(tmp_path).update_interval == 120
    monkeypatch.setattr(updates, "latest", lambda http: "99.0.0")
    assert cli.main(["update-due", "--since", "60"]) == 0 and capsys.readouterr().out == ""
    assert cli.main(["update-due", "--since", "120"]) == 0
    assert capsys.readouterr().out.split("\n")[:2] == ["checked", "install 99.0.0"]
    assert cli.main(["update-done", "--from", "0.33.2", "--to", "99.0.0"]) == 0
    last = json.loads(State(tmp_path).meta(meta_keys.AUTO_UPDATE()))
    assert (last["from"], last["to"], last["ok"]) == ("0.33.2", "99.0.0", True)
    assert cli.main(["update-done", "--from", "0.33.2", "--to", "99.0.1", "--failed"]) == 1
    assert "fehlgeschlagen" in capsys.readouterr().out


def test_the_page_updates_switches_it_on_with_a_bounded_interval(tmp_path):
    from brokersync import config

    client, _, _ = make(tmp_path)
    first_password(client)
    page = client.get("/updates").text
    assert "Neue Versionen automatisch installieren" in page and 'value="60"' in page
    token = csrf(client, "/updates")
    for bad in ("30", "abc", "90,5"):
        r = client.post("/updates", data={"csrf": token, "auto_update": "1", "interval": bad})
        assert 'role="alert"' in r.text and not config.load(tmp_path).auto_update
    r = client.post("/updates", data={"csrf": token, "auto_update": "1", "interval": "300"})
    assert "Gespeichert." in r.text
    c = config.load(tmp_path)
    assert c.auto_update and c.update_interval == 300
    State(tmp_path).set_meta(meta_keys.AUTO_UPDATE(), json.dumps(
        {"from": "0.33.2", "to": "0.34.0", "ok": False, "at": datetime.now(UTC).isoformat()}))
    assert "Update auf 0.34.0 fehlgeschlagen, Version 0.33.2 läuft weiter" in client.get("/updates").text
