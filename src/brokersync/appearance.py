"""The font the user chose in Wealthfolio (Settings → Appearance), so the UI shows the same one.

Read from ``GET /settings`` when the connection is saved and once per run, kept in ``meta``
``wealthfolio:appearance``. Only Wealthfolio's three font classes count; anything else, a Wealthfolio
without the endpoint or nothing read yet is ``mono`` - Wealthfolio's own default (theme Classic).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from . import meta_keys

FONTS = {"font-mono": "mono", "font-sans": "sans", "font-serif": "serif"}
DEFAULT = "mono"
KEY = meta_keys.WEALTHFOLIO_APPEARANCE()


def refresh(state, wf) -> str:
    """Reads the font from Wealthfolio and stores it; a failing request raises (the caller logs it)."""
    settings = wf.settings() or {}
    font = FONTS.get(str(settings.get("font")), DEFAULT)
    state.set_meta(KEY, json.dumps({"font": font, "at": datetime.now(UTC).isoformat()}))
    return font


def font(state) -> str:
    """The stored font: ``mono``, ``sans`` or ``serif``."""
    try:
        value = json.loads(state.meta(KEY) or "{}").get("font")
    except (ValueError, AttributeError):
        return DEFAULT
    return value if value in FONTS.values() else DEFAULT
