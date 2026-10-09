"""Removes what an installation still holds of brokers this version no longer has.

The test broker "Dummy" (key ``dummy``) existed up to 0.3.6. Its config, saved
credentials and session, sync state, runs, unknown events and holdings check
are removed on the next start. What it booked in Wealthfolio stays - deleting
activities is the user's call; the overview shows once how many there are and
how to find them (comment ``[SYNC dummy:``).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from . import config as config_mod
from .state import State
from .vault import Vault

log = logging.getLogger(__name__)

RETIRED = {"dummy": "Dummy (Test)"}
NOTICE = "retired-notice:"


def clean_up(data_dir: Path, state: State, vault: Vault) -> None:
    cfg = config_mod.load(data_dir)
    if any(k in cfg.brokers for k in RETIRED):
        for key in RETIRED:
            cfg.brokers.pop(key, None)
        config_mod.save(data_dir, cfg)
    if any(k in vault.load().get("brokers", {}) for k in RETIRED):
        def drop(d: dict) -> None:
            for k in RETIRED:
                d.get("brokers", {}).pop(k, None)

        vault.update(drop)
    for key, label in RETIRED.items():
        if not state.known(key) and not state.runs(key, 1) and not state.unknown_events(key):
            continue
        created = state.forget_broker(key)
        log.info("removed the retired broker %s (%d activities stay in Wealthfolio)", key, created)
        if created:
            state.set_meta(NOTICE + key, json.dumps({"label": label, "activities": created,
                                                     "search": f"[SYNC {key}:"}))


def notices(state: State) -> list[dict]:
    """One per retired broker that left activities in Wealthfolio, until dismissed."""
    out = []
    for key in RETIRED:
        raw = state.meta(NOTICE + key)
        if raw:
            out.append({"key": key, **json.loads(raw)})
    return out


def dismiss(state: State, key: str) -> None:
    state.delete_meta(NOTICE + key)
