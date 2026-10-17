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
                   "activityDate": a["date"], "quantity": a.get("quantity"), "amount": a["amount"],
                   "currency": a.get("currency")}
        if a.get("symbol"):
            payload["asset"] = {"symbol": a["symbol"]}
        got[str(a["line"])] = index.find(f"[SYNC test:{a['line']}]", [payload])
    assert got == case["expect"]


def test_index_sees_activities_appended_after_a_find_and_keeps_the_window_edge():
    from brokersync.dedup import MATCH_WINDOW_SECONDS, ExistingIndex

    def dep(i, date, amount):
        return {"id": f"e{i}", "accountId": "c", "activityType": "DEPOSIT", "date": date, "amount": amount,
                "currency": "EUR", "comment": "import"}

    def payload(date, amount):
        return [{"accountId": "c", "activityType": "DEPOSIT", "activityDate": date, "amount": amount,
                 "currency": "EUR"}]

    index = ExistingIndex([dep(1, "2026-01-01T00:00:00Z", "10")])
    assert index.find("[SYNC x:1]", payload("2026-03-01T00:00:00Z", "10")) is None
    # The sync appends the legs it created during a run; the next find sees them.
    index.existing.append(dep(2, "2026-03-01T01:00:00Z", "10"))
    assert index.find("[SYNC x:2]", payload("2026-03-01T00:00:00Z", "10")) == "e2"
    # Exactly 36 hours away still matches, a second more doesn't.
    assert MATCH_WINDOW_SECONDS == 36 * 3600
    index.existing += [dep(3, "2026-05-02T12:00:00Z", "20"), dep(4, "2026-05-10T12:00:01Z", "30")]
    assert index.find("[SYNC x:3]", payload("2026-05-01T00:00:00Z", "20")) == "e3"
    assert index.find("[SYNC x:4]", payload("2026-05-09T00:00:00Z", "30")) is None
