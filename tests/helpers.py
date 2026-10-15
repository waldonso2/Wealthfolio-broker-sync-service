"""Shared steps of the web tests."""

import re

from brokersync.vault import Vault, new_setup_code

PASSWORD = "Kiefer-Ozelot-9182"


def first_password(client, password: str = PASSWORD, **kwargs):
    """Set the first UI password the way a user does: the one-time code from the console (#89), the CSRF
    token from the page."""
    code = new_setup_code(Vault(client.app.data_dir))
    page = client.get("/setup-password").text
    token = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    return client.post("/setup-password", data={"csrf": token, "code": code, "password": password,
                                                "password2": password}, **kwargs)
