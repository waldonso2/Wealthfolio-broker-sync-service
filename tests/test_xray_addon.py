"""The funds' lists for the X-Ray addon in Wealthfolio's addon storage (wealthfolio-importer-addon#146)."""

import json
from decimal import Decimal as D

import pytest

from brokersync import config as config_mod
from brokersync import lookthrough, meta_keys, xray_addon
from brokersync.lookthrough import Depot
from brokersync.state import State
from brokersync.wealthfolio import StorageUnsupported, WealthfolioClient

from .fakes import PASSWORD, FakeWealthfolio

A = xray_addon.ADDON_ID


def _wf(fake):
    return WealthfolioClient("http://wf.local:8080", PASSWORD, transport=fake.transport())


def _depot():
    return Depot(positions=[
        {"id": "a-world", "isin": "IE00B4L5Y983", "name": "iShares Core MSCI World UCITS ETF", "currency": "EUR",
         "value": D(1000)},
        {"id": "a-ubs", "isin": "LU0000000000", "name": "UBS Example UCITS ETF", "currency": "EUR", "value": D(50)},
        {"id": "a-sap", "isin": "DE0007164600", "name": "SAP SE", "currency": "EUR", "value": D(20)}])


def _state(tmp_path, lines=None):
    state = State(tmp_path)
    state.set_fund("a-world", "World", lines or [["US0378331005", "APPLE INC", "0.05", "US", "IT", "USD"],
                                                 ["~X", 'Müller "AG"', "0.01", "DE", "Industrials", "EUR"]])
    lookthrough.set_status(state, "a-world", status="ok", source="iShares", ter="0.002", ter_source="iShares")
    lookthrough.set_status(state, "a-ubs", status="none", error="")
    return state


def _stored(fake):
    return {k: v for (a, k), v in fake.storage.items() if a == A}


def _read(fake):
    """What the addon reads: the index, then every part of its generation."""
    store = _stored(fake)
    index = json.loads(store["xray:index"])
    lines = {}
    for asset, f in index["funds"].items():
        parts = [json.loads(store[xray_addon.part_key(f["h"], index["generation"], i)]) for i in range(f["parts"])]
        lines[asset] = [line for p in parts for line in p["lines"]]
        assert xray_addon.digest(lines[asset]) == f["sha256"]
    return index, lines


def test_a_large_fund_is_split_under_the_limit_and_reads_back_whole(tmp_path):
    lines = [[f"XS{i:010d}", f'Größe "Ölkonzern" Nr. {i} ' + "ä" * 40, "0.0002", "DE", "Energie", "EUR"]
             for i in range(4000)]
    fake = FakeWealthfolio()
    result = xray_addon.write(_state(tmp_path, lines), _wf(fake), _depot(), {}, "2026-10-29T08:00:00+00:00")
    index, read = _read(fake)
    f = index["funds"]["a-world"]
    assert f["parts"] > 1 and read["a-world"] == lines and result.written == f["parts"] + 1
    assert all(xray_addon.fits(k, v) for k, v in _stored(fake).items())
    assert f["coverage"] == "0.8000" and f["ter"] == "0.002" and f["ter_source"] == "issuer"


def test_the_index_names_isins_and_why_a_fund_has_no_list_never_values(tmp_path):
    fake = FakeWealthfolio()
    xray_addon.write(_state(tmp_path), _wf(fake), _depot(), {}, "2026-10-29T08:00:00+00:00")
    index, read = _read(fake)
    assert index["schema"] == 1 and index["base_currency"] == "EUR" and index["generation"] == 1
    assert index["isins"] == {"a-world": "IE00B4L5Y983", "a-ubs": "LU0000000000", "a-sap": "DE0007164600"}
    assert [m["asset_id"] for m in index["missing"]] == ["a-ubs"] and "UBS" in index["missing"][0]["reason"]
    assert read["a-world"][1][1] == 'Müller "AG"'
    raw = json.dumps(_stored(fake))
    assert "1000" not in raw and "value" not in json.dumps(index)


def test_unchanged_writes_nothing_a_change_switches_generation_parts_first(tmp_path):
    fake = FakeWealthfolio()
    state = _state(tmp_path)
    xray_addon.write(state, _wf(fake), _depot(), {}, "t1")
    fake.storage_log.clear()
    assert xray_addon.write(state, _wf(fake), _depot(), {}, "t2").skipped
    assert [m for m, _ in fake.storage_log] == ["GET"]  # only the index read
    state.set_fund("a-world", "World", [["US0378331005", "APPLE INC", "0.06", "US", "IT", "USD"]])
    fake.storage_log.clear()
    xray_addon.write(state, _wf(fake), _depot(), {}, "t3")
    writes = [(m, k) for m, k in fake.storage_log if m != "GET"]
    h = xray_addon.asset_hash("a-world")
    assert writes == [("PUT", f"xray:f:{h}:2:0"), ("PUT", "xray:index"), ("DELETE", f"xray:f:{h}:1:0")]
    index, read = _read(fake)
    assert index["generation"] == 2 and read["a-world"][0][2] == "0.06" and len(_stored(fake)) == 2


def test_a_failed_part_keeps_the_old_index_a_failed_delete_is_done_later(tmp_path):
    fake = FakeWealthfolio()
    state = _state(tmp_path)
    xray_addon.write(state, _wf(fake), _depot(), {}, "t1")
    h = xray_addon.asset_hash("a-world")
    state.set_fund("a-world", "World", [["US0378331005", "APPLE INC", "0.07", "US", "IT", "USD"]])
    fake.storage_fail = {f"xray:f:{h}:2:0"}
    with pytest.raises(Exception):  # noqa: B017 - Wealthfolio's refusal, as the run sees it
        xray_addon.write(state, _wf(fake), _depot(), {}, "t2")
    assert json.loads(_stored(fake)["xray:index"])["generation"] == 1  # the addon still reads the old lists
    fake.storage_fail = {f"xray:f:{h}:1:0"}
    xray_addon.write(state, _wf(fake), _depot(), {}, "t3")
    assert json.loads(state.meta(meta_keys.XRAY_ADDON_STALE())) == [f"xray:f:{h}:1:0"]
    fake.storage_fail = set()
    xray_addon.write(state, _wf(fake), _depot(), {}, "t4")  # unchanged, but the leftover goes
    assert state.meta(meta_keys.XRAY_ADDON_STALE()) is None and f"xray:f:{h}:1:0" not in _stored(fake)


def test_an_uninstalled_addon_gets_everything_again_and_clear_removes_all(tmp_path):
    fake = FakeWealthfolio()
    state = _state(tmp_path)
    xray_addon.write(state, _wf(fake), _depot(), {}, "t1")
    fake.storage.clear()  # Wealthfolio removes an addon's storage on uninstall
    xray_addon.write(state, _wf(fake), _depot(), {}, "t2")
    assert _read(fake)[0]["generation"] == 1
    xray_addon.clear(state, _wf(fake))
    assert _stored(fake) == {}


def test_the_service_is_off_unless_switched_on_and_says_when_wealthfolio_is_too_old(tmp_path):
    from .test_sync import setup

    fake = FakeWealthfolio()
    syncer, _ = setup(tmp_path, fake)
    cfg = config_mod.load(tmp_path)
    assert cfg.services is None and not cfg.service("xray") and cfg.service("sync")  # set up before: stays off
    state = _state(tmp_path)
    syncer.state = state
    with _wf(fake) as wf:
        syncer._write_xray_addon(wf, _depot(), cfg)
        assert _stored(fake) == {} and state.meta(meta_keys.XRAY_ADDON_STATUS()) is None
        cfg.set_service("xray", True)
        syncer._write_xray_addon(wf, _depot(), cfg)
        assert "xray:index" in _stored(fake) and json.loads(state.meta(meta_keys.XRAY_ADDON_STATUS()))["ok"]
        cfg.set_service("xray", False)
        syncer._write_xray_addon(wf, _depot(), cfg)  # switched off: everything goes
        assert _stored(fake) == {} and state.meta(meta_keys.XRAY_ADDON_STATUS()) is None
        fake.storage = None
        cfg.set_service("xray", True)
        syncer._write_xray_addon(wf, _depot(), cfg)
    status = json.loads(state.meta(meta_keys.XRAY_ADDON_STATUS()))
    assert not status["ok"] and "3.11.0" in status["error"]
    with pytest.raises(StorageUnsupported), _wf(fake) as wf:
        wf.storage_get(A, "xray:index")
