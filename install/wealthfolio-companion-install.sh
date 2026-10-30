#!/usr/bin/env bash

# Copyright (c) 2026 waldonso2
# License: MIT | https://github.com/waldonso2/wealthfolio-companion/raw/main/LICENSE
# Source: https://github.com/waldonso2/wealthfolio-companion

source /dev/stdin <<<"$FUNCTIONS_FILE_PATH"
color
verb_ip6
catch_errors
setting_up_container
network_check
update_os

msg_info "Installing Dependencies"
$STD apt install -y python3 python3-venv
msg_ok "Installed Dependencies"

# verify_release <dir>: the source archive just deployed must match the SHA256SUMS the release workflow
# published with this release - no file changed, none added (#88) - and SHA256SUMS itself must carry that
# workflow's build provenance (#165): src/brokersync/provenance.py checks GitHub's attestation (public API,
# no login) offline against Sigstore's root, in a throwaway venv of requirements-verify.txt (hashes, wheels
# only). Verifier and lock come from main, like this script - never from the release being checked.
# Defined in install/wealthfolio-companion-install.sh and ct/wealthfolio-companion.sh; keep both the same
# (test_packaging checks).
verify_release() {
  local dir="$1" version tmp sums ok
  local main="${COMMUNITY_SCRIPTS_URL:-https://raw.githubusercontent.com/waldonso2/wealthfolio-companion/main}"
  version="$(<"$HOME/.wealthfolio-companion")"
  tmp="$(mktemp -d)"
  sums="$tmp/SHA256SUMS"
  if ! curl -fsSL "https://github.com/waldonso2/wealthfolio-companion/releases/download/v${version}/SHA256SUMS" -o "$sums" ||
    ! curl -fsSL "$main/src/brokersync/provenance.py" -o "$tmp/provenance.py" ||
    ! curl -fsSL "$main/requirements-verify.txt" -o "$tmp/requirements-verify.txt" ||
    ! python3 -m venv "$tmp/venv" ||
    ! "$tmp/venv/bin/pip" install --quiet --disable-pip-version-check --require-hashes --only-binary :all: \
      -r "$tmp/requirements-verify.txt" ||
    ! XDG_CACHE_HOME="$tmp" XDG_DATA_HOME="$tmp" "$tmp/venv/bin/python" -I "$tmp/provenance.py" "$sums"; then
    rm -rf "$tmp"
    return 1
  fi
  ok=0
  (cd "$dir" && sha256sum --quiet --strict -c "$sums") || ok=1
  diff <(cd "$dir" && find . -type f ! -path './build/*' ! -path '*.egg-info/*' | LC_ALL=C sort) \
    <(cut -c67- "$sums" | LC_ALL=C sort) >/dev/null || ok=1
  rm -rf "$tmp"
  return "$ok"
}

fetch_and_deploy_gh_release "wealthfolio-companion" "waldonso2/wealthfolio-companion" "tarball" "latest" "/opt/wealthfolio-companion/app"
if ! verify_release /opt/wealthfolio-companion/app; then
  msg_error "The release's provenance or its checksums (SHA256SUMS) could not be verified - details above. Installation stopped."
  exit 1
fi

msg_info "Installing Wealthfolio Companion"
$STD bash /opt/wealthfolio-companion/app/deploy/setup.sh
msg_ok "Installed Wealthfolio Companion"

msg_info "Starting Service"
systemctl enable -q --now wealthfolio-companion wealthfolio-companion-run.timer
msg_ok "Started Service"

motd_ssh
customize

cleanup_lxc
