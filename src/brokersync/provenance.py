"""Checks a release's SHA256SUMS against the build provenance of this repository's release workflow (#165).

Install and update run this file on its own (``python -I provenance.py SHA256SUMS``) in a throwaway venv built
from ``requirements-verify.txt``, before anything of the downloaded release runs - so it imports nothing of
the service. No GitHub login: the attestation comes from GitHub's public API, and Sigstore's trusted root is
the one shipped in the pinned ``sigstore`` package (offline - no TUF download). Exit status 0 only when an
attestation signed by ``release.yml`` on ``main`` (GitHub-hosted runner) names exactly this file's SHA-256.
"""

from __future__ import annotations

import hashlib
import json
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


def _subject_ok(payload: bytes, digest: str) -> bool:
    statement = json.loads(payload)
    return (statement.get("_type") == STATEMENT and statement.get("predicateType") == SLSA
            and any(s.get("name") == SUBJECT and s.get("digest", {}).get("sha256") == digest
                    for s in statement.get("subject", [])))


def verify(sums: bytes, answer: bytes, verifier=None) -> None:
    """Raises ProvenanceError unless one attestation in GitHub's `answer` proves `sums`."""
    from sigstore.models import Bundle
    from sigstore.verify import Verifier, policy

    if len(answer) > MAX_BYTES:
        raise ProvenanceError("GitHub's answer is too large")
    digest = hashlib.sha256(sums).hexdigest()
    try:
        attestations = json.loads(answer)["attestations"]
    except (ValueError, KeyError, TypeError):
        raise ProvenanceError("GitHub's answer holds no attestations") from None
    verifier = verifier or Verifier.production(offline=True)
    wanted = policy.AllOf([policy.Identity(identity=IDENTITY, issuer=ISSUER),
                           policy.OIDCRunnerEnvironment("github-hosted")])
    reasons = []
    # GitHub also lists attestations of its own (immutable releases, its own Sigstore instance): those don't
    # verify against the public root and are skipped; one of the release workflow must.
    for item in attestations if isinstance(attestations, list) else []:
        try:
            bundle = Bundle.from_json(json.dumps(item["bundle"]))
            kind, payload = verifier.verify_dsse(bundle, wanted)
            if kind == "application/vnd.in-toto+json" and _subject_ok(payload, digest):
                return
            reasons.append("statement doesn't name this file")
        except Exception as e:  # any failure only means: not this one
            reasons.append(f"{type(e).__name__}: {e}")
    raise ProvenanceError(f"no attestation of {IDENTITY} for {SUBJECT} (sha256:{digest}): "
                          + ("; ".join(reasons) or "none listed"))


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: provenance.py SHA256SUMS", file=sys.stderr)
        return 2
    sums = Path(argv[0]).read_bytes()
    try:
        verify(sums, fetch(hashlib.sha256(sums).hexdigest()))
    except ProvenanceError as e:
        print(f"Release provenance NOT verified: {e}", file=sys.stderr)
        return 1
    print(f"Release provenance verified: {SUBJECT} comes from {IDENTITY}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
