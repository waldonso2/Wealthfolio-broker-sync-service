#!/usr/bin/env bash

# Copyright (c) 2026 waldonso2
# License: MIT | https://github.com/waldonso2/wealthfolio-broker-sync-service/raw/main/LICENSE
# Source: https://github.com/waldonso2/wealthfolio-broker-sync-service

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

fetch_and_deploy_gh_release "wealthfolio-broker-sync" "waldonso2/wealthfolio-broker-sync-service" "tarball" "latest" "/opt/wealthfolio-broker-sync/app"

msg_info "Installing Wealthfolio Broker Sync"
$STD bash /opt/wealthfolio-broker-sync/app/deploy/setup.sh
msg_ok "Installed Wealthfolio Broker Sync"

msg_info "Starting Service"
systemctl enable -q --now wealthfolio-broker-sync wealthfolio-broker-sync-run.timer
msg_ok "Started Service"

motd_ssh
customize

cleanup_lxc
