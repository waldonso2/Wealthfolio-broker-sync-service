#!/usr/bin/env bash
# Install: run in the Proxmox host shell
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/waldonso2/wealthfolio-broker-sync-service/main/ct/wealthfolio-broker-sync.sh)"
# Update: run `update` in the container.
#
# The engine comes from community-scripts/core; COMMUNITY_SCRIPTS_URL points it
# at this repository, so it loads install/wealthfolio-broker-sync-install.sh
# from here and writes an `update` command that runs this script again.
# The engine runs as root on the Proxmox host: it is pinned to a reviewed commit, not
# core's moving main branch, and set every run (the `update` command keeps the URL of
# the run that wrote it). .github/workflows/core-watch.yml reports newer commits.
export COMMUNITY_SCRIPTS_URL="${COMMUNITY_SCRIPTS_URL:-https://raw.githubusercontent.com/waldonso2/wealthfolio-broker-sync-service/main}"
export COMMUNITY_SCRIPTS_CORE_URL="https://raw.githubusercontent.com/community-scripts/core/2f446f0dfc33538a1e842a447100b86d8b6412f2"
source <(curl -fsSL "$COMMUNITY_SCRIPTS_CORE_URL/core/build.func")
# Copyright (c) 2026 waldonso2
# License: MIT | https://github.com/waldonso2/wealthfolio-broker-sync-service/raw/main/LICENSE
# Source: https://github.com/waldonso2/wealthfolio-broker-sync-service

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

# verify_release <dir>: the source archive just deployed must match the SHA256SUMS the release workflow
# published (and attested) with this release - no file changed, none added (#88). Defined in
# install/wealthfolio-broker-sync-install.sh too; keep both the same (test_packaging checks).
verify_release() {
  local dir="$1" version sums ok
  version="$(<"$HOME/.wealthfolio-broker-sync")"
  sums="$(mktemp)"
  if ! curl -fsSL "https://github.com/waldonso2/wealthfolio-broker-sync-service/releases/download/v${version}/SHA256SUMS" -o "$sums"; then
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

function update_script() {
  header_info
  check_container_storage
  check_container_resources

  if [[ ! -d /opt/wealthfolio-broker-sync/app ]]; then
    msg_error "No ${APP} Installation Found!"
    exit
  fi

  # VERSION=0.19.0 update: that version on purpose, also an older one (setup.sh refuses a downgrade
  # otherwise). Without it: the latest release, if it is newer.
  if [[ -n "${VERSION:-}" ]]; then
    export BROKERSYNC_ALLOW_DOWNGRADE=1
  fi
  if [[ -n "${VERSION:-}" ]] || check_for_gh_release "wealthfolio-broker-sync" "waldonso2/wealthfolio-broker-sync-service"; then
    msg_info "Stopping Service"
    systemctl stop wealthfolio-broker-sync-run.timer wealthfolio-broker-sync
    msg_ok "Stopped Service"

    msg_info "Backing up configuration and credentials"
    BACKUP="/opt/wealthfolio-broker-sync/backup-$(date +%Y%m%d-%H%M%S).tar.gz"
    # umask 077: the file is never readable by others, not even until a chmod.
    (umask 077 && tar -czf "$BACKUP" -C /opt/wealthfolio-broker-sync data)
    find /opt/wealthfolio-broker-sync -maxdepth 1 -name 'backup-*.tar.gz' | sort -r | tail -n +4 | xargs -r rm -f
    msg_ok "Backed up to ${BACKUP}"

    CLEAN_INSTALL=1 fetch_and_deploy_gh_release "wealthfolio-broker-sync" "waldonso2/wealthfolio-broker-sync-service" "tarball" "${VERSION:-latest}" "/opt/wealthfolio-broker-sync/app"
    if ! verify_release /opt/wealthfolio-broker-sync/app; then
      # Nothing of the new files ran: the previous version (its venv) is still installed.
      systemctl start wealthfolio-broker-sync wealthfolio-broker-sync-run.timer
      msg_error "Update stopped: the downloaded files don't match the release's SHA256SUMS. The previous version keeps running."
      exit 1
    fi

    msg_info "Updating ${APP}"
    if ! bash /opt/wealthfolio-broker-sync/app/deploy/setup.sh; then
      # The previous version is still installed: bring it back up.
      systemctl start wealthfolio-broker-sync wealthfolio-broker-sync-run.timer
      msg_error "Update failed - the previous version keeps running. Details above."
      exit 1
    fi
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
# The one-time code for the first password (only this console shows it): without it nobody else in the
# network can choose the password before you do.
SETUP_CODE="$(pct exec "$CTID" -- /usr/local/bin/brokersync-cli setup-code 2>/dev/null | sed -n 's/^Setup code.*: //p')"
echo -e "${INFO}${YW}Open the web UI and choose its password (the certificate comes from the container's own CA: confirm the browser warning once, or import /ca.crt):${CL}"
echo -e "${GATEWAY}${BGN}https://${IP}:8443/setup-password?code=${SETUP_CODE}${CL}"
echo -e "${INFO}${YW}Setup code: ${SETUP_CODE:-run brokersync-cli setup-code in the container} (valid 24 h)${CL}"
