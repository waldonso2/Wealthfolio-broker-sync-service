import pytest


@pytest.fixture(autouse=True)
def _no_recalc_wait(monkeypatch):
    # The fake Wealthfolio has its holdings at once; don't wait for a recalculation.
    monkeypatch.setenv("BROKERSYNC_RECALC_WAIT", "0")


@pytest.fixture(autouse=True)
def _no_scalable_pause(monkeypatch):
    # The pause between Scalable detail queries is for the real rate limit only.
    from brokersync.adapters import scalable

    monkeypatch.setattr(scalable, "DETAIL_PAUSE", 0)
