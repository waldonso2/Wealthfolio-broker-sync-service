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


@pytest.fixture(autouse=True)
def _fast_key_derivation(monkeypatch, tmp_path_factory):
    # argon2id with its real cost (64 MiB) for every login would make the suite slow; the
    # parameters themselves are checked in test_security. No /run in tests: the unlock cache is per process.
    from brokersync import crypto, vault

    monkeypatch.setattr(vault, "PASSWORD_HASH", {"time_cost": 1, "memory_cost": 1024, "parallelism": 1})
    monkeypatch.setattr(crypto, "ARGON2", {"time_cost": 1, "memory_cost": 1024, "parallelism": 1})
    monkeypatch.setenv("BROKERSYNC_RUNTIME", str(tmp_path_factory.mktemp("no-runtime") / "missing"))
    for var in ("CREDENTIALS_DIRECTORY", "BROKERSYNC_KEY_FILE", "BROKERSYNC_KEY_SOURCE"):
        monkeypatch.delenv(var, raising=False)
    crypto._memory.clear()
