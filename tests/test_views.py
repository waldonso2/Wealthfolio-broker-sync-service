"""One date/time format on every page, unknown bookings readable without JSON (#73)."""

import html
import re
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

from brokersync.state import State
from brokersync.web import views

from .helpers import first_password
from .test_web import make


def test_dates_and_times_in_german_time_and_format():
    assert views.local_time("2026-03-03T10:00:01+00:00") == "03.03.2026 11:00"  # CET
    assert views.local_time("2026-07-03T10:00:01Z", seconds=True) == "03.07.2026 12:00:01"  # CEST
    assert views.local_time("2026-07-03T10:00:01") == "03.07.2026 12:00"  # no zone: UTC, like Wealthfolio
    assert views.local_day("2026-12-31T23:30:00+00:00") == "01.01.2027"  # the German day
    assert views.local_day("2026-01-02") == "02.01.2026"
    assert views.local_day(datetime(2026, 1, 2, 12, tzinfo=UTC)) == "02.01.2026"
    assert views.utc_iso("2026-03-03T11:00:01+01:00") == "2026-03-03T10:00:01+00:00"
    assert views.local_time("") == "" and views.local_day("kein Datum") == "kein Datum"
    assert views.money("1234.5") == "1.234,50"


def test_unknown_payloads_of_every_adapter_are_described():
    tr = views.describe_unknown({"eventType": "SPINOFF", "title": "Beispiel AG", "subtitle": "Abspaltung",
                                 "status": "EXECUTED", "betrag": "-12.5 EUR"})
    assert tr == {"amount": "-12,50 EUR", "security": "", "shares": "", "text": "Beispiel AG · Abspaltung · EXECUTED"}
    sc = views.describe_unknown({"type": "SECURITY_TRANSFER", "isin": "IE00TEST0001", "quantity": "3",
                                 "betrag": "0 EUR", "hinweis": "ohne Gegenstück"})
    assert (sc["security"], sc["shares"], sc["text"]) == ("IE00TEST0001", "3", "ohne Gegenstück")
    db = views.describe_unknown({"Umsatzart": "Umtausch", "Bezeichnung": "Test Fonds", "ISIN": "DE000TEST001",
                                 "Nominal / Stück": "10", "Ausmachender Betrag": "1.234,56"})
    assert db == {"amount": "1.234,56", "security": "Test Fonds · DE000TEST001", "shares": "10", "text": ""}
    cd = views.describe_unknown({"betrag": "7.10 EUR", "buchungstext": "Ertrag Fonds"})
    assert (cd["amount"], cd["text"]) == ("7,10 EUR", "Ertrag Fonds")
    assert views.describe_unknown(None)["amount"] == ""


def test_unknown_page_shows_fields_and_raw_data_only_folded(tmp_path):
    client, _, _ = make(tmp_path)
    first_password(client)
    State(tmp_path).add_unknown("fake", "x1", "SPINOFF", "2026-03-03T23:30:00+00:00",
                                {"title": "Beispiel AG", "isin": "DE000TEST001", "betrag": "-12.5 EUR"})
    page = client.get("/unknown").text
    assert '<td data-label="Datum">04.03.2026</td>' in page  # German day
    assert '<td data-label="Betrag">-12,50 EUR</td>' in page
    assert re.search(r'<td data-label="Wertpapier">DE000TEST001', page)
    assert re.search(r"<details><summary[^>]*>Rohdaten</summary><pre>", page)
    # A pre-filled GitHub issue, opened only on click (#86): type, day and the masked payload.
    link = re.search(r'href="(https://github.com/[^"]+/issues/new\?[^"]+)"', page).group(1)
    query = parse_qs(urlparse(html.unescape(link)).query)
    assert query["title"] == ["Unbekannte Buchung: Testbroker SPINOFF"]
    assert "04.03.2026" in query["body"][0] and "DE000TEST001" in query["body"][0]


def test_traffic_light_per_broker():
    now = datetime(2026, 10, 12, 12, tzinfo=UTC)
    ok = {"status": "ok", "started_at": "2026-10-12T06:00:00+00:00", "failed": 0}
    assert views.broker_light(None, None, 0, 0, True, now) == ("grey", [("Noch nie abgerufen", "")])
    assert views.broker_light(ok, {"deviations": []}, 0, 0, True, now) == ("green", [])
    needs_auth = {**ok, "status": "needs_auth"}
    assert views.broker_light(needs_auth, None, 0, 0, True, now) == ("red", [("Anmeldung nötig", "login")])
    assert views.broker_light({**ok, "status": "error", "failed": 2}, None, 0, 0, True, now)[0] == "red"
    light, reasons = views.broker_light(ok, {"deviations": [{}, {}]}, 1, 3, True, now)
    assert light == "yellow" and reasons == [("2 Abweichungen zu Wealthfolio", "check"),
                                             ("1 unbekannte Buchung", "unknown"), ("3 fehlt in Wealthfolio", "check")]
    old = {**ok, "started_at": "2026-10-09T06:00:00+00:00"}
    assert views.broker_light(old, None, 0, 0, True, now) == ("yellow", [("Seit 3 Tagen kein Abruf", "")])
    assert views.broker_light(old, None, 0, 0, False, now) == ("green", [])  # manual only: no reminder


def test_overview_shows_three_runs_and_folds_the_rest(tmp_path):
    client, _, _ = make(tmp_path)
    first_password(client)
    state = State(tmp_path)
    for _ in range(5):
        state.finish_run(state.start_run("fake"), "ok")
    page = client.get("/").text
    head, _, folded = page.partition("Ältere Läufe (2)")
    row = '<li><span class="status ok">'
    assert folded and head.count(row) == 3 and folded.count(row) == 2
    assert "Schritt für Schritt" not in page  # no setup steps any more (#83), a checklist instead (#110)


def test_fonts_are_served_from_the_container(tmp_path):
    client, _, _ = make(tmp_path)
    for name in ("inter.woff2", "merriweather-700.woff2"):
        r = client.get(f"/static/fonts/{name}")
        assert r.status_code == 200 and r.content[:4] == b"wOF2"
    css = client.get("/static/style.css").text
    assert "/static/fonts/inter.woff2" in css and "http" not in css.split("*/", 1)[1]  # nothing from outside


def test_overview_card_has_two_buttons_and_the_reasons_link_to_their_page(tmp_path):
    from .test_safe_mode import make as make_safe

    client, _, _ = make_safe(tmp_path, safe=False)
    state = State(tmp_path)
    state.finish_run(state.start_run("fake"), "ok")
    state.add_unknown("fake", "x1", "ODD", "2026-01-02", {"title": "Beispiel"})
    state.set_reconcile("fake", [{"kind": "cash", "key": "EUR", "name": "Cash EUR", "broker": "1", "wealthfolio": "2"}])
    page = client.get("/").text
    card = page[page.index('id="broker-fake"'):]
    assert '<a href="/unknown">1 unbekannte Buchung</a>' in card
    assert '<a href="/check#broker-fake">1 Abweichung zu Wealthfolio</a>' in card
    actions = card[card.index('<div class="actions">'):card.index("</div>", card.index('<div class="actions">'))]
    assert actions.count("<button") == 1 and "Jetzt abrufen" in actions
    assert 'aria-label="Einstellungen Testbroker"' in actions and "<svg" in actions
    assert ">Unbekannte<" not in card and ">Prüfen<" not in card


def test_colour_scheme_and_language_are_chosen_on_the_page_appearance(tmp_path):
    from brokersync import config as config_mod

    from .test_safe_mode import make as make_safe

    client, _, token = make_safe(tmp_path, safe=False)
    assert "data-theme" not in client.get("/").text  # default: as the device
    r = client.post("/appearance", data={"csrf": token, "theme": "light", "language": "en"})
    assert "Saved." in r.text and '<html lang="en" data-theme="light">' in r.text  # in the new language
    assert (config_mod.load(tmp_path).ui_theme, config_mod.load(tmp_path).ui_language) == ("light", "en")
    assert 'data-theme="dark"' in client.post("/appearance", data={"csrf": token, "theme": "dark"}).text
    assert "Unbekannte Auswahl" in client.post("/appearance", data={"csrf": token, "theme": "pink"}).text


def test_the_colours_meet_wcag_aa_in_both_schemes():
    """#105: status text ≥ 4.5:1 on card and page, the traffic light's grey ≥ 3:1, and the counter badge."""
    import re
    from pathlib import Path

    css = (Path(__file__).parent.parent / "src" / "brokersync" / "web" / "static" / "style.css").read_text()
    light = dict(re.findall(r"--([a-z-]+): (#[0-9a-f]{6})", css.split("@media")[0]))
    dark = dict(re.findall(r"--([a-z-]+): (#[0-9a-f]{6})", css.split(':root[data-theme="dark"]')[1].split("}")[0]))

    def lum(h):
        c = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
        return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]

    def ratio(a, b):
        hi, lo = sorted((lum(a), lum(b)), reverse=True)
        return (hi + 0.05) / (lo + 0.05)

    for scheme in (light, dark):
        for bg in (scheme["bg"], scheme["card"]):
            for text in ("ok", "warn", "error", "muted", "text"):
                assert ratio(scheme[text], bg) >= 4.5, (text, scheme[text], bg)
            assert ratio(scheme["grey"], bg) >= 3, (scheme["grey"], bg)
        assert ratio(scheme["on-primary"], scheme["warn"]) >= 4.5  # the counter of *Unbekannt*


def test_english_pages_use_english_formats():
    """#115: with the language English, days are ISO and amounts 1,234.56; German stays as it was."""
    from brokersync.web import views

    assert views.local_day("2026-03-03T10:00:00Z", "en") == "2026-03-03"
    assert views.local_time("2026-03-03T10:00:00Z", lang="en") == "2026-03-03 11:00"
    assert views.money("1234.5", "en") == "1,234.50" and views.money("1234.5") == "1.234,50"
    assert views.local_day("2026-03-03T10:00:00Z") == "03.03.2026"
