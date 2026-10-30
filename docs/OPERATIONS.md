# Operations: Updates, Security, Backups, and Technical Details

Back to the [README](../README.md).

## Updates

**Manual update:** Open the container console in Proxmox and run `update`. Settings and credentials are preserved. Before updating, the installer creates a backup of the encrypted data at `/opt/wealthfolio-companion/backup-*.tar.gz` (the three most recent are retained). Installation and updates first verify the release's build provenance (see *Release verification* below), then check every downloaded file against the release checksums in `SHA256SUMS`. If the provenance can't be verified, any checksum differs, or an unexpected file is present, the operation stops and the current version keeps running. To install a specific version, including an older one, run `VERSION=<version> update`. Without `VERSION`, updates never downgrade.

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

**Release verification (Proxmox install and update):** A release is accepted only when both hold:

1. `SHA256SUMS` carries the SLSA build provenance that `.github/workflows/release.yml` on `main` created for it, on a GitHub-hosted runner (Sigstore certificate identity `https://github.com/waldonso2/wealthfolio-companion/.github/workflows/release.yml@refs/heads/main`, issuer `https://token.actions.githubusercontent.com`; the attested SHA-256 must equal the downloaded file's).
2. Every deployed file matches `SHA256SUMS`, and there is no extra file.

No GitHub login is needed. The attestation comes from GitHub's public API (`api.github.com/repos/waldonso2/wealthfolio-companion/attestations/sha256:<digest>`, 60 unauthenticated requests per hour per address; when it refuses, the update says so and stops, and you retry later). The signature is checked **offline** against the Sigstore trusted root shipped in the pinned `sigstore` package: nothing is fetched from Sigstore's TUF server. The verifier (`src/brokersync/provenance.py`) runs in a throwaway virtual environment installed from `requirements-verify.txt` (exact versions, SHA-256 hashes, wheels only), before anything from the downloaded release runs.

*Trust chain and its anchor:* the verifier and its lock file are fetched from this repository's `main` branch over HTTPS (`raw.githubusercontent.com`), the same place and the same moment as the install script and the `update` command themselves, and never from the release being checked. A tampered release asset therefore cannot bring its own verifier. Whoever could change `main` could also change the install script, so this adds no new trust; it closes the gap of a release whose assets were replaced or uploaded outside the release workflow. Old containers need no bootstrap step: their `update` command always runs the current script from `main`, so the first update after this change is already fully verified. The check fails closed: no attestation, another signer (workflow, branch, repository, issuer, self-hosted runner), another digest, a download, `pip` or API failure all stop the install or update. A release only proves that it was built by the release workflow on `main`; it does not tell which version it is, so `setup.sh` still refuses a downgrade. If Sigstore ever rotates its root keys, verification fails until `requirements-verify.txt` is updated on `main` (`scripts/lock.sh`), which immediately fixes it for every container. The Docker image is verified separately (cosign signature and provenance in the registry).

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

## Technical details

For contributors: [ARCHITECTURE.md](../ARCHITECTURE.md) explains the structure and flow; [CLAUDE.md](../CLAUDE.md) describes the rules and how to add a broker.

- Python service `companion` (FastAPI UI over HTTPS on port 8443; port 8090 redirects; `wealthfolio-companion run` for the systemd timer) at `/opt/wealthfolio-companion`. Encrypted data lives in `/opt/wealthfolio-companion/data`; key and certificate in `/etc/wealthfolio-companion`. Runs as the dedicated `companion` user. Commands requiring the service key use `companion-cli <command>`.
- Wealthfolio is accessed through its REST API (`/api/v1`) using the Wealthfolio password.
- Scalable's CLI `sc` is installed at `/opt/wealthfolio-companion/bin/sc`; `wealthfolio-companion install-sc` installs or updates it (the install and update processes call it).
- Dependencies are installed only at the versions in `requirements.txt`, each checked against a hash; the community-scripts install engine is pinned to a reviewed revision. Report security issues privately; see [SECURITY.md](../SECURITY.md).
- Releases: a new version becomes installable after the version in `pyproject.toml` is bumped and merged into `main`. The release workflow runs the full CI first, then creates the GitHub release used by installation and updates. Each release contains the source archive, `SHA256SUMS` (all files), and GitHub SLSA build provenance, which installation and updates verify themselves (see *Release verification*). To check by hand: `gh attestation verify wealthfolio-companion-<version>.tar.gz --repo waldonso2/wealthfolio-companion` (needs a GitHub login). Published releases cannot be changed.
