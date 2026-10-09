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

rm -rf "$BASE/venv"
python3 -m venv "$BASE/venv"
"$BASE/venv/bin/pip" install --quiet --disable-pip-version-check "$APP"

install -m 644 "$APP"/deploy/systemd/*.service "$APP"/deploy/systemd/*.timer /etc/systemd/system/
install -m 755 "$APP/deploy/brokersync-reset-password" /usr/local/bin/brokersync-reset-password
systemctl daemon-reload
