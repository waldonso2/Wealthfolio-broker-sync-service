#!/usr/bin/env bash
# Install: run in the Proxmox host shell
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/waldonso2/wealthfolio-companion/main/ct/wealthfolio-companion.sh)"
# Update: run `update` in the container.
#
# The engine comes from community-scripts/core; COMMUNITY_SCRIPTS_URL points it
# at this repository, so it loads install/wealthfolio-companion-install.sh
# from here and writes an `update` command that runs this script again.
# The engine runs as root on the Proxmox host: it is pinned to a reviewed commit, not
# core's moving main branch, and set every run (the `update` command keeps the URL of
# the run that wrote it). .github/workflows/core-watch.yml reports newer commits.
export COMMUNITY_SCRIPTS_URL="${COMMUNITY_SCRIPTS_URL:-https://raw.githubusercontent.com/waldonso2/wealthfolio-companion/main}"
export COMMUNITY_SCRIPTS_CORE_URL="https://raw.githubusercontent.com/community-scripts/core/8354b069a5fce0ab074c15825fb24704387dde04"
source <(curl -fsSL "$COMMUNITY_SCRIPTS_CORE_URL/core/build.func")
# Copyright (c) 2026 waldonso2
# License: MIT | https://github.com/waldonso2/wealthfolio-companion/raw/main/LICENSE
# Source: https://github.com/waldonso2/wealthfolio-companion

APP="Wealthfolio-Companion"
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
# install/wealthfolio-companion-install.sh too; keep both the same (test_packaging checks).
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

function update_script() {
  header_info
  check_container_storage
  check_container_resources

  if [[ ! -d /opt/wealthfolio-companion/app ]]; then
    msg_error "No ${APP} Installation Found!"
    exit
  fi

  # VERSION=0.19.0 update: that version on purpose, also an older one (setup.sh refuses a downgrade
  # otherwise). Without it: the latest release, if it is newer.
  if [[ -n "${VERSION:-}" ]]; then
    export COMPANION_ALLOW_DOWNGRADE=1
  fi
  if [[ -n "${VERSION:-}" ]] || check_for_gh_release "wealthfolio-companion" "waldonso2/wealthfolio-companion"; then
    msg_info "Stopping Service"
    systemctl stop wealthfolio-companion-run.timer wealthfolio-companion
    msg_ok "Stopped Service"

    msg_info "Backing up configuration and credentials"
    BACKUP="/opt/wealthfolio-companion/backup-$(date +%Y%m%d-%H%M%S).tar.gz"
    # umask 077: the file is never readable by others, not even until a chmod.
    (umask 077 && tar -czf "$BACKUP" -C /opt/wealthfolio-companion data)
    find /opt/wealthfolio-companion -maxdepth 1 -name 'backup-*.tar.gz' | sort -r | tail -n +4 | xargs -r rm -f
    msg_ok "Backed up to ${BACKUP}"

    CLEAN_INSTALL=1 fetch_and_deploy_gh_release "wealthfolio-companion" "waldonso2/wealthfolio-companion" "tarball" "${VERSION:-latest}" "/opt/wealthfolio-companion/app"
    if ! verify_release /opt/wealthfolio-companion/app; then
      # Nothing of the new files ran: the previous version (its venv) is still installed.
      systemctl start wealthfolio-companion wealthfolio-companion-run.timer
      msg_error "Update stopped: the downloaded files don't match the release's SHA256SUMS. The previous version keeps running."
      exit 1
    fi

    msg_info "Updating ${APP}"
    if ! bash /opt/wealthfolio-companion/app/deploy/setup.sh; then
      # The previous version is still installed: bring it back up.
      systemctl start wealthfolio-companion wealthfolio-companion-run.timer
      msg_error "Update failed - the previous version keeps running. Details above."
      exit 1
    fi
    msg_ok "Updated ${APP}"

    msg_info "Starting Service"
    systemctl start wealthfolio-companion wealthfolio-companion-run.timer
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
# stdin from /dev/null: the engine may run this with stdin closed, and systemd-run --pipe then fails with 208
# (EXIT_STDIN); a missing code must never fail the finished install - the line below says how to get it.
SETUP_CODE="$( (pct exec "$CTID" -- /usr/local/bin/companion-cli setup-code </dev/null 2>/dev/null || true) | sed -n 's/^Setup code.*: //p')"
echo -e "${INFO}${YW}Open the web UI and choose its password (the certificate comes from the container's own CA: confirm the browser warning once, or import /ca.crt):${CL}"
HOST="$IP"
[[ "$HOST" == *:* ]] && HOST="[$HOST]"  # an IPv6 address goes in brackets in a URL
echo -e "${GATEWAY}${BGN}https://${HOST}:8443/setup-password?code=${SETUP_CODE}${CL}"
echo -e "${INFO}${YW}Setup code: ${SETUP_CODE:-run companion-cli setup-code in the container} (valid 24 h)${CL}"
