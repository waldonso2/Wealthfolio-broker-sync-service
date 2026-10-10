"""How the service protects what it stores: the checks on the page Sicherheit,
the master passphrase (set, change, remove) and the migration of data from
versions before 0.9.0 (``brokersync migrate``, run by ``deploy/setup.sh``).
"""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from . import config as config_mod
from . import crypto
from . import state as state_mod
from .sync import run_lock
from .vault import Vault

PLAINTEXT_LEFTOVERS = ("config.json", "secret.key")


@dataclass
class Check:
    label: str
    ok: bool
    detail: str
    warn: bool = False  # not a problem, but worth knowing


def status(data_dir: Path, *, secure: bool) -> list[Check]:
    d = Path(data_dir)
    out: list[Check] = []
    source = crypto.host_key_source(d)
    out.append(Check(
        "Schlüssel des Dienstes",
        source in ("systemd-creds", "systemd", "file"),
        {"systemd-creds": "Mit systemd-creds verschlüsselt außerhalb von data/; der Dienst bekommt ihn nur beim Start.",
         "systemd": "Außerhalb von data/, nur für root lesbar; der Dienst bekommt ihn beim Start (systemd-creds "
                    "konnte hier nicht verschlüsseln).",
         "file": "Aus BROKERSYNC_KEY_FILE.",
         "data": "Liegt in data/ neben den verschlüsselten Daten - wer data/ kopiert, kann alles entschlüsseln. "
                 "Ein Update richtet das ein."}[source],
    ))
    if crypto.passphrase_enabled(d):
        out.append(Check("Master-Passphrase", True, "Gesetzt: Ohne sie sind Zugangsdaten, Einstellungen und "
                                                     "Sync-Daten nicht lesbar, auch nicht aus einer Sicherung."))
    else:
        out.append(Check("Master-Passphrase", True, "Nicht gesetzt: Abrufe laufen nach einem Neustart ohne Zutun "
                                                     "weiter. Wer den ganzen Container samt Schlüssel kopiert, kann "
                                                     "die Daten entschlüsseln.", warn=True))
    secrets_file = d / "secrets.enc"
    sealed = secrets_file.exists() and crypto.is_sealed(secrets_file.read_bytes())
    out.append(Check("Zugangsdaten und Sitzungen", sealed or not secrets_file.exists(),
                     "AES-256-GCM (secrets.enc)." if sealed else "Noch nichts gespeichert." if not secrets_file.exists()
                     else "Altes Format - wird beim nächsten Lesen neu verschlüsselt."))
    out.append(Check("Einstellungen", not (d / "config.json").exists(),
                     "AES-256-GCM (config.enc, Benachrichtigungen in notify.enc)." if not (d / "config.json").exists()
                     else "config.json liegt noch unverschlüsselt vor."))
    db = d / "state.db"
    if not state_mod.ENCRYPTED:
        out.append(Check("Sync-Datenbank", False, "Unverschlüsselt: SQLCipher gibt es für diese Plattform nicht "
                                                  "(nur x86-64)."))
    else:
        plain = state_mod.is_plain(db)
        out.append(Check("Sync-Datenbank", not plain, "Mit SQLCipher verschlüsselt (AES-256)." if not plain
                         else "Noch unverschlüsselt - wird beim nächsten Öffnen verschlüsselt."))
    out.append(Check("Verbindung zur Oberfläche", secure, f"HTTPS.{_fingerprint()}" if secure
                     else "Unverschlüsseltes HTTP - Passwörter gehen im Klartext durchs Netz."))
    loose = _loose_permissions(d)
    out.append(Check("Dateirechte", not loose, "Nur der Dienst-Benutzer kann data/ lesen." if not loose
                     else "Für andere lesbar: " + ", ".join(loose)))
    leftovers = [f for f in PLAINTEXT_LEFTOVERS if (d / f).exists()]
    if source == "data":
        leftovers = [f for f in leftovers if f != "secret.key"]
    out.append(Check("Klartext-Reste", not leftovers, "Keine." if not leftovers else ", ".join(leftovers)))
    return out


def _fingerprint() -> str:
    """SHA-256 of the certificate systemd handed over - to compare with what the browser shows."""
    cred = os.environ.get("CREDENTIALS_DIRECTORY")
    path = Path(cred) / "tls.crt" if cred else None
    if not path or not path.is_file():
        return ""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes

    cert = x509.load_pem_x509_certificate(path.read_bytes())
    fingerprint = cert.fingerprint(hashes.SHA256()).hex(":").upper()
    return f" Zertifikat bis {cert.not_valid_after_utc:%d.%m.%Y}, SHA-256 {fingerprint}"


def _loose_permissions(d: Path) -> list[str]:
    loose = []
    if d.exists() and stat.S_IMODE(d.stat().st_mode) & 0o077:
        loose.append("data/")
    for f in sorted(d.glob("*")) if d.exists() else []:
        if f.is_file() and stat.S_IMODE(f.stat().st_mode) & 0o077:
            loose.append(f.name)
    return loose


def change_passphrase(data_dir: Path, new: str | None) -> None:
    """Set, change (``new``) or remove (``None``) the master passphrase: everything in the data tier is
    re-encrypted. The service must be unlocked; no sync may run meanwhile."""
    d = Path(data_dir)
    if new is not None and len(new) < crypto.MIN_PASSPHRASE:
        raise ValueError(f"Die Passphrase braucht mindestens {crypto.MIN_PASSPHRASE} Zeichen.")
    with run_lock(d, wait=30), crypto.file_lock(d, "secrets.lock"):
        old_key = crypto.data_key(d)
        vault = Vault(d)
        secrets_data = vault.load()
        cfg = config_mod.load(d)
        new_key, ring = crypto.new_passphrase_key(d, new)
        # Written next to the old files first: a failure before the switch leaves everything as it was.
        sealed_secrets = crypto.seal(new_key, json.dumps(secrets_data).encode(), "secrets")
        state_mod.rekey(d, old_key, new_key)
        crypto.write_atomic(vault.file, sealed_secrets)
        config_mod.save(d, cfg, new_key)
        crypto.write_keyring(d, ring)


def migrate(data_dir: Path) -> list[str]:
    """Bring the data of an older version up to date (run by deploy/setup.sh); returns what was done."""
    d = Path(data_dir)
    done = []
    if (d / "secrets.enc").exists() and not crypto.is_sealed((d / "secrets.enc").read_bytes()):
        Vault(d).load()
        done.append("Zugangsdaten mit AES-256-GCM neu verschlüsselt")
    if (d / "config.json").exists():
        config_mod.load(d)
        done.append("Einstellungen verschlüsselt")
    if state_mod.ENCRYPTED and state_mod.is_plain(d / "state.db"):
        state_mod.State(d).close()
        done.append("Sync-Datenbank mit SQLCipher verschlüsselt")
    if crypto.is_locked(d):
        return done
    cfg = config_mod.load(d)
    m = re.fullmatch(r"http://([^/:]+):8090", cfg.public_url or "")
    if m:
        cfg.public_url = f"https://{m.group(1)}:8443"
        config_mod.save(d, cfg)
        done.append("Adresse für Links auf HTTPS umgestellt")
    for f in [d] + [p for p in d.glob("*") if p.is_file()]:
        mode = stat.S_IMODE(f.stat().st_mode)
        want = 0o700 if f.is_dir() else 0o600
        if mode & 0o077:
            os.chmod(f, want)
    return done
