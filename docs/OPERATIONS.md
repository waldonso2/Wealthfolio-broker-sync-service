# Operations: Updates, Security, Backups, and Technical Details

Back to the [README](../README.md).

## Updates

**Manual update:** Open the container console in Proxmox and run `update`. Settings and credentials are preserved. Before updating, the installer creates a backup of the encrypted data at `/opt/wealthfolio-companion/backup-*.tar.gz` (the three most recent are retained). Installation and updates check every downloaded file against the release checksums in `SHA256SUMS`. If any checksum differs, or an unexpected file is present, the operation stops and the current version keeps running. To install a specific version, including an older one, run `VERSION=<version> update`. Without `VERSION`, updates never downgrade.

**New-release notifications:** After a sync, the service checks GitHub once per day for the latest release (it only follows the `releases/latest` redirect; no data is sent). If a newer release exists, the *Broker* page shows it and ntfy sends one notification. Disable this with `Environment=COMPANION_UPDATE_CHECK=0` via `systemctl edit wealthfolio-companion-run`.

**Automatic updates (optional, *Updates* page):** When enabled, the container checks for a new release at the configured interval (minimum one minute, maximum once per day) and installs it using the same `update` command described above, including checksum validation and a pre-update backup. ntfy reports the installed version, and the *Updates* page shows the latest result. If an update fails, the previous version continues running and the same version is not retried for an hour. Updates do not run during a sync. The container timer `wealthfolio-companion-update.timer` runs every minute as root because it performs the same operation as `update`; when disabled, it installs nothing. Enabling or disabling it requires the UI password. Inspect its logs with `journalctl -u wealthfolio-companion-update` in the console.

## Security

Stored data is encrypted. The **Security** page always shows what is protected and how; from the console, run `companion-cli security-status`.

| Data | Protection |
|---|---|
| Credentials, sessions, tokens, Wealthfolio password, logo.dev key | AES-256-GCM (`data/secrets.enc`) |
| Settings (accounts, securities, transfers with IBANs) | AES-256-GCM (`data/config.enc`) |
| Sync database (bookings, holdings, runs) | SQLCipher, AES-256 (`data/state.db`) |
| ntfy settings | AES-256-GCM (`data/notify.enc`), using only the service key so notifications can be sent while the service is locked |
| Service key | **Not** in `data/`: encrypted with `systemd-creds` at `/etc/wealthfolio-companion/key.cred` (root-only); systemd provides it at startup |
| UI password | argon2id; 15-minute pause after five failed attempts, and the same pause after 30 failed attempts across all clients; at most two checks run simultaneously to limit memory exhaustion. The initial password requires the one-time setup code from the console |
| UI | HTTPS on port 8443 using a certificate issued by the container's own CA. Import the CA once from the *Security* page to trust it in your browser. Its name constraints permit only local-network names; its key is root-only in `/etc/wealthfolio-companion/tls/`. An existing certificate from another issuer (`cert.pem`/`key.pem`) is preserved. Secure cookies and strict security headers are also used |
| Logs, notifications, and error messages | Credentials, tokens, IBANs, and account numbers are redacted in logs, ntfy messages, and UI errors. Request URLs are not logged. ntfy messages omit amounts unless *Details* is enabled |
| Service | Dedicated user and systemd hardening (write access only to `data/`, no privilege escalation, restricted system calls; `systemd-analyze security`: 1.5); files readable only by the service; daily sync ends within 45 minutes |

**Optional master passphrase (*Security* page):** Without it, someone who copies the entire container and its key (for example, a Proxmox backup) can decrypt the data. With it, they cannot: the effective key is derived from both the service key and the passphrase using argon2id. You must unlock the service in the UI after each container restart; until then it does not sync and sends an ntfy notification. **If you lose the passphrase, the data is lost** and the service must be set up again. Setting, changing, or removing the passphrase (and key rotation during updates) re-encrypts everything: first alongside the old files, then switches over atomically. If power is lost, the next start recovers either the old or new state, never a mixture.

**Events:** The *Security* page lists logins (including failures), logouts, and changes to credentials, password, passphrase, and sync mode for the last 180 days, including the source address but no passwords or credentials. A login from an address never seen before triggers an ntfy notification.

## Backup and restore

Before updating, `update` backs up `data/` to `/opt/wealthfolio-companion/backup-*.tar.gz`—encrypted data only, without the key. You can also create a backup manually from the container console:

```bash
(umask 077; companion-cli backup > /root/companion-$(date +%F).tar.gz)
systemctl stop wealthfolio-companion
companion-cli restore < /root/companion-2026-10-15.tar.gz
systemctl start wealthfolio-companion
```

`restore` first verifies that the archive can be opened with this container's key (and the master passphrase, if configured). Otherwise, it changes nothing. The previous state is kept as `data/before-restore-*.tar.gz`. Update backups can also be restored this way.

A backup is **complete only when paired with the key** from `/etc/wealthfolio-companion`; without the key, `data/` is useless. The easiest full backup is a Proxmox backup of the entire container. It includes both key and data, so store it on an encrypted target or configure the master passphrase.

**Sync mode (*Security* page):**
- **Automatic** (default): broker and bank sessions remain saved and the sync runs every morning.
- **Secure:** each sync starts with a fresh login—app approval, TAN, or device code. The service logs out immediately afterwards and deletes the session, so no reusable login remains.
  - The scheduled run does not sync; it reminds you via ntfy.
  - **Fetch all** walks you through the logins one by one (you can skip individual brokers) and then syncs all of them.
  - The mode applies to all brokers.

**Do not save PIN (DKB, Deutsche Bank):** Leave the PIN field empty in broker settings to be prompted for the PIN on every login. It remains in memory only until the sync ends and is never stored. Each sync then requires a login, even in automatic mode; the scheduled run cannot fetch this bank. To delete a previously saved PIN, use the checkbox below the PIN field.

**Wealthfolio:** Prefer connecting to Wealthfolio over HTTPS; otherwise the Wealthfolio password travels over the network unencrypted. The *Security* page warns you about this.

## Moving from Wealthfolio Broker Sync

An installation of Wealthfolio Broker Sync is not updated into Wealthfolio Companion: you install Wealthfolio Companion fresh and bring your data over with a backup and the old key. Nothing gets lost on the way: `restore` first checks that the backup opens with the key the new installation uses, and changes nothing otherwise - the old installation stays as it is until you remove it.

**Before you start:** if you set a master passphrase, remove it on the page *Sicherheit* of the old installation first (you set it again afterwards): a fresh installation can't unlock a backup sealed with it.

### Proxmox VE (LXC)

1. In the console of the **old** container, write a backup and the key, and stop the daily fetch so nothing new is booked there:

   ```bash
   (umask 077; brokersync-cli backup > /root/move.tar.gz)
   (umask 077; if [ -f /etc/wealthfolio-broker-sync/key.cred ]; then systemd-creds decrypt --name=brokersync-key /etc/wealthfolio-broker-sync/key.cred - > /root/move.key; else cp /etc/wealthfolio-broker-sync/secret.key /root/move.key; fi)
   systemctl disable --now wealthfolio-broker-sync-run.timer wealthfolio-broker-sync-update.timer
   ```

2. Install Wealthfolio Companion as a new container: in the Proxmox host shell

   ```bash
   bash -c "$(curl -fsSL https://raw.githubusercontent.com/waldonso2/wealthfolio-companion/main/ct/wealthfolio-companion.sh)"
   ```

   You don't need the setup code it shows: the backup brings your password along.

3. In the Proxmox host shell, copy both files from the old container (`OLD`, its ID) to the new one (`NEW`):

   ```bash
   OLD=101 NEW=102   # your containers' IDs (pct list)
   (umask 077; pct pull $OLD /root/move.tar.gz /root/move.tar.gz && pct pull $OLD /root/move.key /root/move.key)
   pct push $NEW /root/move.tar.gz /root/move.tar.gz --perms 600 && pct push $NEW /root/move.key /root/move.key --perms 600
   shred -u /root/move.key /root/move.tar.gz
   ```

4. In the console of the **new** container, stop the service, put the old key in place of the new one (`setup.sh` seals a root-only `secret.key` with systemd-creds), empty the new installation's data (it holds nothing yet), run the setup again and restore the backup:

   ```bash
   systemctl stop wealthfolio-companion wealthfolio-companion-run.timer
   rm -f /etc/wealthfolio-companion/key.cred /etc/wealthfolio-companion/secret.key
   install -m 600 /root/move.key /etc/wealthfolio-companion/secret.key
   find /opt/wealthfolio-companion/data -mindepth 1 -delete
   bash /opt/wealthfolio-companion/app/deploy/setup.sh
   companion-cli restore < /root/move.tar.gz
   systemctl start wealthfolio-companion wealthfolio-companion-run.timer
   shred -u /root/move.key /root/move.tar.gz
   ```

   If `restore` says *Not restored, data/ unchanged*, the key doesn't fit the backup: check step 1 and run step 4 again.

5. Open the new container's web UI (`https://<its address>:8443`) and sign in with your old password. Its certificate comes from the new container's own CA: confirm the browser warning once, or import `/ca.crt` again. Set the master passphrase again if you had one, and switch the automatic update on again on the page *Updates* if you used it.

6. When everything is there, stop and remove the old container in Proxmox.

### Docker / Podman

The old key and the old data volume stay as they are; only the image, the secret's name and the variables change.

1. Make a backup first (the old image's command is `brokersync`):

   ```bash
   (umask 077; docker compose exec -T brokersync brokersync backup > before-move.tar.gz)
   ```

2. Change `docker-compose.yml` - keep the service, volume and `hostname` names (a new hostname would replace the UI's CA):

   ```diff
    services:
      brokersync:
   -    image: ghcr.io/waldonso2/wealthfolio-broker-sync:latest
   +    image: ghcr.io/waldonso2/wealthfolio-companion:latest
        environment:
   -      BROKERSYNC_CERT_NAMES: "nas.fritz.box"
   +      COMPANION_CERT_NAMES: "nas.fritz.box"
        secrets:
   -      - brokersync-key
   +      - companion-key
    secrets:
   -  brokersync-key:
   +  companion-key:
        file: ./brokersync.key
   ```

   Every `BROKERSYNC_…` variable becomes `COMPANION_…` (`_TLS`, `_TRUST_PROXY`, `_ALLOWED_HOSTS`, `_RUN_AT`, `_METRICS_TOKEN`, `_LOG_FORMAT` …). With `docker run`, change the image the same way and mount the key as `/run/secrets/companion-key`.

3. Start it:

   ```bash
   docker compose pull && docker compose up -d
   docker compose logs brokersync
   ```

   Commands in the container are now `wealthfolio-companion …` (`docker compose exec brokersync wealthfolio-companion security-status`). Should the data not open, the log says so and nothing was changed: `before-move.tar.gz` restores the old state with the old image.

## Technical details

For contributors: [ARCHITECTURE.md](../ARCHITECTURE.md) explains the structure and flow; [CLAUDE.md](../CLAUDE.md) describes the rules and how to add a broker.

- Python service `companion` (FastAPI UI over HTTPS on port 8443; port 8090 redirects; `wealthfolio-companion run` for the systemd timer) at `/opt/wealthfolio-companion`. Encrypted data lives in `/opt/wealthfolio-companion/data`; key and certificate in `/etc/wealthfolio-companion`. Runs as the dedicated `companion` user. Commands requiring the service key use `companion-cli <command>`.
- Wealthfolio is accessed through its REST API (`/api/v1`) using the Wealthfolio password.
- Scalable's CLI `sc` is installed at `/opt/wealthfolio-companion/bin/sc`; `wealthfolio-companion install-sc` installs or updates it (the install and update processes call it).
- Dependencies are installed only at the versions in `requirements.txt`, each checked against a hash; the community-scripts install engine is pinned to a reviewed revision. Report security issues privately; see [SECURITY.md](../SECURITY.md).
- Releases: a new version becomes installable after the version in `pyproject.toml` is bumped and merged into `main`. The release workflow runs the full CI first, then creates the GitHub release used by installation and updates. Each release contains the source archive, `SHA256SUMS` (all files), and GitHub SLSA build provenance. Verify it with `gh attestation verify wealthfolio-companion-<version>.tar.gz --repo waldonso2/wealthfolio-companion`. Published releases cannot be changed.
