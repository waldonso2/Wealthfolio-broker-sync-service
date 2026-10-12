"""One date/time format on every page, unknown bookings readable without JSON (#73)."""

import re
from datetime import UTC, datetime

from brokersync.state import State
from brokersync.web import views

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
    client.post("/setup-password", data={"password": "geheim-123456", "password2": "geheim-123456"})
    State(tmp_path).add_unknown("fake", "x1", "SPINOFF", "2026-03-03T23:30:00+00:00",
                                {"title": "Beispiel AG", "isin": "DE000TEST001", "betrag": "-12.5 EUR"})
    page = client.get("/unknown").text
    assert '<td data-label="Datum">04.03.2026</td>' in page  # German day
    assert '<td data-label="Betrag">-12,50 EUR</td>' in page
    assert re.search(r'<td data-label="Wertpapier">DE000TEST001', page)
    assert re.search(r"<details><summary[^>]*>Rohdaten für einen Fehlerbericht</summary><pre>", page)
