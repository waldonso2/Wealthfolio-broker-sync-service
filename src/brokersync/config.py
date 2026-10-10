"""Settings, encrypted like everything else (``brokersync.crypto``).

- ``<data>/config.enc`` (data tier): Wealthfolio address, brokers, accounts,
  security mappings, transfer patterns (own IBANs).
- ``<data>/notify.enc`` (host tier): ntfy server, topic and token and the UI's
  address for links - so a locked service can still say it is locked.

Users never edit these files: the web UI writes them. ``config.json`` of
versions before 0.9.0 (and the ntfy token from the vault) are moved in on the
first read. Passwords and broker credentials live in the vault.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import crypto

DEFAULT_DATA_DIR = "/opt/wealthfolio-broker-sync/data"


def data_dir() -> Path:
    return Path(os.environ.get("BROKERSYNC_DATA", DEFAULT_DATA_DIR))


@dataclass
class BrokerConfig:
    enabled: bool = False
    cash_account_id: str = ""
    portfolio_account_id: str = ""
    # First run fetches from here (ISO date); later runs from the last success.
    start_date: str = ""


@dataclass
class Config:
    wealthfolio_url: str = "http://localhost:8080"
    # Address of this service's UI as the user reaches it; used for links in
    # notifications.
    public_url: str = ""
    ntfy_server: str = "https://ntfy.sh"
    ntfy_topic: str = ""
    ntfy_token: str = ""
    # Details (amounts, positions, error texts) in notifications; off: only what happened and a link.
    ntfy_details: bool = False
    brokers: dict[str, BrokerConfig] = field(default_factory=dict)
    # ISIN -> {"symbol", "exchangeMic", "name"}: the user's own ticker for a security.
    security_mappings: dict[str, dict] = field(default_factory=dict)
    # Outbound bank transfers to own accounts:
    # [{"label", "iban", "keyword", "destinationAccountId"}]. A matched outbound
    # transfer is booked as TRANSFER_OUT (plus TRANSFER_IN on the destination
    # account, if one is set) instead of a withdrawal.
    transfer_patterns: list[dict] = field(default_factory=list)

    def broker(self, key: str) -> BrokerConfig:
        return self.brokers.setdefault(key, BrokerConfig())


NOTIFY_FIELDS = ("public_url", "ntfy_server", "ntfy_topic", "ntfy_token", "ntfy_details")


def _read(path: Path, key: bytes, purpose: str) -> dict:
    return json.loads(crypto.unseal(key, path.read_bytes(), purpose)) if path.exists() else {}


def load_notify(directory: Path) -> Config:
    """Only the ntfy settings - readable while the service is locked."""
    d = Path(directory)
    if not (d / "notify.enc").exists() and (d / "config.json").exists():
        raw = json.loads((d / "config.json").read_text())
        return Config(**{k: raw[k] for k in NOTIFY_FIELDS if k in raw})
    raw = _read(d / "notify.enc", crypto.host_key(d), "notify")
    return Config(**{k: raw[k] for k in NOTIFY_FIELDS if k in raw})


def load(directory: Path) -> Config:
    d = Path(directory)
    if (d / "config.json").exists():
        _migrate(d)
    raw = _read(d / "config.enc", crypto.data_key(d), "config")
    raw.update(_read(d / "notify.enc", crypto.host_key(d), "notify"))
    brokers = {k: BrokerConfig(**v) for k, v in raw.pop("brokers", {}).items()}
    known = Config.__dataclass_fields__
    return Config(brokers=brokers, **{k: v for k, v in raw.items() if k in known})


def save(directory: Path, cfg: Config, key: bytes | None = None) -> None:
    d = Path(directory)
    data = asdict(cfg)
    notify = {k: data.pop(k) for k in NOTIFY_FIELDS}
    crypto.write_atomic(d / "config.enc", crypto.seal(key or crypto.data_key(d), json.dumps(data).encode(), "config"))
    crypto.write_atomic(d / "notify.enc", crypto.seal(crypto.host_key(d), json.dumps(notify).encode(), "notify"))


def _migrate(d: Path) -> None:
    """config.json (before 0.9.0) → config.enc + notify.enc; the ntfy token comes out of the vault."""
    from .vault import Vault

    with crypto.file_lock(d, "config.lock"):
        if not (d / "config.json").exists():
            return
        raw = json.loads((d / "config.json").read_text())
        brokers = {k: BrokerConfig(**v) for k, v in raw.pop("brokers", {}).items()}
        known = Config.__dataclass_fields__
        cfg = Config(brokers=brokers, **{k: v for k, v in raw.items() if k in known})
        vault = Vault(d)
        cfg.ntfy_token = cfg.ntfy_token or vault.load().get("ntfy_token") or ""
        save(d, cfg)
        if "ntfy_token" in vault.load():
            vault.update(lambda v: v.pop("ntfy_token", None))
        (d / "config.json").unlink()
