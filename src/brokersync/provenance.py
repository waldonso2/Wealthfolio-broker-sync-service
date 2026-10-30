"""Checks a release's SHA256SUMS against the build provenance of this repository's release workflow (#165).

Install and update run this file on its own (``python -I provenance.py SHA256SUMS [ARCHIVE]``) in a venv built
from ``requirements-verify.txt``, before anything of the downloaded release is unpacked or runs - so it imports
nothing of the service. No GitHub login: the attestation comes from GitHub's public API, and Sigstore's trusted
root is the one shipped in the pinned ``sigstore`` package (offline - no TUF download). Exit status 0 only when
one attestation signed by ``release.yml`` on ``main`` (GitHub-hosted runner) names every given file - by its
file name and exactly its SHA-256.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO = "waldonso2/wealthfolio-companion"
IDENTITY = f"https://github.com/{REPO}/.github/workflows/release.yml@refs/heads/main"
ISSUER = "https://token.actions.githubusercontent.com"
API = f"https://api.github.com/repos/{REPO}/attestations/sha256:{{digest}}"
STATEMENT = "https://in-toto.io/Statement/v1"
SLSA = "https://slsa.dev/provenance/v1"
SUBJECT = "SHA256SUMS"
MAX_BYTES = 4 * 1024 * 1024


class ProvenanceError(Exception):
    pass


def fetch(digest: str) -> bytes:
    """GitHub's attestations of a digest (public repository: no token)."""
    request = urllib.request.Request(API.format(digest=digest), headers={  # noqa: S310 - fixed https URL
        "Accept": "application/vnd.github+json", "User-Agent": "wealthfolio-companion-verify"})
    try:
        with urllib.request.urlopen(request, timeout=30) as answer:  # noqa: S310 - fixed https URL
            return answer.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise ProvenanceError(f"GitHub has no attestation for {SUBJECT} (sha256:{digest})") from None
        if e.code in (403, 429):
            raise ProvenanceError("GitHub's API refused the request (rate limit: 60 an hour per address) - "
                                  "try again in an hour") from None
        raise ProvenanceError(f"GitHub's API answered {e.code}") from None
    except (urllib.error.URLError, TimeoutError) as e:
        raise ProvenanceError(f"GitHub's API could not be reached: {e}") from None


def _subjects_ok(payload: bytes, digests: dict[str, str]) -> bool:
    statement = json.loads(payload)
    named = {(s.get("name"), s.get("digest", {}).get("sha256")) for s in statement.get("subject", [])}
    return (statement.get("_type") == STATEMENT and statement.get("predicateType") == SLSA
            and all((name, digest) in named for name, digest in digests.items()))


def verify(files: dict[str, bytes], answer: bytes, verifier=None) -> None:
    """Raises ProvenanceError unless one attestation in GitHub's `answer` proves every file (name -> content)."""
    from sigstore.models import Bundle
    from sigstore.verify import Verifier, policy

    if len(answer) > MAX_BYTES:
        raise ProvenanceError("GitHub's answer is too large")
    if not files:
        raise ProvenanceError("nothing to verify")
    digests = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
    try:
        attestations = json.loads(answer)["attestations"]
    except (ValueError, KeyError, TypeError):
        raise ProvenanceError("GitHub's answer holds no attestations") from None
    # COMPANION_VERIFY_ONLINE=1: the trusted root refreshed through Sigstore's TUF repository (signed from the root
    # shipped in the package) - for when Sigstore has rotated its keys since this verifier was released.
    verifier = verifier or Verifier.production(offline=os.environ.get("COMPANION_VERIFY_ONLINE") != "1")
    wanted = policy.AllOf([policy.Identity(identity=IDENTITY, issuer=ISSUER),
                           policy.OIDCRunnerEnvironment("github-hosted")])
    reasons = []
    # GitHub also lists attestations of its own (immutable releases, its own Sigstore instance): those don't
    # verify against the public root and are skipped; one of the release workflow must.
    for item in attestations if isinstance(attestations, list) else []:
        try:
            bundle = Bundle.from_json(json.dumps(item["bundle"]))
            kind, payload = verifier.verify_dsse(bundle, wanted)
            if kind == "application/vnd.in-toto+json" and _subjects_ok(payload, digests):
                return
            reasons.append("statement doesn't name these files")
        except Exception as e:  # any failure only means: not this one
            reasons.append(f"{type(e).__name__}: {e}")
    listed = ", ".join(f"{name} (sha256:{digest})" for name, digest in digests.items())
    raise ProvenanceError(f"no attestation of {IDENTITY} for {listed}: "
                          + ("; ".join(reasons) or "none listed"))


def main(argv: list[str]) -> int:
    if not argv or Path(argv[0]).name != SUBJECT:
        print(f"usage: provenance.py {SUBJECT} [FILE...]", file=sys.stderr)
        return 2
    files = {Path(p).name: Path(p).read_bytes() for p in argv}
    try:
        # Looked up by SHA256SUMS' digest: the release workflow attests it together with the archive.
        verify(files, fetch(hashlib.sha256(files[SUBJECT]).hexdigest()))
    except ProvenanceError as e:
        print(f"Release provenance NOT verified: {e}", file=sys.stderr)
        return 1
    print(f"Release provenance verified: {', '.join(files)} come from {IDENTITY}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
