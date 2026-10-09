import pytest


@pytest.fixture(autouse=True)
def _no_recalc_wait(monkeypatch):
    # The fake Wealthfolio has its holdings at once; don't wait for a recalculation.
    monkeypatch.setenv("BROKERSYNC_RECALC_WAIT", "0")
