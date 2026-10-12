# Security policy

Wealthfolio Broker Sync holds bank and broker credentials. Please report a vulnerability
**privately**, not as a public issue:

- GitHub: *Security* → *Report a vulnerability* in this repository (private vulnerability reporting).

Please include what is affected (version, component - web UI, sync, an adapter, install/update),
how to reproduce it, and what an attacker gains. You get an answer within a week; a fix is
released as a new version (the `update` command in the container installs it).

## Supported versions

Only the latest release. Install and update always deploy the latest release.

## Scope

In scope: this repository - the service, its web UI, the install and update scripts, the release.
Not in scope: Wealthfolio itself, the brokers' and banks' interfaces, community-scripts/core
(report to those projects), and attacks that need root on the container or the Proxmox host.

## Security libraries and measures (web UI and storage)

What the service uses, and what it deliberately does not (evaluated in #33):

| Concern | Used | Why / instead of |
|---|---|---|
| Password hashing (UI password) | `argon2-cffi`, argon2id (64 MiB, t=3) | stronger than bcrypt/scrypt; scrypt hashes of old versions are upgraded at login |
| Encryption at rest | `cryptography` (AES-256-GCM, HKDF), `sqlcipher3` for `state.db` | the key comes from systemd-creds, optionally combined with a master passphrase (argon2id) |
| Sessions | Starlette `SessionMiddleware` (`itsdangerous`-signed cookie: `Secure`, `SameSite=strict`, 24 h) | a fresh session at login and at the first password |
| CSRF | a token per session, checked for every POST (FastAPI dependency) | together with `SameSite=strict` |
| XSS | Jinja2 autoescaping, CSP `script-src 'none'` (the UI has no JavaScript) | |
| Security headers | own middleware `web/gate.py` (CSP, HSTS, `X-Frame-Options`, `nosniff`, `no-store`, …) | the Python equivalent of helmet; no extra dependency |
| Brute force | `web/gate.Throttle`: 15 minutes after 5 wrong passwords (login, unlock) | a library (slowapi) isn't needed for one login form; everything else needs a session |
| Input validation | `web/inputs.py`: URLs (http/https only), dates, ISIN/IBAN check digits, MIC, symbols, lengths; request bodies over 6 MB refused before reading | Pydantic models would add little for a dozen HTML forms; FastAPI still types each field |
| Secrets in the environment | none: no `.env`, no `python-dotenv` | secrets live in the encrypted vault, the key in systemd credentials |
| Supply chain | `requirements.txt` with hashes, Dependabot, gitleaks in CI, actions pinned to SHAs | |

Node libraries (helmet, cors, bcryptjs, zod, express-rate-limit) don't apply: the Wealthfolio addon has no
server, stores no passwords and makes no network requests.

## What the service does to protect data

See *Sicherheit* in the [README](README.md#sicherheit): everything stored is encrypted, the key is
outside the data directory, logs and notifications are redacted, the dependencies are installed
only at locked, hash-checked versions, and the install engine is pinned to a reviewed commit.
