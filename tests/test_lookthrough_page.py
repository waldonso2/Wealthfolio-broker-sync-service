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
    # 5 direct + 20 % of 5 in the fund = 6 of 10: the stock first, at 60 %, through both positions.
    first = r.text.split("<tbody>")[2].split("</tr>")[0]
    assert "Example Inc" in first and "60,00 %" in first and "10,00 %" in first and "50,00 %" in first
    assert "Nicht zugeordnet: 5,00 %" in r.text


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
