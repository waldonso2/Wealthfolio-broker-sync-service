"""#165: the release verifier of install and update, offline against GitHub's recorded answer for v0.1.0a4."""

import io
import urllib.error
from pathlib import Path

import pytest

from brokersync import provenance

FIXTURES = Path(__file__).parent / "fixtures" / "provenance"
SUMS = (FIXTURES / "SHA256SUMS-0.1.0a4").read_bytes()
ANSWER = (FIXTURES / "attestations-0.1.0a4.json").read_bytes()


@pytest.fixture(autouse=True)
def _own_cache(tmp_path, monkeypatch):
    # Sigstore's trusted root comes from the package (offline); nothing of the user's cache.
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))


def test_the_release_workflows_attestation_proves_the_checksums():
    provenance.verify({"SHA256SUMS": SUMS}, ANSWER)  # GitHub's own (immutable release) bundle is skipped, ours verifies


def test_the_archive_is_proven_by_the_same_attestation():
    archive = "wealthfolio-companion-0.1.0a4.tar.gz"
    # The recorded answer names the archive's digest, not its bytes: a stand-in with that name must fail,
    # also under another name.
    with pytest.raises(provenance.ProvenanceError, match="doesn't name these files"):
        provenance.verify({"SHA256SUMS": SUMS, archive: b"not the archive"}, ANSWER)
    with pytest.raises(provenance.ProvenanceError):
        provenance.verify({}, ANSWER)


def test_a_file_under_another_name_fails():
    with pytest.raises(provenance.ProvenanceError, match="doesn't name these files"):
        provenance.verify({"SHA256SUMS.txt": SUMS}, ANSWER)


def test_changed_checksums_fail():
    with pytest.raises(provenance.ProvenanceError, match="statement doesn't name these files"):
        provenance.verify({"SHA256SUMS": SUMS.replace(b"setup.sh", b"setup.sh ")}, ANSWER)


@pytest.mark.parametrize("identity", [
    "https://github.com/waldonso2/wealthfolio-companion/.github/workflows/ci.yml@refs/heads/main",
    "https://github.com/waldonso2/wealthfolio-companion/.github/workflows/release.yml@refs/heads/dev",
    "https://github.com/someone/wealthfolio-companion/.github/workflows/release.yml@refs/heads/main",
])
def test_another_signer_fails(monkeypatch, identity):
    monkeypatch.setattr(provenance, "IDENTITY", identity)
    with pytest.raises(provenance.ProvenanceError, match="SANs do not match"):
        provenance.verify({"SHA256SUMS": SUMS}, ANSWER)


def test_another_issuer_fails(monkeypatch):
    monkeypatch.setattr(provenance, "ISSUER", "https://example.invalid")
    with pytest.raises(provenance.ProvenanceError):
        provenance.verify({"SHA256SUMS": SUMS}, ANSWER)


@pytest.mark.parametrize("answer", [b'{"attestations": []}', b"{}", b"not json", b'{"attestations": [{}]}'])
def test_no_usable_attestation_fails(answer):
    with pytest.raises(provenance.ProvenanceError):
        provenance.verify({"SHA256SUMS": SUMS}, answer)


def test_a_huge_answer_fails():
    with pytest.raises(provenance.ProvenanceError, match="too large"):
        provenance.verify({"SHA256SUMS": SUMS}, b" " * (provenance.MAX_BYTES + 1))


@pytest.mark.parametrize(("code", "text"), [(404, "no attestation"), (403, "rate limit"), (429, "rate limit"),
                                            (500, "answered 500")])
def test_fetch_errors_say_why(monkeypatch, code, text):
    def refuse(request, timeout):
        raise urllib.error.HTTPError(request.full_url, code, "no", {}, io.BytesIO())

    monkeypatch.setattr(provenance.urllib.request, "urlopen", refuse)
    with pytest.raises(provenance.ProvenanceError, match=text):
        provenance.fetch("ab" * 32)


def test_main_fetches_by_the_files_digest(monkeypatch, tmp_path, capsys):
    asked = []

    def fetch(digest):
        asked.append(digest)
        return ANSWER

    monkeypatch.setattr(provenance, "fetch", fetch)
    (tmp_path / "SHA256SUMS").write_bytes(SUMS)
    assert provenance.main([str(tmp_path / "SHA256SUMS")]) == 0
    assert asked == ["22aae0ad937a79d6dadb68de3591345e2824c1cc3d30e256724f7da44934eec3"]
    (tmp_path / "SHA256SUMS").write_bytes(SUMS + b"x")
    assert provenance.main([str(tmp_path / "SHA256SUMS")]) == 1
    assert "NOT verified" in capsys.readouterr().err
    assert provenance.main([]) == 2
    (tmp_path / "other").write_bytes(SUMS)
    assert provenance.main([str(tmp_path / "other")]) == 2


@pytest.mark.parametrize(("online", "offline"), [("", True), ("1", False)])
def test_offline_unless_asked(monkeypatch, online, offline):
    from sigstore.verify import Verifier

    asked = []

    def production(*, offline):
        asked.append(offline)
        raise RuntimeError("stop")

    monkeypatch.setattr(Verifier, "production", staticmethod(production))
    monkeypatch.setenv("COMPANION_VERIFY_ONLINE", online)
    with pytest.raises(RuntimeError):
        provenance.verify({"SHA256SUMS": SUMS}, ANSWER)
    assert asked == [offline]
