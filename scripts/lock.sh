#!/usr/bin/env bash
# Regenerates requirements.txt: every runtime dependency of pyproject.toml (and the build
# backend) at an exact version with its hashes, for every supported Python and platform.
# deploy/setup.sh installs only from it (pip --require-hashes). Run after changing a
# dependency in pyproject.toml; `--upgrade` moves everything to the newest versions.
set -euo pipefail
cd "$(dirname "$0")/.."
command -v uv >/dev/null || { echo "uv is needed: pip install uv" >&2; exit 1; }
uv pip compile pyproject.toml requirements-build.in --universal --python-version 3.11 \
  --generate-hashes --no-header --annotation-style line --quiet "$@" -o requirements.txt
# The CI's test and lint tools (requirements-dev.in), constrained to the runtime versions above.
uv pip compile requirements-dev.in -c requirements.txt --universal --python-version 3.11 \
  --generate-hashes --no-header --annotation-style line --quiet "$@" -o requirements-dev.txt
