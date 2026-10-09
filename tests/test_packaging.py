"""The community-scripts layout PVE Scripts Local expects - a mistake here
only shows up when someone clicks "Install", so it is checked here."""

import json
import re
import tomllib
from pathlib import Path

import brokersync

ROOT = Path(__file__).parent.parent
SLUG = "wealthfolio-broker-sync"
# PVE Scripts Local rewrites exactly this line to its bundled build.func
# (scriptDownloader.js, modifyScriptContent); anything else would fetch the
# install script from community-scripts/ProxmoxVE instead of this repository.
BUILD_FUNC_LINE = "source <(curl -fsSL https://raw.githubusercontent.com/community-scripts/ProxmoxVE/main/misc/build.func)"


def test_catalog_entry_matches_the_scripts():
    meta = json.loads((ROOT / "json" / f"{SLUG}.json").read_text())
    assert meta["slug"] == SLUG and meta["type"] == "ct"
    assert [m["script"] for m in meta["install_methods"]] == [f"ct/{SLUG}.sh"]
    assert (ROOT / "ct" / f"{SLUG}.sh").exists()
    assert (ROOT / "install" / f"{SLUG}-install.sh").exists()
    assert meta["interface_port"] == 8090


def test_ct_script_is_rewritable_by_pve_scripts_local():
    ct = (ROOT / "ct" / f"{SLUG}.sh").read_text().splitlines()
    assert ct[1] == BUILD_FUNC_LINE
    # build.func derives the install script name from APP: lower case, no spaces.
    app = re.search(r'^APP="([^"]+)"', "\n".join(ct), re.M).group(1)
    assert app.lower().replace(" ", "") == SLUG


def test_update_points_to_this_repository():
    install = (ROOT / "install" / f"{SLUG}-install.sh").read_text()
    assert f"waldonso2/wealthfolio-broker-sync-service/main/ct/{SLUG}.sh" in install
    assert install.index("customize") < install.index("/usr/bin/update")


def test_units_and_setup_agree_on_paths():
    setup = (ROOT / "deploy" / "setup.sh").read_text()
    for unit in (ROOT / "deploy" / "systemd").iterdir():
        text = unit.read_text()
        if unit.suffix == ".service":
            assert "ExecStart=/opt/wealthfolio-broker-sync/venv/bin/brokersync" in text
            assert "BROKERSYNC_DATA=/opt/wealthfolio-broker-sync/data" in text
            assert "User=brokersync" in text
    assert "USER_NAME=brokersync" in setup and 'BASE=/opt/wealthfolio-broker-sync' in setup
    ct = (ROOT / "ct" / f"{SLUG}.sh").read_text()
    for name in ("wealthfolio-broker-sync", "wealthfolio-broker-sync-run.timer"):
        assert name in ct


def test_version_is_the_same_everywhere():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert project["version"] == brokersync.__version__
    changelog = (ROOT / "CHANGELOG.md").read_text()
    assert f"## {brokersync.__version__}" in changelog
