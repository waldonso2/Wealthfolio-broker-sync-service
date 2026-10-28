"""The event log on *Sicherheit* (#102) and the protection the page claims (#94)."""

from brokersync import crypto, security
from brokersync import state as state_mod
from brokersync.state import State

from .helpers import PASSWORD, first_password
from .test_web import csrf, make


def test_every_security_action_is_logged_without_a_secret(tmp_path):
    client, _, notifier = make(tmp_path)
    first_password(client)
    client.post("/logout", data={"csrf": csrf(client, "/")})
    client.post("/login", data={"password": "falsch-falsch-1"})
    client.post("/login", data={"password": PASSWORD})
    token = csrf(client, "/brokers/fake")
    client.post("/brokers/fake", data={"csrf": token, "cred_username": "max", "cred_password": "Geh3im-Broker-PIN",
                                       "cash_account_id": "", "portfolio_account_id": ""})
    client.post("/security/mode", data={"csrf": token, "mode": "safe"})
    client.post("/security/sessions", data={"csrf": token})
    new = "Moewe-Kartoffel-Jazz-71"
    client.post("/security/password", data={"csrf": csrf(client, "/security"), "password": PASSWORD, "new": new,
                                            "new2": new})
    phrase = "eine sehr lange passphrase"
    client.post("/security/passphrase", data={"csrf": csrf(client, "/security"), "password": new, "action": "set",
                                              "new": phrase, "new2": phrase})
    client.post("/security/lock", data={"csrf": csrf(client, "/security")})
    client.post("/unlock", data={"passphrase": "eine sehr lange passphrase"})
    events = State(tmp_path).events()
    seen = [(e["event"], e["ok"]) for e in reversed(events)]
    assert seen == [("setup_password", True), ("logout", True), ("login", False), ("login", True),
                    ("credentials", True), ("mode", True), ("sessions", True), ("password", True),
                    ("passphrase", True), ("lock", True), ("unlock", True)]
    assert {e["client"] for e in events} == {"testclient"}
    assert [e["broker"] for e in events if e["event"] == "credentials"] == ["fake"]
    dump = repr(events)
    for secret in (PASSWORD, new, "falsch-falsch-1", "Geh3im-Broker-PIN", "eine sehr lange passphrase"):
        assert secret not in dump
    client.post("/login", data={"password": new})
    page = client.get("/security").text
    assert "Ereignisse (12)" in page and "Zugangsdaten geändert · Test" in page


def test_a_login_from_a_new_address_is_notified(tmp_path):
    from fastapi.testclient import TestClient

    client, _, notifier = make(tmp_path)
    first_password(client)
    from brokersync import config as config_mod

    config_mod.update(tmp_path, lambda c: setattr(c, "ntfy_topic", "t"))
    client.post("/login", data={"password": PASSWORD})
    assert not notifier.sent  # the first sign-in of this address happened with the first password
    other = TestClient(client.app, base_url="http://sync.local:8090", client=("192.168.1.77", 5000))
    other.post("/login", data={"password": PASSWORD})
    assert [title for title, *_ in notifier.sent] == ["Broker Sync: neue Anmeldung"]
    assert "192.168.1.77" in notifier.sent[0][1]


def test_old_events_are_dropped(tmp_path):
    s = State(tmp_path)
    s.add_event("login", client="x")
    with s.db:
        s.db.execute("UPDATE events SET at = '2020-01-01T00:00:00+00:00'")
    s.add_event("logout", client="x")
    s.prune_events()
    assert [e["event"] for e in s.events()] == ["logout"]


def test_the_page_claims_only_the_protection_there_is(tmp_path, monkeypatch):
    client, _, _ = make(tmp_path)
    first_password(client)
    assert "alle Daten verschlüsselt" in client.get("/broker").text
    monkeypatch.setattr(state_mod, "ENCRYPTED", False)
    page = client.get("/broker").text
    assert "alle Daten verschlüsselt" not in page and "die Sync-Datenbank nicht" in page
    page = client.get("/security").text
    assert "Die Sync-Datenbank nicht" in page and "Die Sync-Datenbank mit SQLCipher" not in page
    # Key: TPM, host secret, file - and without a TPM and passphrase a hint about container backups.
    creds = tmp_path / "creds"
    creds.mkdir()
    (creds / crypto.KEY_NAME).write_bytes(b"k" * 44)
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(creds))
    for source, words in (("systemd-creds-tpm2", "TPM verschlüsselt"), ("systemd-creds", "Host-Secret"),
                          ("systemd", "nur für root lesbar")):
        monkeypatch.setenv("BROKERSYNC_KEY_SOURCE", source)
        checks = {c.label: c for c in security.status(tmp_path, secure=True)}
        assert words in checks["Schlüssel des Dienstes"].text
        assert ("Backup des Containers" in checks["Master-Passphrase"].text) == (source != "systemd-creds-tpm2")
