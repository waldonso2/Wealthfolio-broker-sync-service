"""The duplicate-detection cases shared with the Broker Importer addon (contract/dedup/cases.json): the sync and
the addon must recognise the same activities as already in Wealthfolio."""

import json
from pathlib import Path

import pytest

from brokersync.dedup import ExistingIndex

CASES = json.loads((Path(__file__).parent.parent / "contract" / "dedup" / "cases.json").read_text())["cases"]


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_shared_dedup_case(case):
    index = ExistingIndex([dict(e) for e in case["existing"]])
    got = {}
    for a in case["incoming"]:
        payload = {"accountId": a["accountId"], "activityType": a["activityType"], "subtype": a.get("subtype"),
                   "activityDate": a["date"], "quantity": a.get("quantity"), "amount": a["amount"]}
        if a.get("symbol"):
            payload["asset"] = {"symbol": a["symbol"]}
        got[str(a["line"])] = index.find(f"[SYNC test:{a['line']}]", [payload])
    assert got == case["expect"]
