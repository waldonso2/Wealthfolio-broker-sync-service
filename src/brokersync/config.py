"""Settings that aren't secret, kept in ``<data>/config.json``.

Users never edit this file: the web UI writes it. Secrets live in the vault.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

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
    brokers: dict[str, BrokerConfig] = field(default_factory=dict)
    # ISIN -> {"symbol", "exchangeMic", "name"}, like the addon's securityMappings.
    security_mappings: dict[str, dict] = field(default_factory=dict)
    # Outbound bank transfers to own accounts, like the addon's transferPatterns:
    # [{"label", "iban", "keyword", "destinationAccountId"}]. A matched outbound
    # transfer is booked as TRANSFER_OUT (plus TRANSFER_IN on the destination
    # account, if one is set) instead of a withdrawal.
    transfer_patterns: list[dict] = field(default_factory=list)

    def broker(self, key: str) -> BrokerConfig:
        return self.brokers.setdefault(key, BrokerConfig())


def load(directory: Path) -> Config:
    f = Path(directory) / "config.json"
    if not f.exists():
        return Config()
    raw = json.loads(f.read_text())
    brokers = {k: BrokerConfig(**v) for k, v in raw.pop("brokers", {}).items()}
    known = Config.__dataclass_fields__
    return Config(brokers=brokers, **{k: v for k, v in raw.items() if k in known})


def save(directory: Path, cfg: Config) -> None:
    d = Path(directory)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / "config.json.tmp"
    tmp.write_text(json.dumps(asdict(cfg), indent=2, ensure_ascii=False))
    os.replace(tmp, d / "config.json")
