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

## What the service does to protect data

See *Sicherheit* in the [README](README.md#sicherheit): everything stored is encrypted, the key is
outside the data directory, logs and notifications are redacted, the dependencies are installed
only at locked, hash-checked versions, and the install engine is pinned to a reviewed commit.
