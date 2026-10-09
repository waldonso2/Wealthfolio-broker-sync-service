#!/usr/bin/env bash
# Installs or updates the service from the unpacked source in
# /opt/wealthfolio-broker-sync/app. Used by the install and the update script;
# idempotent. Never touches /opt/wealthfolio-broker-sync/data (configuration,
# encrypted credentials, sync state).
set -euo pipefail

BASE=/opt/wealthfolio-broker-sync
APP="$BASE/app"
USER_NAME=brokersync

id -u "$USER_NAME" >/dev/null 2>&1 || useradd --system --home-dir "$BASE" --shell /usr/sbin/nologin "$USER_NAME"
install -d -m 700 -o "$USER_NAME" -g "$USER_NAME" "$BASE/data"
chown -R "$USER_NAME:$USER_NAME" "$BASE/data"

# Keep the old environment until the new one is complete: a failed install
# (network, disk, a broken package) puts the previous version back instead of
# leaving a service that can't start. The service is stopped meanwhile.
rm -rf "$BASE/venv.old"
[[ -d "$BASE/venv" ]] && mv "$BASE/venv" "$BASE/venv.old"
if ! { python3 -m venv "$BASE/venv" &&
  "$BASE/venv/bin/pip" install --quiet --disable-pip-version-check --no-cache-dir "$APP" &&
  "$BASE/venv/bin/brokersync" --version >/dev/null; }; then
  rm -rf "$BASE/venv"
  [[ -d "$BASE/venv.old" ]] && mv "$BASE/venv.old" "$BASE/venv"
  echo "Installing the Python packages failed - the previous version is back in place." >&2
  echo "Free disk space: $(df -h "$BASE" | awk 'NR==2 {print $4}')" >&2
  exit 1
fi
rm -rf "$BASE/venv.old"

install -m 644 "$APP"/deploy/systemd/*.service "$APP"/deploy/systemd/*.timer /etc/systemd/system/
install -m 755 "$APP/deploy/brokersync-reset-password" /usr/local/bin/brokersync-reset-password
systemctl daemon-reload
