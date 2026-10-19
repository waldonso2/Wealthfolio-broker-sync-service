"""Schema version of state.db with numbered migrations, and the central list of meta keys (#77)."""

import pytest

from brokersync import meta_keys
from brokersync import state as state_mod
from brokersync.state import State


def _as_0_13(tmp_path) -> None:
    """Turn a fresh state.db back into what 0.13.x wrote: balances without the account column, version 0."""
    s = State(tmp_path)
    with s.db:
        s.db.execute("DROP TABLE balances")
        s.db.execute("CREATE TABLE balances (broker TEXT NOT NULL, currency TEXT NOT NULL, amount TEXT NOT NULL, "
                     "at TEXT NOT NULL, PRIMARY KEY (broker, currency))")
        s.db.executemany("INSERT INTO balances VALUES (?, ?, ?, ?)", [
            ("dkb", "EUR", "502.68", "2026-10-01T06:00:00+00:00"),
            ("dkb", "EUR:savings", "10000.00", "2026-10-01T06:00:00+00:00"),
            ("tr", "EUR", "12.34", "2026-10-02T06:00:00+00:00"),
        ])
        s.db.execute("DROP TABLE events")  # 0.22.0 (#102)
        s.db.execute("DROP TABLE completions")  # waldonso2/wealthfolio-importer-addon#132
        s.db.execute("PRAGMA user_version = 0")
    s.mark("dkb", "tx-1", "imported", ["a1", "a2"])
    s.close()


def test_a_database_of_0_13_is_migrated_without_loss(tmp_path):
    _as_0_13(tmp_path)
    s = State(tmp_path)
    assert s.db.execute("PRAGMA user_version").fetchone()[0] == state_mod.SCHEMA_VERSION
    assert s.balances("dkb") == [
        {"currency": "EUR", "account": "", "amount": "502.68", "at": "2026-10-01T06:00:00+00:00"},
        {"currency": "EUR", "account": "savings", "amount": "10000.00", "at": "2026-10-01T06:00:00+00:00"},
    ]
    assert s.balances("tr")[0]["amount"] == "12.34"
    assert s.synced("dkb") == {"tx-1": ("imported", ["a1", "a2"])}
    s.set_balances("dkb", [("EUR", "1", ""), ("EUR", "2", "savings"), ("USD", "3")])
    assert [(b["currency"], b["account"], b["amount"]) for b in s.balances("dkb")] == [
        ("EUR", "", "1"), ("EUR", "savings", "2"), ("USD", "", "3")]
    s.close()
    assert State(tmp_path).balances("dkb")[1]["account"] == "savings"  # opening again changes nothing
    s = State(tmp_path)
    s.add_completion("tr", "ev-1", {"kind": "split", "isin": "US0378331005", "ratio": "0.1", "day": "2026-04-20"})
    assert s.completions("tr") == {("tr", "ev-1"): {"kind": "split", "isin": "US0378331005", "ratio": "0.1",
                                                     "day": "2026-04-20"}}


def test_a_fresh_database_gets_the_current_schema_and_a_newer_one_is_refused(tmp_path):
    s = State(tmp_path)
    assert s.db.execute("PRAGMA user_version").fetchone()[0] == state_mod.SCHEMA_VERSION
    with s.db:
        s.db.execute(f"PRAGMA user_version = {state_mod.SCHEMA_VERSION + 1}")
    s.close()
    with pytest.raises(state_mod.NewerSchema):
        State(tmp_path)


def test_a_failing_migration_changes_nothing(tmp_path, monkeypatch):
    _as_0_13(tmp_path)

    def broken(db):
        state_mod._balances_account_column(db)
        raise RuntimeError("boom")

    monkeypatch.setattr(state_mod, "MIGRATIONS", [broken])
    with pytest.raises(RuntimeError):
        State(tmp_path)
    monkeypatch.undo()
    assert State(tmp_path).balances("dkb")[1]["account"] == "savings"  # the real one still runs afterwards


def test_meta_keys_of_a_broker_are_forgotten_and_reset_but_no_others(tmp_path):
    s = State(tmp_path)
    for k in meta_keys.BROKER_KEYS:
        s.set_meta(k("dkb"), "x")
        s.set_meta(k("tr"), "x")
    s.set_meta(meta_keys.LOGO("asset:dkb"), "{}")
    s.set_meta(meta_keys.LOGOS_LAST(), "{}")
    s.reset_broker("dkb")
    assert all(s.meta(k("dkb")) is None and s.meta(k("tr")) == "x" for k in meta_keys.BROKER_KEYS)
    assert s.meta(meta_keys.LOGO("asset:dkb")) == "{}" and s.meta(meta_keys.LOGOS_LAST()) == "{}"
    s.forget_broker("tr")
    assert all(s.meta(k("tr")) is None for k in meta_keys.BROKER_KEYS)
    assert meta_keys.REFETCH("dkb") == "refetch:dkb" and meta_keys.LOGOS_LAST() == "logos:last"
