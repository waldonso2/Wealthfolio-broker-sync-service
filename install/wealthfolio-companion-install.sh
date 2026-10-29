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
# published (and attested) with this release - no file changed, none added (#88). Defined in
# ct/wealthfolio-companion.sh too; keep both the same (test_packaging checks).
verify_release() {
  local dir="$1" version sums ok
  version="$(<"$HOME/.wealthfolio-companion")"
  sums="$(mktemp)"
  if ! curl -fsSL "https://github.com/waldonso2/wealthfolio-companion/releases/download/v${version}/SHA256SUMS" -o "$sums"; then
    rm -f "$sums"
    return 1
  fi
  ok=0
  (cd "$dir" && sha256sum --quiet --strict -c "$sums") || ok=1
  diff <(cd "$dir" && find . -type f ! -path './build/*' ! -path '*.egg-info/*' | LC_ALL=C sort) \
    <(cut -c67- "$sums" | LC_ALL=C sort) >/dev/null || ok=1
  rm -f "$sums"
  return "$ok"
}

fetch_and_deploy_gh_release "wealthfolio-companion" "waldonso2/wealthfolio-companion" "tarball" "latest" "/opt/wealthfolio-companion/app"
if ! verify_release /opt/wealthfolio-companion/app; then
  msg_error "The downloaded files don't match the release's SHA256SUMS - installation stopped."
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
