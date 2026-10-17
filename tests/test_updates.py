"""The hint at a newer release: asked once a day, shown on the overview, notified once."""

import json
from datetime import UTC, datetime, timedelta

import httpx

from brokersync import meta_keys, updates
from brokersync.state import State
from brokersync.sync import Syncer

from .helpers import first_password
from .test_sync import RecordingNotifier
from .test_web import make


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
