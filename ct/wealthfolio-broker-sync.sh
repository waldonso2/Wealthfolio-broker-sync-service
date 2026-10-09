#!/usr/bin/env bash
source <(curl -fsSL https://raw.githubusercontent.com/community-scripts/ProxmoxVE/main/misc/build.func)
# Copyright (c) 2026 waldonso2
# License: MIT | https://github.com/waldonso2/wealthfolio-broker-sync-service/raw/main/LICENSE
# Source: https://github.com/waldonso2/wealthfolio-broker-sync-service
#
# Installed through PVE Scripts Local with this repository added as a custom
# repository: PVE Scripts Local replaces the build.func line above with its
# bundled copy, which then runs install/wealthfolio-broker-sync-install.sh from
# this repository. Inside the container, `update` runs this script again, which
# calls update_script below.

APP="Wealthfolio-Broker-Sync"
var_tags="${var_tags:-finance;wealthfolio}"
var_cpu="${var_cpu:-1}"
var_ram="${var_ram:-512}"
var_disk="${var_disk:-2}"
var_os="${var_os:-debian}"
var_version="${var_version:-13}"
var_unprivileged="${var_unprivileged:-1}"

header_info "$APP"
variables
color
catch_errors

function update_script() {
  header_info
  check_container_storage
  check_container_resources

  if [[ ! -d /opt/wealthfolio-broker-sync/app ]]; then
    msg_error "No ${APP} Installation Found!"
    exit
  fi

  if check_for_gh_release "wealthfolio-broker-sync" "waldonso2/wealthfolio-broker-sync-service"; then
    msg_info "Stopping Service"
    systemctl stop wealthfolio-broker-sync-run.timer wealthfolio-broker-sync
    msg_ok "Stopped Service"

    msg_info "Backing up configuration and credentials"
    BACKUP="/opt/wealthfolio-broker-sync/backup-$(date +%Y%m%d-%H%M%S).tar.gz"
    tar -czf "$BACKUP" -C /opt/wealthfolio-broker-sync data
    chmod 600 "$BACKUP"
    find /opt/wealthfolio-broker-sync -maxdepth 1 -name 'backup-*.tar.gz' | sort -r | tail -n +4 | xargs -r rm -f
    msg_ok "Backed up to ${BACKUP}"

    CLEAN_INSTALL=1 fetch_and_deploy_gh_release "wealthfolio-broker-sync" "waldonso2/wealthfolio-broker-sync-service" "tarball" "latest" "/opt/wealthfolio-broker-sync/app"

    msg_info "Updating ${APP}"
    bash /opt/wealthfolio-broker-sync/app/deploy/setup.sh
    msg_ok "Updated ${APP}"

    msg_info "Starting Service"
    systemctl start wealthfolio-broker-sync wealthfolio-broker-sync-run.timer
    msg_ok "Started Service"
    msg_ok "Updated successfully!"
  fi
  exit
}

start
build_container
description

msg_ok "Completed successfully!\n"
echo -e "${CREATING}${GN}${APP} setup has been successfully initialized!${CL}"
echo -e "${INFO}${YW}Open the web UI and follow the assistant:${CL}"
echo -e "${GATEWAY}${BGN}http://${IP}:8090${CL}"
