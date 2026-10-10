#!/usr/bin/env bash
# Installs or updates the service from the unpacked source in
# /opt/wealthfolio-broker-sync/app. Used by the install and the update script;
# idempotent. Keeps the service's key outside data/ (/etc/wealthfolio-broker-sync,
# encrypted with systemd-creds where possible), creates the TLS certificate and
# brings the data of older versions up to date (all of it encrypted).
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

# Scalable's official CLI (sc), used by the Scalable Capital adapter. Signature-
# checked; a failure only affects Scalable, so it doesn't stop the install.
"$BASE/venv/bin/brokersync" install-sc --dir "$BASE/bin" ||
  echo "Warning: the Scalable CLI could not be installed - Scalable Capital won't work until the next update." >&2

ETC=/etc/wealthfolio-broker-sync
install -d -m 700 "$ETC" "$ETC/tls"

# The service's key: never in data/. Encrypted with systemd-creds (bound to this
# container's host key), or root-only where systemd-creds can't encrypt; systemd
# hands it to the services at start. An installation before 0.9.0 keeps its key.
if [[ ! -f "$ETC/key.cred" && ! -f "$ETC/secret.key" ]]; then
  KEY_TMP="$(mktemp -p "$ETC")"
  if [[ -f "$BASE/data/secret.key" ]]; then
    cat "$BASE/data/secret.key" >"$KEY_TMP"
  else
    "$BASE/venv/bin/python" -c 'import base64, secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())' >"$KEY_TMP"
  fi
  if systemd-creds encrypt --name=brokersync-key "$KEY_TMP" "$ETC/key.cred" 2>/dev/null &&
    systemd-creds decrypt --name=brokersync-key "$ETC/key.cred" - 2>/dev/null | cmp -s - "$KEY_TMP"; then
    chmod 600 "$ETC/key.cred"
  else
    rm -f "$ETC/key.cred"
    install -m 600 "$KEY_TMP" "$ETC/secret.key"
    echo "Note: systemd-creds can't encrypt here - the key is kept root-only in $ETC/secret.key." >&2
  fi
  rm -f "$KEY_TMP"
fi
if [[ -f "$ETC/key.cred" ]]; then
  KEY_LINE="LoadCredentialEncrypted=brokersync-key:$ETC/key.cred"
  KEY_SOURCE=systemd-creds
else
  KEY_LINE="LoadCredential=brokersync-key:$ETC/secret.key"
  KEY_SOURCE=systemd
fi
for unit in wealthfolio-broker-sync wealthfolio-broker-sync-run; do
  install -d -m 755 "/etc/systemd/system/$unit.service.d"
  printf '[Service]\n%s\nEnvironment=BROKERSYNC_KEY_SOURCE=%s\n' "$KEY_LINE" "$KEY_SOURCE" \
    >"/etc/systemd/system/$unit.service.d/10-key.conf"
done

# TLS for the web UI: self-signed for this container's name and addresses,
# renewed when it runs out or an address changes; a certificate of your own in
# $ETC/tls (cert.pem, key.pem) is kept.
CERT_ARGS=(--dir "$ETC/tls" --host "$(hostname)")
for ip in $(hostname -I 2>/dev/null || true); do
  CERT_ARGS+=(--ip "$ip")
done
"$BASE/venv/bin/brokersync" make-cert "${CERT_ARGS[@]}"
chmod 600 "$ETC/tls/"*.pem

install -m 644 "$APP"/deploy/systemd/*.service "$APP"/deploy/systemd/*.timer /etc/systemd/system/
install -m 755 "$APP/deploy/brokersync-cli" /usr/local/bin/brokersync-cli
install -m 755 "$APP/deploy/brokersync-reset-password" /usr/local/bin/brokersync-reset-password
systemctl daemon-reload

# Data of older versions: credentials, settings and database encrypted with the
# key that now lives outside data/. Only then the old key file goes.
if /usr/local/bin/brokersync-cli migrate; then
  if [[ -f "$BASE/data/secret.key" ]]; then
    shred -u "$BASE/data/secret.key" 2>/dev/null || rm -f "$BASE/data/secret.key"
  fi
  # Backups of older versions hold the key next to the data: replace them by one of the encrypted data.
  replaced=0
  for f in "$BASE"/backup-*.tar.gz; do
    [[ -e "$f" ]] || continue
    if tar -tzf "$f" 2>/dev/null | grep -qx 'data/secret.key'; then
      rm -f "$f"
      replaced=1
    fi
  done
  if [[ "$replaced" == 1 ]]; then
    BACKUP="$BASE/backup-$(date +%Y%m%d-%H%M%S)-encrypted.tar.gz"
    tar -czf "$BACKUP" -C "$BASE" data
    chmod 600 "$BACKUP"
    echo "Older backups held the key next to the data; replaced by $BACKUP (encrypted data only)." >&2
  fi
else
  echo "Warning: bringing the data up to date failed - see above. The previous key stays in place." >&2
fi
chown -R "$USER_NAME:$USER_NAME" "$BASE/data"
chmod -R u=rwX,go= "$BASE/data"
