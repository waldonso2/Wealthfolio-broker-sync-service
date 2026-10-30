#!/usr/bin/env bash
# Resolve a release's container image to its digest and verify it before anything runs it (#150):
#   scripts/pull-verified-image.sh [--env FILE] <version>
# 1. the tag v<version> on ghcr.io is resolved to its digest once - nothing after this reads a tag again;
# 2. cosign checks the signature of image@sha256:... (keyless, made by release.yml on main of this repository);
# 3. cosign checks the SLSA provenance attestation (actions/attest-build-provenance) with the same identity.
# Only then the verified reference is printed on stdout (and with --env written as COMPANION_IMAGE=... into FILE,
# e.g. the Compose folder's .env). Every failure exits non-zero with stdout empty and FILE unchanged; the script
# never pulls, stops or replaces a container - `docker pull` / `docker compose up -d` of the printed reference does.
# Needs cosign 3 (README: "Verified image") and curl. Repository, identity and issuer are fixed on purpose.
set -euo pipefail

REPO=waldonso2/wealthfolio-companion
IMAGE="ghcr.io/$REPO"
IDENTITY="https://github.com/$REPO/.github/workflows/release.yml@refs/heads/main"
ISSUER=https://token.actions.githubusercontent.com

die() { echo "pull-verified-image: $*" >&2; exit 1; }

ENV_FILE=""
if [ "${1:-}" = "--env" ]; then
  [ $# -ge 2 ] || die "--env needs a file"
  ENV_FILE="$2"
  shift 2
fi
[ $# -eq 1 ] || die "usage: $0 [--env FILE] <version>   (e.g. 0.1.0 or 0.1.0a4; never latest)"
VERSION="${1#v}"
[ "$VERSION" != latest ] || die "'latest' is a mutable tag and is refused: name the release's version"
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+((a|b|rc)[0-9]+)?$ ]] || die "not a release version: '$1' (X.Y.Z, X.Y.ZaN, X.Y.ZbN or X.Y.ZrcN)"

command -v curl >/dev/null || die "curl is missing"
command -v cosign >/dev/null || die "cosign is missing - install it first (README, section 'Verified image')"
cosign_version="$(cosign version 2>/dev/null | sed -n 's/^GitVersion:[[:space:]]*v\{0,1\}//p')" || true
[[ "$cosign_version" =~ ^([3-9]|[1-9][0-9])\. ]] || die "cosign 3 or newer is needed (found: '${cosign_version:-unknown}')"

# Anonymous pull token, then the tag's digest from the registry (the index of both platforms).
token="$(curl -fsS --max-time 30 "https://ghcr.io/token?scope=repository:$REPO:pull" \
  | sed -n 's/.*"token":"\([^"]*\)".*/\1/p')" || die "the registry ghcr.io can't be reached (token)"
[ -n "$token" ] || die "the registry ghcr.io gave no token"
headers="$(curl -fsSI --max-time 30 -H "Authorization: Bearer $token" \
  -H "Accept: application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json" \
  "https://ghcr.io/v2/$REPO/manifests/v$VERSION")" || die "no image v$VERSION on ghcr.io (or the registry can't be reached)"
digest="$(tr -d '\r' <<<"$headers" | sed -n 's/^[Dd]ocker-[Cc]ontent-[Dd]igest:[[:space:]]*//p' | tail -n 1)"
[[ "$digest" =~ ^sha256:[0-9a-f]{64}$ ]] || die "the registry named no valid digest for v$VERSION"
REF="$IMAGE@$digest"

policy=(--certificate-identity "$IDENTITY" --certificate-oidc-issuer "$ISSUER"
  --certificate-github-workflow-repository "$REPO" --certificate-github-workflow-ref refs/heads/main
  --certificate-github-workflow-trigger push)
echo "Verifying $REF (v$VERSION) ..." >&2
cosign verify "${policy[@]}" "$REF" >/dev/null \
  || die "signature check failed for $REF: no valid signature from $IDENTITY - nothing changed"
cosign verify-attestation --type slsaprovenance1 "${policy[@]}" "$REF" >/dev/null \
  || die "provenance check failed for $REF: no SLSA provenance from $IDENTITY - nothing changed"

if [ -n "$ENV_FILE" ]; then
  tmp="$(mktemp "$(dirname -- "$ENV_FILE")/.companion-env.XXXXXX")" || die "can't write next to $ENV_FILE"
  { if [ -f "$ENV_FILE" ]; then grep -v '^COMPANION_IMAGE=' "$ENV_FILE" || true; fi
    echo "COMPANION_IMAGE=$REF"; } >"$tmp"
  chmod 600 "$tmp"  # .env holds Wealthfolio's secret key too
  mv -f -- "$tmp" "$ENV_FILE"
  echo "COMPANION_IMAGE written to $ENV_FILE" >&2
fi
echo "Verified: signature and provenance from $IDENTITY" >&2
echo "$REF"
