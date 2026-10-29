"""The funds' holdings lists for the X-Ray addon, in Wealthfolio's addon storage (wealthfolio-importer-addon#146).

The addon (``waldonso2/wealthfolio-companion-addon-xray``) has no network: it reads what this module writes and
the values of the positions live from Wealthfolio, and computes X-Ray in the browser. The data contract is
``contract/xray/README.md`` (schema 1); this copy is leading.

- ``build`` (pure): the index (``xray:index``) and each fund's lines split into parts ``xray:f:<h>:<g>:<p>`` that
  stay under Wealthfolio's size limit (``fits``). Only public issuer data and the user's own asset ids, names and
  ISINs - never values, shares, accounts or costs.
- ``write``: parts of the new generation first, then the index (the switch), then the old generation's parts are
  deleted; unchanged content writes nothing. A delete that failed is kept in ``meta`` ``xray-addon:stale`` and
  tried again.
- ``clear``: the service switched off - every key it wrote goes.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal

from . import __version__, lookthrough, meta_keys
from .lookthrough import Depot
from .redact import redact
from .state import State
from .wealthfolio import StorageUnsupported, WealthfolioClient

ADDON_ID = "broker-sync-xray"
SCHEMA = 1
INDEX = "xray:index"
# Wealthfolio refuses a value whose sync payload is over 250 000 bytes; this leaves room.
LIMIT = 230_000


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def fits(key: str, value: str) -> bool:
    """Wealthfolio's own measure: the compact JSON of {addon_id, key, value} in UTF-8 bytes."""
    return len(_json({"addon_id": ADDON_ID, "key": key, "value": value}).encode()) <= LIMIT


def digest(lines: list) -> str:
    return hashlib.sha256(_json(lines).encode()).hexdigest()


def asset_hash(asset_id: str) -> str:
    return hashlib.sha256(asset_id.encode()).hexdigest()[:16]


def part_key(h: str, generation: int, part: int) -> str:
    return f"xray:f:{h}:{generation}:{part}"


def _parts(asset_id: str, h: str, generation: int, lines: list) -> list[str]:
    """The lines in order, each part as full as the size limit allows."""
    out: list[list] = []
    current: list = []
    for line in lines:
        trial = [*current, line]
        if current and not fits(part_key(h, generation, len(out)), _part(asset_id, len(out), 999, trial)):
            out.append(current)
            trial = [line]
        current = trial
    out.append(current)
    return [_part(asset_id, i, len(out), p) for i, p in enumerate(out)]


def _part(asset_id: str, part: int, parts: int, lines: list) -> str:
    return _json({"schema": SCHEMA, "asset_id": asset_id, "part": part, "parts": parts, "lines": lines})


def _source(st: dict) -> str:
    if st.get("status") == "upload" or st.get("source") == "Upload":
        return "upload"
    return "saved_page" if st.get("url") else "issuer"


def _ter_source(source: str) -> str | None:
    if not source:
        return None
    return {"manual": "manual", "Finanzfluss": "finanzfluss"}.get(source, "issuer")


@dataclass
class Built:
    index: dict
    parts: dict[str, str] = field(default_factory=dict)


def build(state: State, depot: Depot, isins: dict[str, str], generation: int) -> Built:
    """The index and the parts of ``generation`` for the depot's positions (pure but for reading the state)."""
    funds = state.funds()
    built = Built({"schema": SCHEMA, "generation": generation, "written_at": "", "service_version": __version__,
                   "base_currency": depot.base, "last_pass": {}, "isins": {}, "funds": {}, "missing": []})
    last = lookthrough.last_pass(state)
    built.index["last_pass"] = {"at": last.get("at"), "error": redact(last["error"]) if last.get("error") else None}
    for p in depot.positions:
        isin = p.get("isin") or isins.get(p["id"], "")
        if isin:
            built.index["isins"][p["id"]] = isin
        st = lookthrough.status(state, p["id"])
        fund = funds.get(p["id"])
        if fund is None:
            if lookthrough.is_fund(p, st):
                reason = st.get("error") or lookthrough._unknown(lookthrough.issuer(p, st), isin)
                built.index["missing"].append({"asset_id": p["id"], "name": p["name"], "isin": isin,
                                               "reason": redact(reason)})
            continue
        lines = fund["constituents"]
        h = asset_hash(p["id"])
        parts = _parts(p["id"], h, generation, lines)
        for i, value in enumerate(parts):
            built.parts[part_key(h, generation, i)] = value
        ter, ter_source = lookthrough.ter(st)
        built.index["funds"][p["id"]] = {
            "h": h, "name": p["name"], "isin": isin, "issuer": lookthrough.issuer(p, st) or None,
            "fetched_at": fund["at"], "source": _source(st), "note": st.get("note"), "ter": str(ter) if ter else None,
            "ter_source": _ter_source(ter_source) if ter else None, "lines": len(lines),
            "coverage": str(sum((Decimal(line[2]) for line in lines), Decimal(0))),
            "parts": len(parts), "sha256": digest(lines)}
    return built


def _same(a: dict | None, b: dict) -> bool:
    """Equal content, whatever its generation and moment of writing."""
    def strip(d: dict) -> dict:
        return {k: v for k, v in d.items() if k not in ("generation", "written_at")}
    return a is not None and strip(a) == strip(b)


def _keys(index: dict | None) -> list[str]:
    if not index:
        return []
    g = index.get("generation", 0)
    return [part_key(f["h"], g, i) for f in index.get("funds", {}).values() for i in range(f.get("parts", 0))]


@dataclass
class Result:
    written: int = 0
    deleted: int = 0
    skipped: bool = False


def write(state: State, wf: WealthfolioClient, depot: Depot, isins: dict[str, str], now: str) -> Result:
    """Write the depot's lists for the addon; unchanged content writes nothing (``skipped``)."""
    result = Result()
    old = _read_index(wf)
    built = build(state, depot, isins, (old or {}).get("generation", 0) + 1)
    _delete(state, wf, [], result)  # leftovers of an earlier run first
    if _same(old, built.index):
        result.skipped = True
        return result
    for key, value in built.parts.items():
        wf.storage_set(ADDON_ID, key, value)
        result.written += 1
    built.index["written_at"] = now
    index = _json(built.index)
    if not fits(INDEX, index):
        raise ValueError("Zu viele Fonds für den Addon-Speicher (Index über 230 000 Bytes).")
    wf.storage_set(ADDON_ID, INDEX, index)  # the switch: from now on the addon reads the new parts
    result.written += 1
    _delete(state, wf, _keys(old), result)
    return result


def clear(state: State, wf: WealthfolioClient) -> Result:
    """The service switched off: the index and every part it names go."""
    result = Result()
    old = _read_index(wf)
    keys = [*_keys(old), INDEX] if old else []
    _delete(state, wf, keys, result)
    return result


def _read_index(wf: WealthfolioClient) -> dict | None:
    raw = wf.storage_get(ADDON_ID, INDEX)
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None  # not ours to read: written anew


def _delete(state: State, wf: WealthfolioClient, keys: list[str], result: Result) -> None:
    """Delete ``keys`` and the ones left over; what fails stays in ``xray-addon:stale`` for the next run."""
    todo = list(dict.fromkeys([*json.loads(state.meta(meta_keys.XRAY_ADDON_STALE()) or "[]"), *keys]))
    failed = []
    for key in todo:
        try:
            wf.storage_delete(ADDON_ID, key)
            result.deleted += 1
        except StorageUnsupported:
            raise
        except Exception:
            failed.append(key)
    if failed:
        state.set_meta(meta_keys.XRAY_ADDON_STALE(), json.dumps(failed))
    else:
        state.delete_meta(meta_keys.XRAY_ADDON_STALE())
