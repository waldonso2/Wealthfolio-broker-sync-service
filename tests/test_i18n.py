"""English as the second UI language (waldonso2/wealthfolio-importer-addon#87): every UI text has an English
entry, the English pages carry no German navigation, flash messages follow the language. German stays the
default - the other web tests check the German pages unchanged."""

import ast
import re
from pathlib import Path

from jinja2 import Environment, nodes

from brokersync import audit, security
from brokersync.web import i18n
from brokersync.web.context import SETTINGS_PAGES, STATUS_LABEL
from brokersync.web.routes.settings import THEMES

from .fake_broker import CODE
from .test_safe_mode import make as make_client

SRC = Path(__file__).resolve().parent.parent / "src" / "brokersync"
TEMPLATES = SRC / "web" / "templates"
# The Python helpers that take a UI text key: (name, position of the key among the arguments).
HELPERS = {"tr": 1, "translate": 0, "N_": 0, "Invalid": 0}


def _literals(node) -> list[str]:
    """The keys a call names: a string, or both branches of ``"a" if n == 1 else "b"``."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.IfExp):
        return _literals(node.body) + _literals(node.orelse)
    return []


def python_keys() -> dict[str, str]:
    """Every literal passed to ``tr`` / ``translate`` / ``N_`` / ``Invalid`` in the package, with where it is."""
    keys: dict[str, str] = {}
    for path in SRC.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
            if name not in HELPERS or len(node.args) <= HELPERS[name]:
                continue
            arg = node.args[HELPERS[name]]
            assert not isinstance(arg, ast.JoinedStr), f"{path.name}:{node.lineno}: an f-string is no key - use params"
            for key in _literals(arg):
                keys[key] = f"{path.name}:{node.lineno}"
    return keys


def template_keys() -> dict[str, str]:
    """Every literal passed to ``_`` in the templates (parsed by Jinja, so both quote styles count)."""
    env = Environment()
    keys: dict[str, str] = {}
    for path in TEMPLATES.glob("*.html"):
        for call in env.parse(path.read_text(encoding="utf-8")).find_all(nodes.Call):
            if isinstance(call.node, nodes.Name) and call.node.name == "_" and call.args:
                arg = call.args[0]
                for const in [arg] if isinstance(arg, nodes.Const) else [arg.expr1, arg.expr2] \
                        if isinstance(arg, nodes.CondExpr) else []:
                    if isinstance(const, nodes.Const):
                        keys[const.value] = f"{path.name}:{call.lineno}"
    return keys


def test_every_ui_text_has_an_english_entry():
    keys = {**python_keys(), **template_keys()}
    # Shown through ``_(value)``: the values of these tables are keys too.
    for value in [*STATUS_LABEL.values(), *THEMES.values(), *(t for _, title, hint in SETTINGS_PAGES
                                                                for t in (title, hint)),
                  audit.source({"comment": "[SYNC x:1]"}), audit.source({})]:
        keys.setdefault(value, "table")
    assert len(keys) > 300  # the extraction found the texts
    missing = {k: where for k, where in keys.items() if k not in i18n.EN}
    assert not missing, f"no English entry: {missing}"


def test_english_entries_keep_the_placeholders_of_their_key():
    for key, english in i18n.EN.items():
        assert set(re.findall(r"{(\w+)}", key)) == set(re.findall(r"{(\w+)}", english)), key
        assert english.strip(), key


def test_every_template_text_goes_through_the_translation():
    """No German word outside ``_()`` in the templates: strip the Jinja parts, then look for umlauts and the
    words every German sentence has."""
    german = re.compile(r"[äöüÄÖÜß]|\b(?:und|oder|der|die|das|nicht|ein|eine|mit|für|bitte)\b")
    for path in TEMPLATES.glob("*.html"):
        text = re.sub(r"{[{%#].*?[}%#]}", "", path.read_text(encoding="utf-8"), flags=re.S)
        text = re.sub(r"<[^>]+>", " ", text)  # tags and their attributes (data, CSS)
        assert not german.search(text), f"{path.name}: {german.search(text).group(0)!r} outside _()"


def test_translate_fills_the_params_after_the_lookup():
    assert i18n.translate("Gespeichert.", "de") == "Gespeichert."
    assert i18n.translate("Gespeichert.", "en") == i18n.EN["Gespeichert."]
    assert i18n.translate("Zu viele Fehlversuche. Bitte in {n} Minuten erneut versuchen.", "en", n=3) == \
        "Too many failed attempts. Please try again in 3 minutes."
    assert i18n.translate("Not a key {x}", "en", x="{n}") == "Not a key {n}"  # unknown: as it is, values not parsed
    assert i18n.translate("Gespeichert.", "fr") == "Gespeichert."  # another language: German
    # In a template the key may hold our own HTML, a value is escaped.
    assert str(i18n.markup("{n} neu", "en", n="<b>")) == "&lt;b&gt; new"


def _english(tmp_path):
    client, wf, token = make_client(tmp_path, safe=False)
    client.post("/brokers/fake/login/start", data={"csrf": token})
    client.post("/brokers/fake/login", data={"csrf": token, "code": CODE})
    assert "Abruf beendet." in client.post("/run", data={"csrf": token}).text  # something on the overview
    r = client.post("/appearance", data={"csrf": token, "theme": "system", "language": "en"})
    assert '<html lang="en"' in r.text
    return client, token


def test_the_english_pages_have_no_german_navigation(tmp_path):
    client, _ = _english(tmp_path)
    for path, heading in [("/", "Overview"), ("/check", "Check"), ("/security", "Security"),
                          ("/appearance", "Appearance"), ("/settings", "Settings"), ("/unknown", "Unknown bookings")]:
        page = client.get(path).text
        for word in ("Übersicht", "Einstellungen", "Prüfung", "Speichern", "Abruf"):
            assert word not in page, f"{path}: {word}"
        assert "<h1" in page and heading in page, path
        assert ">Overview</span>" in page and ">Check</span>" in page and ">Settings</span>" in page
        assert ">Unknown</span>" in page
    overview = client.get("/").text
    assert "Fetch all" in overview and "Fetch now" in overview and "Last runs" in overview
    assert "Balance <strong" in overview
    security_page = client.get("/security").text
    assert "Key of the service" in security_page and "Fetch mode" in security_page
    assert "Light" in client.get("/appearance").text and "Deutsch" in client.get("/appearance").text


def test_flash_messages_follow_the_language(tmp_path):
    client, token = _english(tmp_path)
    r = client.post("/securities", data={"csrf": token, "isin": "DE0000000000", "symbol": "X"})
    assert "Not a valid ISIN" in r.text and "Keine gültige ISIN" not in r.text
    r = client.post("/run", data={"csrf": token})
    assert "Fetch finished." in r.text
    r = client.post("/setup/wealthfolio", data={"csrf": token, "url": "ftp://wf.local"})
    assert "Wealthfolio address: please give an address with http:// or https://." in r.text
    # Back to German: the confirmation is already in the language just chosen.
    r = client.post("/appearance", data={"csrf": token, "theme": "system", "language": "de"})
    assert "Gespeichert." in r.text and ">Übersicht</span>" in r.text
    assert "Gespeichert." in client.post("/securities", data={"csrf": token, "isin": "DE0007164600",
                                                                "symbol": "SAP.DE"}).text


def test_security_checks_are_keys_with_params(tmp_path):
    checks = security.status(tmp_path, secure=False)
    assert all(c.label in i18n.EN for c in checks)
    assert all(c.detail in i18n.EN or c.detail == "{files}" for c in checks)
    connection = next(c for c in checks if c.label == "Verbindung zur Oberfläche")
    assert connection.text.startswith("Unverschlüsseltes HTTP")
