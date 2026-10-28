"""The page *Durchschau* (waldonso2/wealthfolio-importer-addon#139): positions, holdings file upload, top stocks."""

from fastapi.testclient import TestClient

from brokersync.state import State

from .fake_broker import ISIN as FUND
from .fakes import FakeWealthfolio
from .helpers import first_password
from .test_sync import setup
from .test_web import csrf

STOCK = "US0378331005"
# Fabricated holdings file of the fake broker's fund: the stock at 20 %, 10 % left open.
FILE = (f"ISIN,Name,Country,Sector,Weight (%)\n{STOCK},Example Inc,United States,IT,20\n"
        "US5949181045,Other Corp,United States,IT,70\n")


def test_upload_a_holdings_file_and_see_the_stock_added_up_with_the_direct_position(tmp_path):
    from brokersync.web.app import create_app

    from .fake_broker import FakeBroker

    wf = FakeWealthfolio()
    syncer, _ = setup(tmp_path, wf)
    syncer.run()  # 5 shares of the fund (1 EUR each in the fake)
    wf.add_existing(accountId="acc-depot", activityType="BUY", date="2026-01-02T10:00:00.000Z", amount="5",
                    quantity="5", unitPrice="1", currency="EUR", assetId=STOCK, assetSymbol=STOCK, comment="direkt")
    app = create_app(tmp_path, wealthfolio=syncer._wealthfolio, adapters={"fake": FakeBroker}, run_in_thread=False)
    client = TestClient(app, base_url="http://sync.local:8090")
    first_password(client)

    page = client.get("/lookthrough").text  # reads the positions (inline in tests)
    page = client.get("/lookthrough").text
    assert FUND in page and "keine" in page
    token = csrf(client, "/lookthrough")
    r = client.post("/lookthrough/upload", data={"csrf": token, "asset_id": FUND},
                    files={"file": ("holdings.csv", FILE.encode(), "text/csv")})
    assert "2 Positionen gelesen, zusammen 90,00 % des Fonds" in r.text
    assert State(tmp_path).funds()[FUND]["constituents"][0][:3] == [STOCK, "Example Inc", "0.2"]
    # 5 direct + 20 % of 5 in the fund = 6 EUR of 10: the stock first, at 60 %, 1 EUR through the fund, 5 direct.
    first = r.text.split("Aktien über alle Positionen</caption>")[1].split("<tbody>")[1].split("</tr>")[0]
    assert "Example Inc" in first and "60,00 %" in first and "<strong>6,00</strong>" in first
    assert '">1,00</td>' in first and 'data-label="direkt">5,00</td>' in first
    assert "Nicht zugeordnet (Bargeld, Derivate, Rest der Gewichte): 5,00 %" in r.text


def test_upload_is_refused_for_an_unknown_position_and_a_file_without_weights(tmp_path):
    from brokersync.web.app import create_app

    from .fake_broker import FakeBroker

    syncer, _ = setup(tmp_path, FakeWealthfolio())
    syncer.run()
    app = create_app(tmp_path, wealthfolio=syncer._wealthfolio, adapters={"fake": FakeBroker}, run_in_thread=False)
    client = TestClient(app, base_url="http://sync.local:8090")
    first_password(client)
    client.get("/lookthrough")
    token = csrf(client, "/lookthrough")
    r = client.post("/lookthrough/upload", data={"csrf": token, "asset_id": "nope"},
                    files={"file": ("h.csv", FILE.encode(), "text/csv")})
    assert "Unbekannte Position" in r.text
    r = client.post("/lookthrough/upload", data={"csrf": token, "asset_id": FUND},
                    files={"file": ("h.csv", b"Ticker,Price\nA,1\n", "text/csv")})
    assert "Spalten für Name und Gewichtung fehlen" in r.text
    assert State(tmp_path).funds() == {}


def _client(tmp_path):
    from brokersync.web.app import create_app

    from .fake_broker import FakeBroker

    syncer, notifier = setup(tmp_path, FakeWealthfolio())
    syncer.run()
    app = create_app(tmp_path, wealthfolio=syncer._wealthfolio, adapters={"fake": FakeBroker}, run_in_thread=False)
    client = TestClient(app, base_url="http://sync.local:8090")
    first_password(client)
    client.get("/lookthrough")
    return client, csrf(client, "/lookthrough")


def test_limits_are_checked_and_saved_and_a_risk_can_be_accepted(tmp_path):
    from brokersync import config as config_mod

    client, token = _client(tmp_path)
    # One fund, nothing else: a single stock of 100 % and too few stocks.
    page = client.get("/lookthrough").text
    assert "Klumpenrisiken" in page and "So gewollt" in page and "100,00 %" in page
    r = client.post("/lookthrough/settings", data={"csrf": token, "write": "on", "stock": "abc"})
    assert "Zahl" in r.text and not config_mod.load(tmp_path).lookthrough_write
    r = client.post("/lookthrough/settings", data={"csrf": token, "write": "on", "stock": "12,5", "effective": "1"})
    c = config_mod.load(tmp_path)
    assert c.lookthrough_write and c.xray_limits == {"stock": "0.125", "effective": "1"}
    assert 'value="12,50"' in r.text
    r = client.post("/lookthrough/accept", data={"csrf": token, "key": f"stock:{FUND}"})
    assert "Keine Klumpenrisiken über deinen Grenzen." in r.text and "So gewollt (1)" in r.text
    r = client.post("/lookthrough/accept", data={"csrf": token, "key": f"stock:{FUND}", "action": "undo"})
    assert "So gewollt (1)" not in r.text


def test_an_ishares_page_is_saved_only_when_it_is_one_and_fetching_needs_no_switch(tmp_path):
    from brokersync import lookthrough

    client, token = _client(tmp_path)
    r = client.post("/lookthrough/source", data={"csrf": token, "asset_id": FUND, "url": "https://example.com/x"})
    assert "keine Produktseite von iShares" in r.text
    page = "https://www.ishares.com/de/privatanleger/de/produkte/251882/ishares-msci-world-ucits-etf-acc-fund"
    client.post("/lookthrough/source", data={"csrf": token, "asset_id": FUND, "url": page})
    assert lookthrough.status(State(tmp_path), FUND)["url"] == page
    r = client.post("/lookthrough/fetch", data={"csrf": token})  # no switch: fetching is always on
    assert "Positionslisten geholt" in r.text


def test_the_fire_page_takes_the_wealth_from_wealthfolio_and_reads_thousands(tmp_path):
    from brokersync import config as config_mod

    client, token = _client(tmp_path)
    page = client.get("/lookthrough/fire").text
    assert "FIRE-Rechner" in page and "Ergebnis" not in page
    assert "Vermögen 507,68 EUR" in page  # 5 shares at 1 EUR + 502.68 cash in the fake
    r = client.post("/lookthrough/fire", data={"csrf": token, "expenses": "30.000", "age": "4x"})
    assert "bitte als Zahl angeben" in r.text and config_mod.load(tmp_path).fire == {}
    r = client.post("/lookthrough/fire", data={"csrf": token, "age": "40"})
    assert "bitte eintragen" in r.text
    r = client.post("/lookthrough/fire", data={"csrf": token, "expenses": "30.000", "safe_rate": "3,5",
                                               "gkv": "on", "equity_fund": "on"})
    saved = config_mod.load(tmp_path).fire
    assert saved["expenses"] == "30000" and saved["safe_rate"] == "0.035" and saved["joint"] == "0"
    assert "Ergebnis" in r.text and "Lean FIRE" in r.text and "Barista FIRE (mit Teilzeit" not in r.text
    assert 'value="30.000"' in r.text and 'value="3,50"' in r.text


def test_costs_need_a_ter_and_a_fee_model_and_go_into_the_fire_calculator(tmp_path):
    from brokersync import config as config_mod

    client, token = _client(tmp_path)
    page = client.get("/lookthrough").text
    assert "Gesamtkostenquote unbekannt" in page and "nicht geschätzt" in page  # nothing guessed
    r = client.post("/lookthrough/ter", data={"csrf": token, "asset_id": FUND, "ter": "9"})
    assert "bitte als Zahl angeben" in r.text
    r = client.post("/lookthrough/costs", data={"csrf": token, "fake:fixed": "abc"})
    assert "bitte als Zahl angeben" in r.text and config_mod.load(tmp_path).cost_models == {}
    client.post("/lookthrough/costs", data={"csrf": token, "fake:fixed": "1", "fake:spread": "0,05"})
    assert config_mod.load(tmp_path).cost_models == {"fake": {"fixed": "1", "spread": "0.0005"}}
    page = "https://www.ishares.com/de/privatanleger/de/produkte/123456/example-etf"
    client.post("/lookthrough/source", data={"csrf": token, "asset_id": FUND, "url": page})  # shown: iShares'
    r = client.post("/lookthrough/ter", data={"csrf": token, "asset_id": FUND, "ter": "2,5"})
    assert 'value="2,5"' in r.text and 'value="0,05"' in r.text and "von Hand" in r.text
    assert "Gesamtkostenquote unbekannt" not in r.text and "Verkauf heute geschätzt 1,00 EUR" in r.text
    risks = r.text.split("<caption class=\"sr-only\">Klumpenrisiken</caption>")[1].split("</table>")[0]
    assert "Gesamtkostenquote" in risks  # 1 EUR fee on 5 EUR is far above 0.5 % a year
    page = client.post("/lookthrough/fire", data={"csrf": token, "expenses": "30000"}).text
    assert "Ohne Kosten" in page and "abgezogen von der Rendite" in page
    page = client.post("/lookthrough/fire", data={"csrf": token, "expenses": "30000", "costs": "0"}).text
    assert "Ohne Kosten" not in page  # overridden by the user


def test_a_funds_top_25_show_under_it_and_the_rest_folded(tmp_path):
    client, token = _client(tmp_path)
    many = "ISIN,Name,Weight (%)\n" + "".join(f",Example {n:02d} Inc,{(n + 1) / 100}\n" for n in range(60))
    client.post("/lookthrough/upload", data={"csrf": token, "asset_id": FUND},
                files={"file": ("holdings.csv", many.encode(), "text/csv")})
    page = client.get("/lookthrough").text
    fund = page.split(f"<caption class=\"sr-only\">Positionen von {FUND}")[1]
    top, rest = fund.split("Alle 60 Positionen")[:2]
    assert top.index("Example 59 Inc") < top.index("Example 58 Inc") and "Example 34 Inc" not in top  # top 25
    assert "Example 34 Inc" in rest.split("Einstellungen dieses ETFs")[0]  # folded, on the same page
    assert "<h1>X-Ray</h1>" in page and "Gesamt: Aktien über alle Positionen" in page
    assert "/lookthrough/upload" not in page and "Xtrackers" not in page  # only iShares for now (#144)
    matrix = client.get("/lookthrough?top=50").text.split("Weitere Aktien über alle Positionen")[0]
    assert '<strong aria-current="page">Top 50</strong>' in matrix and "Example 10 Inc" in matrix  # 50th of 60


def test_only_ishares_funds_are_shown_with_the_reason_a_list_is_missing(tmp_path):
    from brokersync import lookthrough

    client, token = _client(tmp_path)
    page = client.get("/lookthrough").text
    assert "0 iShares-ETFs durchleuchtet · 0 ohne Positionsliste · 1 weitere Positionen ausgeblendet" in page
    assert f"<h2>{FUND}" not in page and "Kein iShares-ETF in Wealthfolio gefunden." in page
    state = State(tmp_path)
    lookthrough.set_status(state, FUND, url="https://www.ishares.com/uk/a/en/products/123456/x-etf", status="error",
                           error="iShares: HTTP 403")
    lookthrough.record(state, fetched=0, none=0, failed=1, finder="iShares-Fondsfinder: HTTP 403")
    page = client.get("/lookthrough").text
    assert "0 iShares-ETFs durchleuchtet · 1 ohne Positionsliste · 0 weitere" in page
    assert "Abruf fehlgeschlagen" in page and "iShares: HTTP 403" in page and "Fondsfinder: HTTP 403" in page
    assert "produktliste#/?productView=all&amp;search=" in page and "<details open>" in page  # find it, paste it
    assert "0 Positionslisten geholt, 1 fehlgeschlagen" in page
    lookthrough.record(state, error="RuntimeError: kaputt")
    assert "Der Abruf ist abgebrochen: RuntimeError: kaputt" in client.get("/lookthrough").text
