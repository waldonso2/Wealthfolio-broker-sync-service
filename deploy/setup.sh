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

# No way back to an older version unless asked for (VERSION=... update sets BROKERSYNC_ALLOW_DOWNGRADE):
# an older release may lack a fix, and data a newer version wrote may not open in it (#88).
NEW_VERSION="$(sed -n 's/^version = "\(.*\)"$/\1/p' "$APP/pyproject.toml")"
if [[ -x "$BASE/venv/bin/brokersync" && "${BROKERSYNC_ALLOW_DOWNGRADE:-}" != 1 ]]; then
  OLD_VERSION="$("$BASE/venv/bin/brokersync" --version 2>/dev/null || true)"
  if [[ -n "$OLD_VERSION" && "$(printf '%s\n%s\n' "$OLD_VERSION" "$NEW_VERSION" | sort -V | tail -n 1)" != "$NEW_VERSION" ]]; then
    echo "Not going back from $OLD_VERSION to $NEW_VERSION - to do that on purpose: VERSION=$NEW_VERSION update" >&2
    exit 1
  fi
fi

# Keep the old environment until the new one is complete: a failed install
# (network, disk, a broken package) puts the previous version back instead of
# leaving a service that can't start. The service is stopped meanwhile.
rm -rf "$BASE/venv.old"
[[ -d "$BASE/venv" ]] && mv "$BASE/venv" "$BASE/venv.old"
# Only the locked versions (requirements.txt, with hashes): a package that was changed on PyPI,
# or a dependency nobody tested, is refused. The package itself is built with the locked
# setuptools, nothing else is fetched.
PIP=("$BASE/venv/bin/pip" install --quiet --disable-pip-version-check --no-cache-dir)
if ! { python3 -m venv "$BASE/venv" &&
  "${PIP[@]}" --require-hashes --only-binary :all: -r "$APP/requirements.txt" &&
  "${PIP[@]}" --no-deps --no-build-isolation "$APP" &&
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

new_key() {
  "$BASE/venv/bin/python" -c 'import base64, secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())'
}
remove() {
  shred -u "$1" 2>/dev/null || rm -f "$1"
}
# encrypt_key <file> <credential>: systemd-creds, with the TPM where there is one (#94), checked by decrypting;
# $ETC/key-protection says which (tpm2 or host).
encrypt_key() {
  local with=host
  if timeout 30 systemd-creds has-tpm2 >/dev/null 2>&1; then
    with=tpm2+host
  fi
  timeout 60 systemd-creds encrypt --with-key="$with" --name=brokersync-key "$1" "$2" 2>/dev/null &&
    timeout 60 systemd-creds decrypt --name=brokersync-key "$2" - 2>/dev/null | cmp -s - "$1" &&
    chmod 600 "$2" && echo "${with%+host}" >"$ETC/key-protection"
}
# store_key <file> <suffix>: $ETC/key.cred<suffix> (systemd-creds) or, where systemd-creds can't encrypt,
# $ETC/secret.key<suffix> (root-only). Prints the file it wrote.
store_key() {
  if [[ ! -f "$ETC/secret.key" ]] && encrypt_key "$1" "$ETC/key.cred$2"; then
    echo "$ETC/key.cred$2"
  else
    rm -f "$ETC/key.cred$2"
    install -m 600 "$1" "$ETC/secret.key$2"
    [[ -n "$2" ]] || echo "Note: systemd-creds can't encrypt here - the key is kept root-only in $ETC/secret.key." >&2
    echo "$ETC/secret.key$2"
  fi
}

# The service's key: never in data/. Encrypted with systemd-creds (bound to this
# container's host key), or root-only where systemd-creds can't encrypt; systemd
# hands it to the services at start. An installation before 0.9.0 keeps its key.
if [[ ! -f "$ETC/key.cred" && ! -f "$ETC/secret.key" ]]; then
  KEY_TMP="$(mktemp -p "$ETC")"
  if [[ -f "$BASE/data/secret.key" ]]; then
    cat "$BASE/data/secret.key" >"$KEY_TMP"
  else
    new_key >"$KEY_TMP"
    touch "$ETC/key-rotated" # a new installation: the key has never been anywhere else
  fi
  store_key "$KEY_TMP" "" || true
  remove "$KEY_TMP"
fi
# A key kept root-only because systemd-creds couldn't encrypt it then: moved as soon as it can (#94).
if [[ -f "$ETC/secret.key" && ! -f "$ETC/key.cred" ]]; then
  if encrypt_key "$ETC/secret.key" "$ETC/key.cred"; then
    remove "$ETC/secret.key"
    echo "The service's key is now encrypted with systemd-creds." >&2
  else
    rm -f "$ETC/key.cred"
  fi
fi
if [[ -f "$ETC/key.cred" ]]; then
  KEY_LINE="LoadCredentialEncrypted=brokersync-key:$ETC/key.cred"
  KEY_SOURCE=systemd-creds
  # Encrypted with the TPM (#94): the page Sicherheit says so; without one, a container backup holds the key.
  if [[ "$(cat "$ETC/key-protection" 2>/dev/null)" == tpm2 ]]; then
    KEY_SOURCE=systemd-creds-tpm2
  fi
else
  KEY_LINE="LoadCredential=brokersync-key:$ETC/secret.key"
  KEY_SOURCE=systemd
fi
for unit in wealthfolio-broker-sync wealthfolio-broker-sync-run; do
  install -d -m 755 "/etc/systemd/system/$unit.service.d"
  printf '[Service]\n%s\nEnvironment=BROKERSYNC_KEY_SOURCE=%s\n' "$KEY_LINE" "$KEY_SOURCE" \
    >"/etc/systemd/system/$unit.service.d/10-key.conf"
done

# TLS for the web UI: a certificate from the container's own CA (limited to its name and addresses, #92),
# renewed when it runs out or an address changes - here and daily by wealthfolio-broker-sync-cert.timer.
# More names or addresses: one per line in $ETC/tls/extra-names. A certificate of your own in $ETC/tls
# (cert.pem, key.pem) is kept.
"$BASE/venv/bin/brokersync" renew-cert --dir "$ETC/tls" --no-restart

install -m 644 "$APP"/deploy/systemd/*.service "$APP"/deploy/systemd/*.timer /etc/systemd/system/
install -m 755 "$APP/deploy/brokersync-cli" /usr/local/bin/brokersync-cli
install -m 755 "$APP/deploy/brokersync-reset-password" /usr/local/bin/brokersync-reset-password
install -m 755 "$APP/deploy/brokersync-autoupdate" /usr/local/bin/brokersync-autoupdate
systemctl daemon-reload
systemctl enable -q --now wealthfolio-broker-sync-cert.timer || true
# Every minute; it installs nothing until the page Updates switches it on.
systemctl enable -q --now wealthfolio-broker-sync-update.timer || true

# Data of older versions: credentials, settings and database encrypted with the
# key that now lives outside data/. Only then the old key file goes.
if timeout 900 /usr/local/bin/brokersync-cli migrate; then
  if [[ -f "$BASE/data/secret.key" ]]; then
    remove "$BASE/data/secret.key"
  fi
  # Once: a fresh key. Up to 0.9.0 the key of data/secret.key was moved to $ETC, not replaced, so
  # backups of older versions (and old Proxmox backups) still held a key that opens the current data.
  if [[ ! -f "$ETC/key-rotated" ]]; then
    NEW_KEY="$(mktemp -p "$ETC")"
    new_key >"$NEW_KEY"
    STAGED="$(store_key "$NEW_KEY" .new)"
    if timeout 900 /usr/local/bin/brokersync-cli rotate-key <"$NEW_KEY"; then
      # Storing the key is the switch: rotate-key only staged the re-encrypted files, recover swaps them
      # in (every start of the service does too, should anything stop in between).
      mv -f "$STAGED" "${STAGED%.new}"
      timeout 900 /usr/local/bin/brokersync-cli recover
      touch "$ETC/key-rotated"
      echo "New key: everything re-encrypted; copies of the old key no longer open the data." >&2
    else
      remove "$STAGED"
      timeout 900 /usr/local/bin/brokersync-cli recover || true # drops what a failed attempt staged
      echo "Warning: the key was not replaced (locked by the master passphrase? unlock and run update again)." >&2
    fi
    remove "$NEW_KEY"
  fi
  # Backups of older versions hold the key next to the data: replace them by one of the encrypted data.
  # (grep reads the whole listing: with -q it would stop early, tar would get SIGPIPE and pipefail say no.)
  replaced=0
  for f in "$BASE"/backup-*.tar.gz; do
    [[ -e "$f" ]] || continue
    if tar -tzf "$f" 2>/dev/null | grep -x 'data/secret.key' >/dev/null; then
      rm -f "$f"
      replaced=1
    fi
  done
  if [[ "$replaced" == 1 ]]; then
    BACKUP="$BASE/backup-$(date +%Y%m%d-%H%M%S)-encrypted.tar.gz"
    (umask 077 && tar -czf "$BACKUP" -C "$BASE" data)
    echo "Older backups held the key next to the data; replaced by $BACKUP (encrypted data only)." >&2
  fi
else
  echo "Warning: bringing the data up to date failed - see above. The previous key stays in place." >&2
fi
chown -R "$USER_NAME:$USER_NAME" "$BASE/data"
chmod -R u=rwX,go= "$BASE/data"
# Run by hand while the service is up (the update stops it first): restart it with the new files and key.
systemctl try-restart wealthfolio-broker-sync || true
