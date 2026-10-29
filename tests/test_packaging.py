"""The community-scripts layout the install line relies on - a mistake here
only shows up when someone runs the install on Proxmox, so it is checked here."""

import json
import re
import tomllib
from pathlib import Path

import brokersync

ROOT = Path(__file__).parent.parent
SLUG = "wealthfolio-companion"
RAW = "https://raw.githubusercontent.com/waldonso2/wealthfolio-companion/main"


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
    # The engine runs as root on the host: pinned to a commit, set unconditionally (an `update`
    # command written by an older version still exports the URL it used), and loaded from there.
    assert re.fullmatch(r'export COMMUNITY_SCRIPTS_CORE_URL="https://raw\.githubusercontent\.com/'
                        r'community-scripts/core/[0-9a-f]{40}"', lines[1])
    assert lines[2] == 'source <(curl -fsSL "$COMMUNITY_SCRIPTS_CORE_URL/core/build.func")'
    assert "core/main" not in ct
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
        if unit.name == "wealthfolio-companion-update.service":
            # Root on purpose: the updater runs the console's update (opt-in on the page Updates).
            assert "User=root" in text and "ExecStart=/usr/local/bin/companion-autoupdate" in text
        elif unit.suffix == ".service":
            assert "ExecStart=/opt/wealthfolio-companion/venv/bin/wealthfolio-companion" in text
            assert "COMPANION_DATA=/opt/wealthfolio-companion/data" in text
            # Else only the certificate's renewal is root: it writes /etc/wealthfolio-companion/tls (#92).
            assert "User=companion" in text or ("User=root" in text and "renew-cert" in text)
    assert "USER_NAME=companion" in setup and 'BASE=/opt/wealthfolio-companion' in setup
    ct = (ROOT / "ct" / f"{SLUG}.sh").read_text()
    for name in ("wealthfolio-companion", "wealthfolio-companion-run.timer"):
        assert name in ct


def test_version_is_the_same_everywhere():
    """One place, pyproject.toml (#80): the package reads it from its installed metadata."""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert project["version"] == brokersync.__version__, "reinstall: pip install --no-deps --no-build-isolation -e ."
    changelog = (ROOT / "CHANGELOG.md").read_text()
    assert f"## {project['version']}" in changelog


def test_setup_survives_its_own_pitfalls():
    setup = (ROOT / "deploy" / "setup.sh").read_text()
    # tar | grep -q under pipefail: grep stops early, tar gets SIGPIPE, the pipeline "fails" - the check never hit.
    assert not re.search(r"tar [^|]*\| *grep -q", setup)
    # Steps that talk to systemd never hang an update forever, and a run by hand restarts the service.
    assert "timeout 900 /usr/local/bin/companion-cli migrate" in setup and "rotate-key" in setup
    assert "systemctl try-restart wealthfolio-companion" in setup


def test_the_updater_is_installed_and_runs_the_consoles_update_unattended():
    setup = (ROOT / "deploy" / "setup.sh").read_text()
    script = (ROOT / "deploy" / "companion-autoupdate").read_text()
    timer = (ROOT / "deploy" / "systemd" / "wealthfolio-companion-update.timer").read_text()
    assert "/usr/local/bin/companion-autoupdate" in setup
    assert "enable -q --now wealthfolio-companion-update.timer" in setup
    # The same, checked update as by hand; never while a fetch runs; nothing before the service says so.
    assert "PHS_SILENT=1 /usr/bin/update" in script
    assert "is-active --quiet wealthfolio-companion-run.service" in script
    assert script.index("update-due") < script.index("/usr/bin/update")
    assert "OnUnitInactiveSec=60s" in timer
    service = (ROOT / "deploy" / "systemd" / "wealthfolio-companion-update.service").read_text()
    assert "Environment=TERM=" in service  # the engine's `clear` fails without one (0.34.0)
    # 0077 left the new program root-only and the service unable to start (203/EXEC, 0.34.0).
    assert "UMask=0022" in service and "\numask 022\n" in setup


def test_the_cli_wrapper_runs_commands_like_the_web_service():
    cli = (ROOT / "deploy" / "companion-cli").read_text()
    unit = (ROOT / "deploy" / "systemd" / "wealthfolio-companion.service").read_text()
    for env in ("COMPANION_DATA", "COMPANION_RUNTIME", "COMPANION_TLS"):
        assert f"Environment={env}=" in unit and f"Environment={env}=" in cli


def test_core_pin_is_the_one_the_watch_workflow_compares():
    ct = (ROOT / "ct" / f"{SLUG}.sh").read_text()
    pin = re.search(r"community-scripts/core/([0-9a-f]{40})", ct).group(1)
    watch = (ROOT / ".github" / "workflows" / "core-watch.yml").read_text()
    assert "ct/wealthfolio-companion.sh" in watch and "community-scripts/core" in watch
    assert len(pin) == 40


def test_backups_are_never_readable_by_others():
    for path in (ROOT / "ct" / f"{SLUG}.sh", ROOT / "deploy" / "setup.sh"):
        for line in path.read_text().splitlines():
            if "tar -czf" in line:
                assert "umask 077" in line, f"{path.name}: {line.strip()}"


def test_setup_installs_only_the_locked_hashes():
    setup = (ROOT / "deploy" / "setup.sh").read_text()
    assert '--require-hashes --only-binary :all: -r "$APP/requirements.txt"' in setup
    assert '--no-deps --no-build-isolation "$APP"' in setup
    # Every install of the package goes through the lock.
    assert setup.count('bin/pip" install') == 1 and setup.count('"${PIP[@]}"') == 2


def _locked() -> dict[str, tuple[str, str]]:
    """requirements.txt: name -> (version, marker)."""
    out = {}
    for line in (ROOT / "requirements.txt").read_text().splitlines():
        m = re.match(r"^([A-Za-z0-9_.-]+)==([^ ;\\]+)\s*(?:;\s*([^\\]+?))?\s*\\", line)
        if m:
            out[m.group(1).lower().replace("_", "-")] = (m.group(2), (m.group(3) or "").strip())
    return out


def test_lock_covers_every_dependency_with_hashes():
    from packaging.markers import Marker
    from packaging.requirements import Requirement

    meta = tomllib.loads((ROOT / "pyproject.toml").read_text())
    locked = _locked()
    wanted = [Requirement(r) for r in meta["project"]["dependencies"]]
    wanted += [Requirement(r) for r in meta["build-system"]["requires"]]
    for req in wanted:
        name = req.name.lower().replace("_", "-")
        assert name in locked, f"{req.name} is missing in requirements.txt - run scripts/lock.sh"
        version, marker = locked[name]
        assert req.specifier.contains(version, prereleases=True), \
            f"{req.name}=={version} doesn't satisfy {req.specifier} - run scripts/lock.sh"
        if req.marker:
            assert str(Marker(marker)) == str(req.marker), f"{req.name}: marker {marker!r} - run scripts/lock.sh"
    # Every locked package carries at least one hash.
    text = (ROOT / "requirements.txt").read_text()
    assert text.count("--hash=sha256:") >= len(locked)
    blocks = re.split(r"\n(?=[A-Za-z0-9])", text.strip())
    assert all("--hash=sha256:" in b for b in blocks if "==" in b)


def _verify_release_function(path: Path) -> str:
    text = path.read_text()
    return text[text.index("verify_release() {"):text.index("\n}\n", text.index("verify_release() {")) + 2]


def test_install_and_update_check_the_release_against_its_checksums(tmp_path):
    """#88: what the release workflow lists in SHA256SUMS is what install and update accept - a changed or
    an added file stops them; what pip leaves behind (build/, *.egg-info) doesn't."""
    import subprocess

    func = _verify_release_function(ROOT / "ct" / "wealthfolio-companion.sh")
    assert func == _verify_release_function(ROOT / "install" / "wealthfolio-companion-install.sh")
    release = (ROOT / ".github" / "workflows" / "release.yml").read_text()
    assert "find . -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum) > SHA256SUMS" in release
    app = tmp_path / "app"
    app.mkdir()
    for name, content in {"setup.sh": "echo hi\n", "src/pkg/mod.py": "x = 1\n", "with space.txt": "y\n"}.items():
        (app / name).parent.mkdir(parents=True, exist_ok=True)
        (app / name).write_text(content)
    sums = tmp_path / "SHA256SUMS"
    sums.write_text(subprocess.run("find . -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum", shell=True,
                                   cwd=app, capture_output=True, text=True, check=True).stdout)
    (tmp_path / ".wealthfolio-companion").write_text("0.1.0a1\n")

    def verify() -> int:
        script = f'curl() {{ [[ "$*" == *"/v0.1.0a1/SHA256SUMS"* ]] && cp "{sums}" "${{@: -1}}"; }}\n{func}\n' \
                 f'verify_release "{app}"'
        return subprocess.run(["bash", "-c", script], env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}).returncode

    assert verify() == 0
    (app / "build" / "lib").mkdir(parents=True)
    (app / "build" / "lib" / "mod.py").write_text("x = 1\n")
    (app / "src" / "pkg.egg-info").mkdir()
    (app / "src" / "pkg.egg-info" / "PKG-INFO").write_text("Name: pkg\n")
    assert verify() == 0
    (app / "src" / "pkg" / "evil.py").write_text("import os\n")
    assert verify() == 1  # an added file
    (app / "src" / "pkg" / "evil.py").unlink()
    (app / "setup.sh").write_text("curl evil | sh\n")
    assert verify() == 1  # a changed file
    (app / "setup.sh").write_text("echo hi\n")
    (tmp_path / ".wealthfolio-companion").write_text("0.18.0\n")
    assert verify() == 1  # no SHA256SUMS for that release


def test_setup_refuses_a_downgrade_unless_asked(tmp_path):
    import subprocess

    setup = (ROOT / "deploy" / "setup.sh").read_text()
    guard = setup[setup.index('NEW_VERSION="$('):setup.index("# Keep the old environment")]
    base = tmp_path / "base"
    (base / "app").mkdir(parents=True)
    (base / "venv" / "bin").mkdir(parents=True)
    (base / "venv" / "bin" / "wealthfolio-companion").write_text("#!/bin/sh\necho \"$(cat \"$0.version\")\"\n")
    (base / "venv" / "bin" / "wealthfolio-companion").chmod(0o755)
    installed = base / "venv" / "bin" / "wealthfolio-companion.version"

    def run(new: str, allow: str = "", old: str = "0.19.0") -> int:
        installed.write_text(old)
        (base / "app" / "pyproject.toml").write_text(f'[project]\nversion = "{new}"\n')
        script = f'set -euo pipefail\nBASE="{base}"\nAPP="$BASE/app"\n{guard}\necho ok'
        env = {"PATH": "/usr/bin:/bin", "COMPANION_ALLOW_DOWNGRADE": allow}
        return subprocess.run(["bash", "-c", script], env=env, capture_output=True).returncode

    assert run("0.19.0") == 0 and run("0.20.0") == 0 and run("0.19.10") == 0
    assert run("0.18.0") == 1 and run("0.9.9") == 1
    assert run("0.18.0", allow="1") == 0
    # PEP 440 pre-releases: 0.1.0a1 < 0.1.0a2 < 0.1.0b1 < 0.1.0rc1 < 0.1.0 (sort -V alone puts 0.1.0 first).
    order = ["0.1.0a1", "0.1.0a2", "0.1.0a10", "0.1.0b1", "0.1.0rc1", "0.1.0", "0.1.1a1"]
    for i, older in enumerate(order):
        for newer in order[i + 1:]:
            assert run(newer, old=older) == 0, f"{older} -> {newer}"
            assert run(older, old=newer) == 1, f"{newer} -> {older}"
    assert run("0.1.0a1", old="0.1.0a1") == 0
    assert "COMPANION_ALLOW_DOWNGRADE=1" in (ROOT / "ct" / "wealthfolio-companion.sh").read_text()


def test_the_old_script_name_only_explains_the_move():
    """Containers of Wealthfolio Broker Sync fetch ct/wealthfolio-broker-sync.sh from their `update` command (through
    the renamed repository's redirect): it must install nothing, say how to move (English and German) and fail."""
    import subprocess

    stub = ROOT / "ct" / "wealthfolio-broker-sync.sh"
    text = stub.read_text()
    assert "curl" not in text and "source" not in text and "build.func" not in text
    done = subprocess.run(["bash", str(stub)], capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"})
    assert done.returncode == 1 and not done.stdout
    assert "Wealthfolio Companion" in done.stderr
    assert "#moving-from-wealthfolio-broker-sync" in done.stderr
    assert "Nothing was changed here." in done.stderr and "Hier wurde nichts geändert." in done.stderr
    # The guide it links to exists.
    assert "### Moving from Wealthfolio Broker Sync" in (ROOT / "CHANGELOG.md").read_text()
    assert "## Moving from Wealthfolio Broker Sync" in (ROOT / "docs" / "OPERATIONS.md").read_text()


def test_the_updater_takes_pre_releases():
    import subprocess

    script = (ROOT / "deploy" / "companion-autoupdate").read_text()
    pick = re.search(r"sed -n '([^']+)'", script).group(1)
    cases = (("install 0.1.0", "0.1.0"), ("install 0.1.0a1", "0.1.0a1"), ("install 0.2.0rc12", "0.2.0rc12"),
             ("install 0.1.0x", ""), ("install ../evil", ""))
    for answer, version in cases:
        out = subprocess.run(["sed", "-n", pick], input=f"checked\n{answer}\n", capture_output=True, text=True).stdout
        assert out.strip() == version, answer


def test_a_pre_release_is_named_but_never_marked_as_one():
    """releases/latest - what the update check, the install and `update` read - skips GitHub pre-releases."""
    release = (ROOT / ".github" / "workflows" / "release.yml").read_text()
    assert "--prerelease" not in release and "--latest=false" not in release
    assert 'TITLE="$TITLE (alpha)"' in release


def test_setup_takes_a_moved_key_left_as_secret_key(tmp_path):
    """The move from Wealthfolio Broker Sync (docs/OPERATIONS.md): the old key put root-only into $ETC/secret.key, the
    new installation's key.cred removed - setup.sh seals that key with systemd-creds and hands it to the services."""
    import subprocess

    setup = (ROOT / "deploy" / "setup.sh").read_text()
    block = setup[setup.index("ETC=/etc/wealthfolio-companion"):setup.index("\nfor unit in ")]
    etc = tmp_path / "etc"
    etc.mkdir()
    (etc / "secret.key").write_text("old-key-of-broker-sync-32-characters-long\n")
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "systemd-creds").write_text(  # encrypt writes "<name>:<content>", decrypt checks the name
        '#!/bin/bash\ncase "$1" in\n has-tpm2) exit 1 ;;\n'
        ' encrypt) name="${3#--name=}"; { printf "%s:" "$name"; cat "$4"; } >"$5" ;;\n'
        ' decrypt) name="${2#--name=}"; c="$(cat "$3")"; [[ "$c" == "$name:"* ]] || exit 1\n'
        '   printf "%s\\n" "${c#*:}" ;;\n'
        'esac\n')
    (fake / "systemd-creds").chmod(0o755)
    script = f'set -euo pipefail\n{block.replace("ETC=/etc/wealthfolio-companion", f"ETC={etc}", 1)}\n' \
             'echo "$KEY_LINE $KEY_SOURCE"'
    done = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                          env={"PATH": f"{fake}:/usr/bin:/bin", "BASE": str(tmp_path)})
    assert done.returncode == 0, done.stderr
    assert not (etc / "secret.key").exists()
    assert (etc / "key.cred").read_text() == "companion-key:old-key-of-broker-sync-32-characters-long\n"
    assert done.stdout.split()[-2:] == [f"LoadCredentialEncrypted=companion-key:{etc}/key.cred", "systemd-creds"]
