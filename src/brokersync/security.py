"""How the service protects what it stores: the checks on the page Sicherheit,
the master passphrase (set, change, remove) and the migration of data from
versions before 0.9.0 (``brokersync migrate``, run by ``deploy/setup.sh``).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
from dataclasses import dataclass, field
from pathlib import Path

from . import config as config_mod
from . import crypto
from . import state as state_mod
from .sync import run_lock
from .vault import Vault
from .web.i18n import N_

PLAINTEXT_LEFTOVERS = ("config.json", "secret.key")


@dataclass
class Check:
    """One line on the page *Sicherheit*. ``label`` and ``detail`` are UI text keys (German, ``web/i18n.py``,
    translated on the page); ``detail``'s placeholders are filled from ``params``, ``text`` is the German line."""
    label: str
    ok: bool
    detail: str
    warn: bool = False  # not a problem, but worth knowing
    params: dict[str, str] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return self.detail.format(**self.params) if self.params else self.detail


KEY_DETAILS = {
    "systemd-creds": N_("Mit systemd-creds verschlüsselt außerhalb von data/; der Dienst bekommt ihn nur beim Start."),
    "systemd": N_("Außerhalb von data/, nur für root lesbar; der Dienst bekommt ihn beim Start (systemd-creds "
                  "konnte hier nicht verschlüsseln)."),
    "file": N_("Aus BROKERSYNC_KEY_FILE."),
    "data": N_("Liegt in data/ neben den verschlüsselten Daten - wer data/ kopiert, kann alles entschlüsseln. "
               "Ein Update richtet das ein."),
}


def status(data_dir: Path, *, secure: bool) -> list[Check]:
    d = Path(data_dir)
    out: list[Check] = []
    source = crypto.host_key_source(d)
    out.append(Check(N_("Schlüssel des Dienstes"), source in ("systemd-creds", "systemd", "file"),
                     KEY_DETAILS[source]))
    if crypto.passphrase_enabled(d):
        out.append(Check(N_("Master-Passphrase"), True, N_("Gesetzt: Ohne sie sind Zugangsdaten, Einstellungen und "
                                                           "Sync-Daten nicht lesbar, auch nicht aus einer Sicherung.")))
    else:
        out.append(Check(N_("Master-Passphrase"), True, N_("Nicht gesetzt: Abrufe laufen nach einem Neustart ohne "
                                                           "Zutun weiter. Wer den ganzen Container samt Schlüssel "
                                                           "kopiert, kann die Daten entschlüsseln."), warn=True))
    secrets_file = d / "secrets.enc"
    sealed = secrets_file.exists() and crypto.is_sealed(secrets_file.read_bytes())
    out.append(Check(N_("Zugangsdaten und Sitzungen"), sealed or not secrets_file.exists(),
                     N_("AES-256-GCM (secrets.enc).") if sealed
                     else N_("Noch nichts gespeichert.") if not secrets_file.exists()
                     else N_("Altes Format - wird beim nächsten Lesen neu verschlüsselt.")))
    out.append(Check(N_("Einstellungen"), not (d / "config.json").exists(),
                     N_("AES-256-GCM (config.enc, Benachrichtigungen in notify.enc).")
                     if not (d / "config.json").exists() else N_("config.json liegt noch unverschlüsselt vor.")))
    db = d / "state.db"
    if not state_mod.ENCRYPTED:
        out.append(Check(N_("Sync-Datenbank"), False, N_("Unverschlüsselt: SQLCipher gibt es für diese Plattform "
                                                         "nicht (nur x86-64).")))
    else:
        plain = state_mod.is_plain(db)
        out.append(Check(N_("Sync-Datenbank"), not plain, N_("Mit SQLCipher verschlüsselt (AES-256).") if not plain
                         else N_("Noch unverschlüsselt - wird beim nächsten Öffnen verschlüsselt.")))
    out.append(_connection(secure))
    loose = _loose_permissions(d)
    out.append(Check(N_("Dateirechte"), not loose, N_("Nur der Dienst-Benutzer kann data/ lesen.") if not loose
                     else N_("Für andere lesbar: {files}"), params={"files": ", ".join(loose)} if loose else {}))
    leftovers = [f for f in PLAINTEXT_LEFTOVERS if (d / f).exists()]
    if source == "data":
        leftovers = [f for f in leftovers if f != "secret.key"]
    out.append(Check(N_("Klartext-Reste"), not leftovers, N_("Keine.") if not leftovers else "{files}",
                     params={"files": ", ".join(leftovers)} if leftovers else {}))
    return out


def _connection(secure: bool) -> Check:
    """HTTPS with the certificate systemd handed over: its validity and the CA's fingerprint to compare on import."""
    label = N_("Verbindung zur Oberfläche")
    if not secure:
        return Check(label, False, N_("Unverschlüsseltes HTTP - Passwörter gehen im Klartext durchs Netz."))
    cred = os.environ.get("CREDENTIALS_DIRECTORY")
    path = Path(cred) / "tls.crt" if cred else None
    if not path or not path.is_file():
        return Check(label, True, N_("HTTPS."))
    from cryptography import x509

    until = f"{x509.load_pem_x509_certificate(path.read_bytes()).not_valid_after_utc:%d.%m.%Y}"
    ca = ca_fingerprint()
    if not ca:
        return Check(label, True, N_("HTTPS. Zertifikat bis {until}"), params={"until": until})
    return Check(label, True, N_("HTTPS. Zertifikat bis {until}, CA SHA-256 {ca}"), params={"until": until, "ca": ca})


def ca_file() -> Path | None:
    """The CA certificate systemd handed over (for download and its fingerprint), if any."""
    cred = os.environ.get("CREDENTIALS_DIRECTORY")
    path = Path(cred) / "tls.ca" if cred else None
    return path if path and path.is_file() else None


def ca_fingerprint() -> str:
    path = ca_file()
    if not path:
        return ""
    from cryptography import x509

    from .tls import fingerprint

    return fingerprint(x509.load_pem_x509_certificate(path.read_bytes()))


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


def rotate_host_key(data_dir: Path, material: bytes) -> None:
    """Re-encrypt everything for a new host key (``deploy/setup.sh`` hands it over, then stores it).

    Everything is written next to the old files first and only then swapped in, so a failure before the swap
    leaves the data readable with the old key."""
    d = Path(data_dir)
    if len(material) < 32:
        raise ValueError("Der neue Schlüssel ist zu kurz.")
    with run_lock(d, wait=30), crypto.file_lock(d, "secrets.lock"):
        old_data = crypto.data_key(d)
        secrets_data = Vault(d).load() if (d / "secrets.enc").exists() else None
        cfg = config_mod.load(d)
        new_host, new_data, ring = crypto.keys_for_new_host_key(d, material)
        staged: list[tuple[Path, Path]] = []

        def stage(name: str, blob: bytes) -> None:
            tmp = d / f"{name}.rotate"
            crypto.write_atomic(tmp, blob)
            staged.append((tmp, d / name))

        if secrets_data is not None:
            stage("secrets.enc", crypto.seal(new_data, json.dumps(secrets_data).encode(), "secrets"))
        config_blob, notify_blob = config_mod.sealed(cfg, new_data, new_host)
        stage("config.enc", config_blob)
        stage("notify.enc", notify_blob)
        db = d / "state.db"
        if db.exists() and state_mod.ENCRYPTED:
            tmp = d / "state.db.rotate"
            shutil.copy2(db, tmp)
            os.chmod(tmp, 0o600)
            state_mod.rekey(d, old_data, new_data, path=tmp)
            staged.append((tmp, db))
        stage("keyring.json", json.dumps(ring, indent=2).encode())
        for tmp, target in staged:
            os.replace(tmp, target)
