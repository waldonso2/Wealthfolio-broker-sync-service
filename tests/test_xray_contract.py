"""contract/xray/cases.json is what xray.py computes today (wealthfolio-importer-addon#146): the X-Ray addon checks its
own computation against the same file. After a change to xray.py: ``python scripts/xray_cases.py >
contract/xray/cases.json`` - and the addon's copy in the same change."""

import json
import runpy
from pathlib import Path

ROOT = Path(__file__).parent.parent


def test_the_shared_cases_match_xray():
    module = runpy.run_path(str(ROOT / "scripts" / "xray_cases.py"))
    assert json.loads((ROOT / "contract" / "xray" / "cases.json").read_text()) == module["cases"]()
