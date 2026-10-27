"""Missing ISINs on the Wealthfolio assets the sync knows (waldonso2/wealthfolio-importer-addon#140)."""

from brokersync import assets, meta_keys
from brokersync.state import State
from brokersync.wealthfolio import WealthfolioClient

from .fakes import PASSWORD, FakeWealthfolio

APPLE, SAP = "US0378331005", "DE0007164600"


def asset(asset_id, **kw):
    return {"id": asset_id, "name": f"Name {asset_id}", "displayCode": asset_id, "kind": "INVESTMENT",
            "notes": "my own note", "quoteMode": "MARKET", "quoteCcy": "USD", "instrumentType": "EQUITY",
            "instrumentSymbol": asset_id, "instrumentExchangeMic": "XNAS", "providerConfig": {"p": 1}, **kw}


def client(wf):
    return WealthfolioClient("http://wf.local:8080", PASSWORD, transport=wf.transport())


def test_only_a_missing_isin_is_added_and_everything_else_is_kept(tmp_path):
    wf = FakeWealthfolio()
    profile = {"profile": {"sectors": "x"}, "bond": {"coupon": 1}, "identifiers": {"wkn": "865985"}}
    wf.assets = [asset("AAPL", metadata=profile),
                 asset("SAP", metadata={"identifiers": {"isin": "DE000SOMEONE"}}),  # someone else's ISIN stays
                 asset("OTHER")]  # not known to the sync: never touched
    before = [dict(a) for a in wf.assets]
    state = State(tmp_path)
    learned = {"AAPL": APPLE, "SAP": SAP, "BAD": "US0000000000"}
    assert assets.complete_isins(state, client(wf), learned) == 1
    aapl = wf.assets[0]
    assert aapl["metadata"] == {**profile, "identifiers": {"wkn": "865985", "isin": APPLE}}
    assert {k: v for k, v in aapl.items() if k != "metadata"} == {k: v for k, v in before[0].items()
                                                                   if k != "metadata"}
    assert wf.assets[1:] == before[1:]
    # Removed by the user: not set again.
    aapl["metadata"] = profile
    assert assets.complete_isins(state, client(wf), learned) == 0
    assert state.meta(meta_keys.ISIN("AAPL")) == APPLE


def test_a_run_completes_isins_and_a_refusal_never_fails_a_broker(tmp_path):
    from .test_sync import setup

    wf = FakeWealthfolio()
    syncer, _ = setup(tmp_path, wf)
    [r] = syncer.run()  # learns the fake broker's fund
    known = next(iter(State(tmp_path).assets("fake").values()))["asset_id"]
    wf.assets = [asset(known, metadata=None)]
    [r] = syncer.run()
    assert r.status == "ok" and wf.assets[0]["metadata"]["identifiers"]["isin"]
    wf.assets = None  # type: ignore[assignment]  # an answer that breaks the pass: never a broker's failure
    State(tmp_path).delete_meta(meta_keys.ISIN(known))
    assert syncer.run()[0].status == "ok"
