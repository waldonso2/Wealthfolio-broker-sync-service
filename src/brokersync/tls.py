"""The web UI's TLS certificate: self-signed, created by ``deploy/setup.sh``
(``brokersync make-cert``) and renewed when it runs out or the container's
addresses change. A certificate the user put there (not self-signed) is kept.
"""

from __future__ import annotations

import ipaddress
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

VALID_DAYS = 825  # what browsers accept for a certificate
RENEW_DAYS = 30


def _names(cert: x509.Certificate) -> tuple[set[str], set[str]]:
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return set(), set()
    return set(san.get_values_for_type(x509.DNSName)), {str(i) for i in san.get_values_for_type(x509.IPAddress)}


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def ensure_certificate(directory: Path, hosts: list[str], ips: list[str]) -> str:
    cert_file, key_file = directory / "cert.pem", directory / "key.pem"
    hosts = sorted({h for h in hosts + ["localhost"] if h})
    ips = sorted({str(ipaddress.ip_address(i)) for i in ips + ["127.0.0.1"] if _is_ip(i)})
    if cert_file.exists() and key_file.exists():
        cert = x509.load_pem_x509_certificate(cert_file.read_bytes())
        if cert.issuer != cert.subject:
            return f"Keeping the certificate in {cert_file} (not self-signed)."
        dns, addresses = _names(cert)
        fresh = cert.not_valid_after_utc - datetime.now(UTC) > timedelta(days=RENEW_DAYS)
        if fresh and set(hosts) <= dns and set(ips) <= addresses:
            return f"Certificate in {cert_file} is valid until {cert.not_valid_after_utc:%Y-%m-%d}."
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hosts[0] if hosts else "localhost"),
                      x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Wealthfolio Broker Sync")])
    now = datetime.now(UTC)
    san = [x509.DNSName(h) for h in hosts] + [x509.IPAddress(ipaddress.ip_address(i)) for i in ips]
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(days=VALID_DAYS))
            .add_extension(x509.SubjectAlternativeName(san), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .sign(key, hashes.SHA256()))
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    for path, data in ((key_file, key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                    serialization.NoEncryption())),
                       (cert_file, cert.public_bytes(serialization.Encoding.PEM))):
        tmp = path.with_name(path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    fingerprint = cert.fingerprint(hashes.SHA256()).hex(":").upper()
    return f"New self-signed certificate in {cert_file}, valid until {cert.not_valid_after_utc:%Y-%m-%d}; " \
           f"SHA-256 {fingerprint}"
