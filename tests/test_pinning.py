"""Wealthfolio with a certificate of its own, trusted by its fingerprint (#95), and "Nur testen"."""

import hashlib
import json
import ssl
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from brokersync import config as config_mod
from brokersync.vault import Vault
from brokersync.wealthfolio import WealthfolioClient, WealthfolioError, normalize_fingerprint, server_fingerprint

from .helpers import first_password
from .test_web import csrf, make


def _self_signed(tmp_path, name):
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.now(UTC)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
            .serial_number(1).not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=1))
            .sign(key, hashes.SHA256()))
    cert_file, key_file = tmp_path / f"{name}.pem", tmp_path / f"{name}-key.pem"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    return cert_file, key_file, hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()


@pytest.fixture
def wealthfolio_tls(tmp_path):
    """A Wealthfolio stand-in on HTTPS with a self-signed certificate; records whether a request arrived."""
    cert, key, fingerprint = _self_signed(tmp_path, "wealthfolio")
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            seen.append(self.path)
            body = json.dumps([{"id": "a", "name": "Cash", "currency": "EUR", "accountType": "CASH",
                                "isActive": True}]).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cert, key)
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"https://127.0.0.1:{server.server_address[1]}", fingerprint, seen
    server.shutdown()


def test_a_pinned_certificate_is_trusted_and_another_one_refused(wealthfolio_tls):
    url, fingerprint, seen = wealthfolio_tls
    with pytest.raises(WealthfolioError):  # unknown issuer: refused as before
        WealthfolioClient(url, None).list_accounts()
    pin = ":".join(fingerprint[i:i + 2] for i in range(0, 64, 2)).upper()  # as a browser shows it
    with WealthfolioClient(url, None, fingerprint=pin) as wf:
        assert [a.name for a in wf.list_accounts()] == ["Cash"]
    seen.clear()
    with pytest.raises(WealthfolioError, match="(?i)certificate"):
        WealthfolioClient(url, "secret", fingerprint="0" * 64).list_accounts()
    assert seen == []  # refused in the handshake: not even the login with the password went out
    assert server_fingerprint(url).lower() == fingerprint
    assert normalize_fingerprint("ab" * 31) == "" and normalize_fingerprint(pin) == fingerprint


def test_only_testing_saves_nothing(tmp_path):
    client, _, _ = make(tmp_path)
    first_password(client)
    before = (config_mod.load(tmp_path), Vault(tmp_path).load())
    token = csrf(client, "/setup/wealthfolio")
    r = client.post("/setup/wealthfolio", data={"csrf": token, "url": "http://wf.local:8080/", "password": "nope",
                                                "test": "1"})
    assert r.status_code == 422 and "Verbindung fehlgeschlagen" in r.text and 'value="http://wf.local:8080"' in r.text
    from .fakes import PASSWORD

    r = client.post("/setup/wealthfolio", data={"csrf": token, "url": "http://wf.local:8080/", "password": PASSWORD,
                                                "test": "1"})
    assert "Test erfolgreich - 2 Konten gefunden" in r.text
    assert (config_mod.load(tmp_path), Vault(tmp_path).load()) == before
    assert "unverschlüsselt durchs Netz" in r.text  # http://, not local: a warning at the field


def test_the_page_offers_the_fingerprint_of_a_certificate_nobody_vouches_for(tmp_path, wealthfolio_tls):
    from fastapi.testclient import TestClient

    from brokersync.web.app import create_app

    url, fingerprint, _ = wealthfolio_tls
    client = TestClient(create_app(tmp_path, run_in_thread=False), base_url="http://sync.local:8090")
    first_password(client)
    token = csrf(client, "/setup/wealthfolio")
    r = client.post("/setup/wealthfolio", data={"csrf": token, "url": url, "no_password": "1"})
    assert r.status_code == 422 and fingerprint.upper() in r.text and config_mod.load(tmp_path).wealthfolio_url != url
    r = client.post("/setup/wealthfolio", data={"csrf": token, "url": url, "no_password": "1",
                                                "fingerprint": fingerprint.upper()})
    assert "Verbunden - 1 Konten" in r.text
    assert config_mod.load(tmp_path).wealthfolio_fingerprint == fingerprint
