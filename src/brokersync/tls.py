"""The web UI's TLS certificate, from a small certificate authority of this container.

``deploy/setup.sh`` (``wealthfolio-companion make-cert``) keeps two things in
``/etc/wealthfolio-companion/tls`` (root only):

- **The CA** (``ca.pem``, ``ca-key.pem``): valid 2 years. The user imports
  ``ca.pem`` once (page *Sicherheit*, ``/ca.crt``); from then on the browser trusts
  the UI, renewed certificates included. It may only sign this container (#92,
  *name constraints*): its name, ``localhost`` and its current addresses, each a
  single address - not the router, the NAS or any other device, even for whoever
  has its key. A new name or address means a new CA, to import once more. The
  service never sees the key.
- **The certificate** (``cert.pem``, ``key.pem``), signed by the CA, for the
  container's name and private addresses; renewed when it runs out or an
  address changes - ``wealthfolio-companion renew-cert`` daily (``wealthfolio-companion-cert.timer``).
  More names or addresses (a Tailscale address, a DNS name) go one per line into
  ``extra-names`` there. A certificate the user put there (from another issuer) is kept.
"""

from __future__ import annotations

import ipaddress
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

VALID_DAYS = 825  # what browsers accept for a certificate of a private CA
RENEW_DAYS = 30
CA_VALID_DAYS = 730
CA_RENEW_DAYS = 60
CA_NAME = "Wealthfolio Companion CA"
# Addresses a certificate may name: private networks, and 100.64.0.0/10 (Tailscale, carrier-grade NAT).
PRIVATE_NETWORKS = [ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "100.64.0.0/10", "fc00::/7", "fe80::/10",
    "::1/128")]
EXTRA_NAMES = "extra-names"


def _names(cert: x509.Certificate) -> tuple[set[str], set[str]]:
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return set(), set()
    return set(san.get_values_for_type(x509.DNSName)), {str(i) for i in san.get_values_for_type(x509.IPAddress)}


def _private_ip(value: str) -> str | None:
    """The address in canonical form if it is in the local network, else None (also for garbage)."""
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return None
    return str(ip) if any(ip in n for n in PRIVATE_NETWORKS) else None


HOST_NAME = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$")


def extra_names(directory: Path) -> tuple[list[str], list[str]]:
    """Names and addresses from ``extra-names`` (one per line, ``#`` comments): a Tailscale address, a DNS name.
    Only exactly these - the CA may sign them and nothing near them."""
    path = directory / EXTRA_NAMES
    names: list[str] = []
    ips: list[str] = []
    for line in path.read_text().splitlines() if path.exists() else []:
        value = line.split("#", 1)[0].strip().lower()
        if not value:
            continue
        try:
            ipaddress.ip_address(value)
        except ValueError:
            if HOST_NAME.match(value):
                names.append(value)
            continue
        if _private_ip(value):  # a public address never: the certificate is for this container at home
            ips.append(value)
    return names, ips


def _write(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def _pem_key(key) -> bytes:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())


def _issued_by(cert: x509.Certificate, ca: x509.Certificate) -> bool:
    """By signature, not by name: another container's CA has the same kind of name."""
    try:
        cert.verify_directly_issued_by(ca)
        return True
    except Exception:
        return False


def fingerprint(cert: x509.Certificate) -> str:
    return cert.fingerprint(hashes.SHA256()).hex(":").upper()


def _permitted(names: list[str], ips: list[str]) -> list[x509.GeneralName]:
    """Exactly this container (#92): its names and each address as a single host (/32, /128)."""
    return ([x509.DNSName(n) for n in sorted(set(names))]
            + [x509.IPAddress(ipaddress.ip_network(i)) for i in sorted(set(ips))])


def _constraints(ca: x509.Certificate) -> set:
    try:
        return set(ca.extensions.get_extension_for_class(x509.NameConstraints).value.permitted_subtrees or [])
    except x509.ExtensionNotFound:
        return set()


def _ensure_ca(directory: Path, names: list[str], ips: list[str]
               ) -> tuple[x509.Certificate, ec.EllipticCurvePrivateKey, bool]:
    ca_file, ca_key_file = directory / "ca.pem", directory / "ca-key.pem"
    permitted = _permitted(names, ips)
    if ca_file.exists() and ca_key_file.exists():
        ca = x509.load_pem_x509_certificate(ca_file.read_bytes())
        # A CA of an older version may sign the whole home network: replaced (#92), as is one for other names.
        if (ca.not_valid_after_utc - datetime.now(UTC) > timedelta(days=CA_RENEW_DAYS)
                and _constraints(ca) == set(permitted)):
            ca_key = serialization.load_pem_private_key(ca_key_file.read_bytes(), None)
            if isinstance(ca_key, ec.EllipticCurvePrivateKey):
                return ca, ca_key, False
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"{CA_NAME} ({names[0]})"),
                      x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Wealthfolio Companion")])
    now = datetime.now(UTC)
    ca = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
          .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=5))
          .not_valid_after(now + timedelta(days=CA_VALID_DAYS))
          .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
          .add_extension(x509.KeyUsage(digital_signature=False, content_commitment=False, key_encipherment=False,
                                       data_encipherment=False, key_agreement=False, key_cert_sign=True,
                                       crl_sign=True, encipher_only=False, decipher_only=False), critical=True)
          .add_extension(x509.NameConstraints(permitted_subtrees=permitted, excluded_subtrees=None), critical=True)
          .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
          .sign(key, hashes.SHA256()))
    _write(ca_key_file, _pem_key(key))
    _write(ca_file, ca.public_bytes(serialization.Encoding.PEM))
    return ca, key, True


def ensure_certificate(directory: Path, hosts: list[str], ips: list[str]) -> str:
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    cert_file, key_file = directory / "cert.pem", directory / "key.pem"
    hosts = [h.lower() for h in dict.fromkeys(h for h in hosts if h)] or ["localhost"]
    ips = sorted({p for p in (_private_ip(i) for i in [*ips, "127.0.0.1"]) if p})
    names = sorted(set(hosts) | {"localhost"})
    ca, ca_key, new_ca = _ensure_ca(directory, [hosts[0], *names], ips)
    if cert_file.exists() and key_file.exists() and not new_ca:
        cert = x509.load_pem_x509_certificate(cert_file.read_bytes())
        ours = _issued_by(cert, ca)
        if not ours and cert.issuer != cert.subject:
            return f"Keeping the certificate in {cert_file} (issued by someone else)."
        dns, addresses = _names(cert)
        fresh = cert.not_valid_after_utc - datetime.now(UTC) > timedelta(days=RENEW_DAYS)
        if ours and cert.issuer != cert.subject and fresh and set(names) <= dns and set(ips) <= addresses:
            return (f"Certificate in {cert_file} is valid until {cert.not_valid_after_utc:%Y-%m-%d}; "
                    f"CA SHA-256 {fingerprint(ca)}")
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(UTC)
    san = [x509.DNSName(h) for h in names] + [x509.IPAddress(ipaddress.ip_address(i)) for i in ips]
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hosts[0])]))
            .issuer_name(ca.subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(days=VALID_DAYS))
            .add_extension(x509.SubjectAlternativeName(san), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=False,
                                         data_encipherment=False, key_agreement=False, key_cert_sign=False,
                                         crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256()))
    _write(key_file, _pem_key(key))
    # The chain, so a browser that knows the CA can build the path from the server's answer alone.
    _write(cert_file, cert.public_bytes(serialization.Encoding.PEM) + ca.public_bytes(serialization.Encoding.PEM))
    note = " New CA - import ca.pem (page Sicherheit) once more." if new_ca else ""
    return (f"New certificate in {cert_file}, valid until {cert.not_valid_after_utc:%Y-%m-%d}; "
            f"CA SHA-256 {fingerprint(ca)}.{note}")
