"""Contract tests: every adapter against recorded broker answers.

Each ``tests/contract/<adapter key>/*.json`` holds one case:

- ``recording``: what the broker answered (passed to ``Adapter.replay``)
- ``since``: the argument for ``get_transactions`` (ISO timestamp or null)
- ``expected``: the transactions the adapter must produce (``model.to_dict``)

When a broker changes its API, record the new answers into a new case: the
failing diff shows exactly what changed. Recordings are always fabricated or
anonymised - never real account data.
"""

import json
from datetime import datetime
from pathlib import Path

import pytest

from brokersync.adapters import ADAPTERS
from brokersync.mapping import Accounts, MappingError, to_activities
from brokersync.model import Kind, to_dict

HERE = Path(__file__).parent
CASES = sorted(p for p in HERE.glob("*/*.json"))


@pytest.mark.parametrize("path", CASES, ids=[f"{p.parent.name}/{p.stem}" for p in CASES])
def test_adapter_against_recording(path):
    case = json.loads(path.read_text())
    adapter = ADAPTERS[path.parent.name].replay(case["recording"])
    since = datetime.fromisoformat(case["since"]) if case.get("since") else None
    got = [to_dict(t) for t in adapter.get_transactions(since)]
    assert got == case["expected"]


@pytest.mark.parametrize("path", CASES, ids=[f"{p.parent.name}/{p.stem}" for p in CASES])
def test_every_recorded_transaction_can_be_booked(path):
    case = json.loads(path.read_text())
    adapter = ADAPTERS[path.parent.name].replay(case["recording"])
    for tx in adapter.get_transactions(None):
        if tx.kind == Kind.UNKNOWN:
            continue
        try:
            to_activities(tx, path.parent.name, Accounts("cash", "depot"))
        except MappingError as e:
            pytest.fail(f"{tx.id}: {e}")


def test_every_adapter_has_a_contract_case():
    assert {p.parent.name for p in CASES} >= set(ADAPTERS)
