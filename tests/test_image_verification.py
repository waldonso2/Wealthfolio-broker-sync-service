"""scripts/pull-verified-image.sh (#150): the image is used only by a digest whose signature and provenance cosign
verified with this repository's release identity - with fake `curl` and `cosign` on PATH, fabricated digests."""
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "pull-verified-image.sh"
DIGEST = "sha256:" + "ab12" * 16  # fabricated
REF = f"ghcr.io/waldonso2/wealthfolio-companion@{DIGEST}"
IDENTITY = "https://github.com/waldonso2/wealthfolio-companion/.github/workflows/release.yml@refs/heads/main"
ISSUER = "https://token.actions.githubusercontent.com"

CURL = """#!/bin/bash
echo "curl $*" >>"$LOG"
[ "${NET:-}" = down ] && { echo "curl: (6) Could not resolve host" >&2; exit 6; }
case "$*" in
  *ghcr.io/token*) echo '{"token":"fake-token"}' ;;
  *manifests/v0.1.0a4) printf 'HTTP/2 200\\r\\ncontent-type: application/vnd.oci.image.index.v1+json\\r\\n'
                          printf 'docker-content-digest: %s\\r\\n\\r\\n' "$DIGEST" ;;
  *) echo "curl: (22) The requested URL returned error: 404" >&2; exit 22 ;;
esac
"""

# Accepts only the exact policy, like cosign: identity, issuer, repository, ref and trigger of release.yml on main.
COSIGN = f"""#!/bin/bash
echo "cosign $*" >>"$LOG"
[ "$1" = version ] && {{ echo "GitVersion:    ${{COSIGN_VERSION:-v3.0.6}}"; exit 0; }}
args=" $* "
for want in "--certificate-identity {IDENTITY} " "--certificate-oidc-issuer {ISSUER} " \\
            "--certificate-github-workflow-repository waldonso2/wealthfolio-companion " \\
            "--certificate-github-workflow-ref refs/heads/main " "--certificate-github-workflow-trigger push "; do
  [[ "$args" == *" $want"* ]] || {{ echo "Error: no matching CertificateIdentity" >&2; exit 1; }}
done
[[ "$args" == *" ghcr.io/waldonso2/wealthfolio-companion@$DIGEST "* ]] || {{ echo "Error: wrong ref" >&2; exit 1; }}
case "$1" in
  verify) [ "${{MISSING:-}}" = signature ] && {{ echo "Error: no signatures found" >&2; exit 1; }} ;;
  verify-attestation) [[ "$args" == *" --type slsaprovenance1 "* ]] || exit 1
    [ "${{MISSING:-}}" = attestation ] && {{ echo "Error: no matching attestations" >&2; exit 1; }} ;;
  *) exit 1 ;;
esac
echo '[{{"critical":{{}}}}]'
"""


@pytest.fixture
def run(tmp_path):
    fake = tmp_path / "bin"
    fake.mkdir()
    for name, body in {"curl": CURL, "cosign": COSIGN}.items():
        (fake / name).write_text(body)
        (fake / name).chmod(0o755)
    log = tmp_path / "log"

    def go(*args: str, cosign: bool = True, **env: str) -> subprocess.CompletedProcess:
        if not cosign:
            (fake / "cosign").unlink(missing_ok=True)
        environ = {"PATH": f"{fake}:/usr/bin:/bin", "LOG": str(log), "DIGEST": DIGEST, **env}
        return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, env=environ)

    go.log = log  # type: ignore[attr-defined]
    return go


def test_success_prints_only_the_verified_digest_reference(run):
    done = run("0.1.0a4")
    assert done.returncode == 0, done.stderr
    assert done.stdout == f"{REF}\n"
    calls = run.log.read_text()
    assert calls.count("manifests/") == 1  # the tag is resolved once; cosign gets only the digest
    assert ":v0.1.0a4" not in calls and ":latest" not in calls
    assert "cosign verify --certificate-identity" in calls
    assert "cosign verify-attestation --type slsaprovenance1" in calls


def test_a_leading_v_is_the_same_version(run):
    assert run("v0.1.0a4").stdout == f"{REF}\n"


def test_env_file_gets_the_reference_and_keeps_the_rest(run, tmp_path):
    env = tmp_path / ".env"
    env.write_text("WF_SECRET_KEY=fabricated\nCOMPANION_IMAGE=ghcr.io/old@sha256:" + "0" * 64 + "\n")
    done = run("--env", str(env), "0.1.0a4")
    assert done.returncode == 0, done.stderr
    assert env.read_text() == f"WF_SECRET_KEY=fabricated\nCOMPANION_IMAGE={REF}\n"
    assert env.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(("args", "env", "message"), [
    (("0.1.0a4",), {"MISSING": "signature"}, "signature check failed"),
    (("0.1.0a4",), {"MISSING": "attestation"}, "provenance check failed"),
    (("0.1.0a4",), {"NET": "down"}, "can't be reached"),
    (("0.2.0",), {}, "no image v0.2.0"),
    (("0.1.0a4",), {"COSIGN_VERSION": "v2.4.1"}, "cosign 3 or newer"),
    (("latest",), {}, "refused"),
    (("v",), {}, "not a release version"),
    (("0.1",), {}, "not a release version"),
    (("0.1.0-a4",), {}, "not a release version"),
    (("0.1.0a4;id",), {}, "not a release version"),
    (("$(id)",), {}, "not a release version"),
    (("0.1.0a4", "0.1.0"), {}, "usage"),
    ((), {}, "usage"),
])
def test_failures_are_closed(run, tmp_path, args, env, message):
    dotenv = tmp_path / ".env"
    dotenv.write_text("WF_SECRET_KEY=fabricated\n")
    done = run("--env", str(dotenv), *args, **env)
    assert done.returncode != 0
    assert done.stdout == ""
    assert message in done.stderr
    assert dotenv.read_text() == "WF_SECRET_KEY=fabricated\n"


def test_wrong_identity_is_refused(run, tmp_path):
    """A signature by another workflow, branch or repository: the fake cosign (like the real one) finds no match."""
    script = tmp_path / "other.sh"
    script.write_text(SCRIPT.read_text().replace("@refs/heads/main\"", "@refs/heads/evil\"", 1))
    assert "refs/heads/evil" in script.read_text()
    done = subprocess.run(["bash", str(script), "0.1.0a4"], capture_output=True, text=True,
                          env={"PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin", "LOG": str(run.log), "DIGEST": DIGEST})
    assert done.returncode != 0 and done.stdout == ""
    assert "signature check failed" in done.stderr


def test_missing_cosign_is_refused(run):
    done = run("0.1.0a4", cosign=False)
    assert done.returncode != 0 and done.stdout == ""
    assert "cosign is missing" in done.stderr


def test_the_compose_example_and_readme_use_no_mutable_companion_tag():
    compose = (ROOT / "deploy" / "docker-compose.example.yml").read_text()
    readme = (ROOT / "README.md").read_text()
    image = re.search(r"^\s+image:\s*(\S+)", compose.split("wealthfolio:\n")[0], re.MULTILINE)
    assert image and image.group(1).startswith("${COMPANION_IMAGE:?")
    for text in (compose, readme):
        assert not re.search(r"wealthfolio-companion:(latest|v?\d)", text)
        assert not re.search(r"wealthfolio-companion@sha256:[0-9a-f]{64}", text)  # no digest as a default either
    assert "pull-verified-image.sh" in readme
