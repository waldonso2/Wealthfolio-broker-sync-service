"""The community-scripts layout the install line relies on - a mistake here
only shows up when someone runs the install on Proxmox, so it is checked here."""

import json
import re
import tomllib
from pathlib import Path

import brokersync

ROOT = Path(__file__).parent.parent
SLUG = "wealthfolio-broker-sync"
RAW = "https://raw.githubusercontent.com/waldonso2/wealthfolio-broker-sync-service/main"


def test_catalog_entry_matches_the_scripts():
    meta = json.loads((ROOT / "json" / f"{SLUG}.json").read_text())
    assert meta["slug"] == SLUG and meta["type"] == "ct"
    assert [m["script"] for m in meta["install_methods"]] == [f"ct/{SLUG}.sh"]
    assert (ROOT / "ct" / f"{SLUG}.sh").exists()
    assert (ROOT / "install" / f"{SLUG}-install.sh").exists()
    assert meta["interface_port"] == 8443


def test_ct_script_loads_the_engine_with_this_repository_as_script_source():
    ct = (ROOT / "ct" / f"{SLUG}.sh").read_text()
    lines = [ln for ln in ct.splitlines() if ln and not ln.startswith("#")]
    # COMMUNITY_SCRIPTS_URL must be set before the engine is sourced: build.func
    # resolves install/<slug>-install.sh and writes the container's `update`
    # command from it. Without it both come from community-scripts/ProxmoxVE.
    assert lines[0] == f'export COMMUNITY_SCRIPTS_URL="${{COMMUNITY_SCRIPTS_URL:-{RAW}}}"'
    assert lines[1].startswith("source <(curl -fsSL ") and "/core/build.func" in lines[1]
    assert "community-scripts/core/main" in lines[1]
    # build.func derives the install script name from APP: lower case, no spaces.
    app = re.search(r'^APP="([^"]+)"', ct, re.M).group(1)
    assert app.lower().replace(" ", "") == SLUG


def test_install_line_in_the_docs_matches_the_script():
    line = f'bash -c "$(curl -fsSL {RAW}/ct/{SLUG}.sh)"'
    assert line in (ROOT / "README.md").read_text()
    assert line in (ROOT / "ct" / f"{SLUG}.sh").read_text()


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


def test_setup_survives_its_own_pitfalls():
    setup = (ROOT / "deploy" / "setup.sh").read_text()
    # tar | grep -q under pipefail: grep stops early, tar gets SIGPIPE, the pipeline "fails" - the check never hit.
    assert not re.search(r"tar [^|]*\| *grep -q", setup)
    # Steps that talk to systemd never hang an update forever, and a run by hand restarts the service.
    assert "timeout 900 /usr/local/bin/brokersync-cli migrate" in setup and "rotate-key" in setup
    assert "systemctl try-restart wealthfolio-broker-sync" in setup


def test_the_cli_wrapper_runs_commands_like_the_web_service():
    cli = (ROOT / "deploy" / "brokersync-cli").read_text()
    unit = (ROOT / "deploy" / "systemd" / "wealthfolio-broker-sync.service").read_text()
    for env in ("BROKERSYNC_DATA", "BROKERSYNC_RUNTIME", "BROKERSYNC_TLS"):
        assert f"Environment={env}=" in unit and f"Environment={env}=" in cli
