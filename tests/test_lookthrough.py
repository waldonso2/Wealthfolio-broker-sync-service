from datetime import date
from decimal import Decimal as D

import pytest

from brokersync.lookthrough import parse

# Fabricated files in the issuers' layouts (no real fund data).
ISHARES = """﻿iShares Example UCITS ETF
Fund Holdings as of,"01.Oct.2026"
\xa0
Ticker,Name,Sector,Asset Class,Market Value,Weight (%),Location,Exchange,Currency,Market Currency
"AAA","EXAMPLE ONE INC","Information Technology","Equity","1,000,000.00","4.50","United States","NASDAQ","USD","USD"
"BBB","EXAMPLE TWO AG","Industrials","Equity","500,000.00","2.25","Germany","Xetra","EUR","EUR"
"USD","USD CASH","Cash and/or Derivatives","Cash","10,000.00","0.01","United States","-","USD","USD"
\xa0
"The content contained herein is owned or licensed by BlackRock."
""".encode()

GERMAN = """ISIN;Bezeichnung;Land;Branche;Gewichtung
US0378331005;Example Three Inc.;USA;IT;1,50
XX0000000000;Kein echtes ISIN;Japan;Industrie;0,5
;Leer;;;
""".encode("cp1252")


def test_ishares_file_with_fund_details_above_and_text_below():
    lines = parse(ISHARES)
    assert [c.key for c in lines] == ["AAA:NASDAQ", "BBB:XETRA", "USD:-"]
    assert lines[0].weight == D("0.045")
    assert (lines[1].name, lines[1].country, lines[1].sector, lines[1].currency) == (
        "EXAMPLE TWO AG", "Germany", "Industrials", "EUR")


def test_german_list_with_isin_semicolons_and_decimal_comma():
    lines = parse(GERMAN)
    assert lines[0].key == "US0378331005"
    assert lines[0].weight == D("0.015")
    assert lines[1].key == "KEIN ECHTES ISIN"  # check digit wrong: falls back to the name
    assert len(lines) == 2


def test_weights_already_given_as_shares_are_kept():
    lines = parse(b"Name,Weight\nA,0.6\nB,0.4\n")
    assert [c.weight for c in lines] == [D("0.6"), D("0.4")]


def test_thousands_and_decimal_marks():
    lines = parse(b'Name;Gewichtung (%)\nA;"1.234,5"\nB;"1,234.5"\n')
    assert [c.weight for c in lines] == [D("12.345"), D("12.345")]


@pytest.mark.parametrize("data", [b"Ticker,Price\nA,1\n", b"Name,Weight\nA,\n", b"\xff\xfe\x00"])
def test_not_a_holdings_list(data):
    with pytest.raises(ValueError):
        parse(data)


# ── fetching ─────────────────────────────────────────────────────────────────
import io  # noqa: E402
import json  # noqa: E402
import zipfile  # noqa: E402

import httpx  # noqa: E402

from brokersync import config as config_mod  # noqa: E402
from brokersync import costs, lookthrough, meta_keys  # noqa: E402
from brokersync.lookthrough import Depot, NoSource, SourceError, Sources, fetch, parse_file  # noqa: E402
from brokersync.state import State  # noqa: E402

NS = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'


def xlsx(rows: list[list[str]]) -> bytes:
    """A minimal XLSX like the Xtrackers export: text as shared strings, numbers inline."""
    shared: list[str] = []
    cells = []
    for r, row in enumerate(rows, 1):
        out = []
        for c, value in enumerate(row):
            ref = f"{chr(65 + c)}{r}"
            if value.replace(".", "", 1).isdigit():
                out.append(f'<c r="{ref}"><v>{value}</v></c>')
            else:
                shared.append(value)
                out.append(f'<c r="{ref}" t="s"><v>{len(shared) - 1}</v></c>')
        cells.append(f'<row r="{r}">{"".join(out)}</row>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/sharedStrings.xml", f"<sst {NS}>" + "".join(f"<si><t>{s}</t></si>" for s in shared) + "</sst>")
        z.writestr("xl/worksheets/sheet1.xml", f"<worksheet {NS}><sheetData>{''.join(cells)}</sheetData></worksheet>")
    return buf.getvalue()


# Fabricated, in the layout of an Xtrackers constituent export (title rows above the header).
XTRACKERS = xlsx([["Xtrackers Example UCITS ETF"], ["As of 01/10/2026"], [],
                  ["ISIN", "Name", "Country", "Industry Classification", "Weighting", "Type of Security"]]
                 + [[isin, f"EXAMPLE {n} INC", "United States", "Information Technology", "0.19", "Equity"]
                    for n, isin in enumerate(["US0000000002", "US0000000010", "US0000000028", "US0000000036",
                                              "US0000000044"])])
ISHARES_ROWS = "".join(f'"T{n}","EXAMPLE {n} INC","IT","Equity","1","19.00","United States","NASDAQ","USD","USD"\n'
                       for n in range(5))
ISHARES_FULL = ISHARES.split(b'"AAA"')[0] + ISHARES_ROWS.encode()


def test_xlsx_rows_turn_into_holdings():
    lines = parse_file(XTRACKERS)
    assert len(lines) == 5 and lines[0].key == "US0000000002" and lines[0].weight == D("0.19")  # a share



def test_an_uploaded_ubs_list_is_read():
    # fabricated, in the layout of UBS' "Constituents" download: fund name and ISIN above the header
    doc = xlsx([["UBS Example UCITS ETF"], ["ISIN: : LU0000000000"],
                ["Wertpapiere", "ISIN", "Sedol-Code", "Währung", "Kurs", "Gewichtung (%)"]]
               + [[f"EXAMPLE {n} CORP", i, "0000000", "USD", "1'430.00", "19.9"] for n, i in enumerate(FIVE)])
    lines = parse_file(doc)
    assert len(lines) == 5 and lines[0].name == "EXAMPLE 0 CORP" and lines[0].weight == D("0.199")
    with pytest.raises(SourceError, match="XLSX oder CSV"):
        parse_file(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + bytes(504))

# Fabricated, in the layout of iShares' fund finder data (product-screener-v3.1.jsn).
SCREENER = json.dumps({
    "251882": {"portfolioId": 251882, "isin": "IE00B4L5Y983", "fundName": "Example World",
               "productPageUrl": "/uk/individual/en/products/251882/ishares-msci-world-ucits-etf-acc-fund"},
    "999999": {"portfolioId": 999999, "isin": "IE0000000017", "fundName": "Example Other", "ter": {"r": 0.07},
               "productPageUrl": "/uk/individual/en/products/999999/example-other-fund"},
    "broken": {"isin": None}, "list": [1, 2]}).encode()


def sources(handler) -> Sources:
    def answer(request: httpx.Request) -> httpx.Response:
        if "product-screener" in request.url.path:
            return httpx.Response(200, content=SCREENER)
        return handler(request)

    return Sources(transport=httpx.MockTransport(answer))


def test_the_fund_finder_gives_every_ishares_fund_and_its_outage_is_retried_soon():
    asked = []

    def answer(request: httpx.Request) -> httpx.Response:
        asked.append(str(request.url))
        if "product-screener" in request.url.path:
            return httpx.Response(200, content=SCREENER)
        issuer = request.url.host in ("www.ishares.com", "www.blackrock.com")
        return httpx.Response(200, content=ISHARES_FULL) if issuer else httpx.Response(404)

    s = Sources(transport=httpx.MockTransport(answer))
    lines, source = fetch(s, "IE0000000017")  # in no built-in list: found by the fund finder
    assert source == "iShares" and "portfolioId=999999&" in asked[-1]
    fetch(s, "IE00B4L5Y983")
    assert sum("product-screener" in a for a in asked) == 1  # read once per pass
    down = Sources(transport=httpx.MockTransport(lambda r: httpx.Response(503) if "product-screener" in r.url.path
                                                 else answer(r)))
    assert fetch(down, "IE00B4L5Y983")[1] == "iShares"  # a confirmed id while the finder is down
    assert "HTTP 503" in down.finder_error


def search(hits) -> Sources:
    """The fund finder gone (404, as in 10/2026), the site's search answering ``hits``."""
    def answer(request: httpx.Request) -> httpx.Response:
        if "product-screener" in request.url.path:
            return httpx.Response(404)
        if "autoComplete" in request.url.path:
            return httpx.Response(200, content=hits) if isinstance(hits, bytes) else httpx.Response(200, json=hits)
        return httpx.Response(200, content=ISHARES_FULL)

    return Sources(transport=httpx.MockTransport(answer))


def test_without_the_fund_finder_the_sites_search_finds_a_fund_but_only_an_unambiguous_one():
    hit = {"category": "productAutocomplete", "id": "/uk/individual/en/products/999999/example-other-fund",
           "label": "Example Other"}
    s = search([hit, {"category": "pageAutocomplete", "id": "/uk/x"}])
    assert fetch(s, "IE0000000017")[1] == "iShares" and s.finder_error == "iShares-Fondsfinder: HTTP 404"
    assert search([{"category": "productAutocomplete", "id": "251882"}]).ishares_product("X") == ("251882",
                                                                                                    "holdings")
    with pytest.raises(NoSource):  # two funds for one ISIN: never a guess
        fetch(search([hit, {"category": "productAutocomplete", "id": "123456"}]), "IE0000000017")
    with pytest.raises(NoSource):
        fetch(search([]), "US0378331005")  # a stock
    with pytest.raises(SourceError, match="iShares-Suche"):
        fetch(search(b"<html>splash</html>"), "IE0000000017")


def test_fetch_asks_ishares_by_its_product_id_and_nothing_else_for_now():
    asked = []

    def answer(request: httpx.Request) -> httpx.Response:
        asked.append(str(request.url))
        assert "Mozilla" in request.headers["user-agent"]
        if request.url.host == "www.blackrock.com":
            return httpx.Response(200, content=ISHARES_FULL)
        if "IE00BJ0KDQ92" in request.url.path:
            return httpx.Response(200, content=XTRACKERS)
        return httpx.Response(404)

    s = sources(answer)
    lines, source = fetch(s, "IE00B4L5Y983")
    assert source == "iShares" and len(lines) == 5 and "portfolioId=251882&" in asked[0]
    with pytest.raises(NoSource):
        fetch(s, "IE00BJ0KDQ92")  # Xtrackers: hidden for now (#144), DWS is not asked
    assert not any("dws" in url for url in asked)
    lines, _ = fetch(s, "IE00XXXXXXXX", "https://www.ishares.com/de/privatanleger/de/produkte/123456/example-etf")
    assert "portfolioId=123456&" in asked[-1]
    with pytest.raises(NoSource):
        fetch(s, "US0378331005")  # a stock: no source has it


def test_an_html_page_or_an_incomplete_list_is_no_holdings_list():
    s = sources(lambda r: httpx.Response(200, content=b"<!DOCTYPE html><html>moved</html>"))
    with pytest.raises(SourceError, match="keine Positionsliste"):
        fetch(s, "IE00B4L5Y983")
    s = sources(lambda r: httpx.Response(200, content=ISHARES))  # 3 lines, 6.8 %
    with pytest.raises(SourceError, match="unvollständig"):
        fetch(s, "IE00B4L5Y983")


def test_only_the_sources_are_asked_and_answers_are_capped(monkeypatch):
    s = sources(lambda r: httpx.Response(302, headers={"Location": "https://evil.example/x"}))
    with pytest.raises(SourceError, match="nicht erreichbar"):
        fetch(s, "IE00B4L5Y983")
    monkeypatch.setattr(lookthrough, "MAX_BYTES", 100)
    s = sources(lambda r: httpx.Response(200, content=ISHARES_FULL))
    with pytest.raises(SourceError, match="größer"):
        fetch(s, "IE00B4L5Y983")
    assert lookthrough.ishares_page("https://www.ishares.com.evil.example/uk/a/en/products/1234/x") is None
    assert lookthrough.ishares_page("http://www.ishares.com/uk/individual/en/products/1234/x") is None


def test_update_fetches_what_is_due_keeps_uploads_and_remembers_funds_without_source(tmp_path):
    state = State(tmp_path)
    depot = Depot(positions=[
        {"id": "a-world", "isin": "IE00B4L5Y983", "name": "World", "currency": "EUR", "value": D(100)},
        {"id": "a-stock", "isin": "US0378331005", "name": "Stock", "currency": "USD", "value": D(50)},
        {"id": "a-own", "isin": "IE00BJ0KDQ92", "name": "Own", "currency": "EUR", "value": D(10)}])
    lookthrough.set_status(state, "a-own", status="upload")
    calls = []

    def answer(request):
        calls.append(request.url.host)
        ishares = request.url.host == "www.blackrock.com"
        return httpx.Response(200, content=ISHARES_FULL) if ishares else httpx.Response(404)

    r = lookthrough.update(state, depot, sources(answer))
    assert (r.fetched, r.none, r.errors) == (1, 1, [])
    assert state.funds()["a-world"]["name"] == "World" and "a-own" not in state.funds()
    assert lookthrough.status(state, "a-stock")["status"] == "none"
    calls.clear()
    assert lookthrough.update(state, depot, sources(answer)).fetched == 0 and calls == []  # nothing due
    assert lookthrough.update(state, depot, sources(answer), force=True).fetched == 1


def test_a_cluster_risk_is_notified_once_and_never_when_accepted(tmp_path):
    from brokersync.xray import Finding

    state = State(tmp_path)
    one, two = Finding("stock:A", "stock", "A", D("0.2"), D("0.05")), Finding("country:US", "country", "US",
                                                                            D("0.7"), D("0.6"))
    assert lookthrough.new_risks(state, [one]) == [one]
    assert lookthrough.new_risks(state, [one]) == []
    state.set_meta(meta_keys.XRAY_ACCEPTED(), json.dumps(["country:US"]))
    assert lookthrough.new_risks(state, [one, two]) == []
    assert lookthrough.new_risks(state, []) == [] and lookthrough.new_risks(state, [one]) == [one]  # back again


def test_after_a_run_the_funds_are_fetched_and_new_risks_notified(tmp_path):
    from .fakes import FakeWealthfolio
    from .test_sync import setup

    wf = FakeWealthfolio()
    syncer, notifier = setup(tmp_path, wf)
    syncer.run()
    assert not any(t == "Klumpenrisiko" for t, _, _ in notifier.sent)  # no list yet: no false alarm
    State(tmp_path).delete_meta(meta_keys.FUND("IE00B4L5Y983"))  # iShares had no list: due again
    syncer._fund_sources = lambda: sources(lambda r: httpx.Response(200, content=ISHARES_FULL))
    syncer.run()
    assert len(State(tmp_path).funds()["IE00B4L5Y983"]["constituents"]) == 5
    risks = [m for t, m, link in notifier.sent if t == "Klumpenrisiko"]
    assert len(risks) == 1 and "neue Klumpenrisiken" in risks[0]
    syncer.run()
    assert len([t for t, _, _ in notifier.sent if t == "Klumpenrisiko"]) == 1  # told once


# ── into Wealthfolio's taxonomies ────────────────────────────────────────────
def _wf():
    from brokersync.wealthfolio import WealthfolioClient

    from .fakes import PASSWORD, FakeWealthfolio

    fake = FakeWealthfolio()
    return fake, WealthfolioClient("http://wf.local:8080", PASSWORD, transport=fake.transport())


FUND_LINES = [["US0000000002", "A", "0.5", "USA", "Information Technology", "USD"],
              ["DE0000000001", "B", "0.3", "Germany", "Industrials", "EUR"],
              ["XX0000000001", "C", "0.15", "Narnia", "Software & Services", "EUR"]]


def test_countries_and_sectors_go_into_wealthfolio_without_touching_the_users_own(tmp_path):
    state = State(tmp_path)
    state.set_fund("a-world", "World", FUND_LINES)
    depot = Depot(positions=[{"id": "a-world", "isin": "", "name": "World", "currency": "EUR", "value": D(1)},
                             {"id": "a-stock", "isin": "", "name": "Stock", "currency": "EUR", "value": D(1)}])
    fake, wf = _wf()
    fake.assignments.append({"id": "y1", "assetId": "a-world", "taxonomyId": "regions", "categoryId": "country_DE",
                             "weight": 10000, "source": "AUTO"})  # Yahoo: only the domicile
    r = lookthrough.write_taxonomies(state, wf, depot)
    assert (r.written, r.manual) == (2, 0) and r.unmatched == ["Narnia"]
    mine = sorted((a["taxonomyId"], a["categoryId"], a["weight"]) for a in fake.assignments)
    # A subcategory (Software & Services) counts for its sector; Narnia has no country: it stays unassigned.
    assert mine == [("industries_gics", "20", 3000), ("industries_gics", "45", 6500),
                    ("regions", "country_DE", 3000), ("regions", "country_US", 5000)]
    assert all(a["source"] == "BROKERSYNC" for a in fake.assignments)
    assert lookthrough.write_taxonomies(state, wf, depot).written == 0  # unchanged: not written again
    # The user sets the sector by hand: from now on it is left alone.
    fake.assignments = [a for a in fake.assignments if a["taxonomyId"] != "industries_gics"]
    fake.assignments.append({"id": "u1", "assetId": "a-world", "taxonomyId": "industries_gics", "categoryId": "45",
                             "weight": 10000, "source": "MANUAL"})
    state.set_fund("a-world", "World", [FUND_LINES[0], [*FUND_LINES[1][:2], "0.4", *FUND_LINES[1][3:]]])
    r = lookthrough.write_taxonomies(state, wf, depot)
    assert (r.written, r.manual) == (1, 1)
    fake.assets = [{"id": "a-world"}, {"id": "a-stock"}]
    assert lookthrough.remove_taxonomies(wf) == 2
    assert [a["source"] for a in fake.assignments] == ["MANUAL"]


def test_german_country_and_sector_names_and_iso_codes_find_wealthfolios_categories(tmp_path):
    state = State(tmp_path)
    state.set_fund("a-world", "World", [["US0000000002", "A", "0.4", "Vereinigte Staaten von Amerika",
                                         "Informationstechnologie", "USD"],
                                        ["US0000000010", "B", "0.3", "US", "IT", "USD"],  # Vanguard: ISO codes
                                        ["DE0000000001", "C", "0.2", "Deutschland", "Industrie", "EUR"],
                                        ["IT0000000001", "D", "0.1", "IT", "", "EUR"]])  # Italy, no Wealthfolio
    depot = Depot(positions=[{"id": "a-world", "isin": "", "name": "World", "currency": "EUR", "value": D(1)}])
    fake, wf = _wf()
    r = lookthrough.write_taxonomies(state, wf, depot)
    mine = sorted((a["taxonomyId"], a["categoryId"], a["weight"]) for a in fake.assignments)
    assert ("regions", "country_US", 7000) in mine and ("regions", "country_DE", 2000) in mine
    assert ("industries_gics", "45", 7000) in mine and ("industries_gics", "20", 2000) in mine
    assert r.unmatched == ["IT"]  # Italy is no category of this fake: unassigned, never "Information Technology"


def test_a_run_writes_the_taxonomies_when_turned_on(tmp_path):
    from .fakes import FakeWealthfolio
    from .test_sync import setup

    wf = FakeWealthfolio()
    syncer, _ = setup(tmp_path, wf)
    syncer.run()
    State(tmp_path).set_fund("IE00B4L5Y983", "World", FUND_LINES)
    wf.assets = [{"id": "IE00B4L5Y983"}]
    syncer.run()
    assert wf.assignments == []  # off by default
    config_mod.update(tmp_path, lambda c: setattr(c, "lookthrough_write", True))
    syncer.run()
    assert {a["categoryId"] for a in wf.assignments} == {"country_US", "country_DE", "45", "20"}
    assert syncer.remove_taxonomies() == 4 and wf.assignments == []


def test_the_fund_finder_gives_the_ter_and_a_fetch_keeps_it_with_its_source(tmp_path):
    assert lookthrough._ter({"fees": "0,20 %"}) == D("0.002")
    assert lookthrough._ter({"ter": "n/a", "netExpenseRatio": 50}) is None  # not a plausible percentage
    state = State(tmp_path)
    depot = Depot(positions=[{"id": "a", "isin": "IE0000000017", "name": "Other", "currency": "EUR",
                              "value": D(100)}])
    lookthrough.update(state, depot, sources(lambda r: httpx.Response(200, content=ISHARES_FULL)))
    st = lookthrough.status(state, "a")
    assert lookthrough.ter(st) == (D("0.0007"), "iShares") and st["ter_at"]
    lookthrough.set_status(state, "a", ter_manual="0.001")
    assert lookthrough.ter(lookthrough.status(state, "a")) == (D("0.001"), "manual")  # the user's first


def test_the_depot_reads_each_positions_trades_from_wealthfolio(tmp_path):
    fake, wf = _wf()
    fake.add_existing(accountId="acc-depot", activityType="BUY", date="2026-01-02T10:00:00Z", quantity="5",
                      unitPrice="1", fee="1", tax="0", amount="6", currency="EUR", assetId="IE00B4L5Y983")
    fake.add_existing(accountId="acc-depot", activityType="DEPOSIT", date="2026-01-02T10:00:00Z", amount="6",
                      currency="EUR")
    depot = lookthrough.read_depot(wf)
    [p] = depot.positions
    assert list(p["parts"]) == ["acc-depot"]
    assert p["trades"] == [costs.Trade(date(2026, 1, 2), "BUY", D(5), D(1), D(0), "acc-depot")]


def test_weights_above_100_percent_are_scaled_down_and_a_refused_fund_stops_no_other(tmp_path):
    state = State(tmp_path)
    # A negative cash line leaves the stocks at 110 %: Wealthfolio refuses more than 10000 basis points.
    state.set_fund("a-1", "One", [["US0000000002", "A", "0.6", "USA", "Industrials", "USD"],
                                  ["DE0000000001", "B", "0.5", "Germany", "Industrials", "EUR"],
                                  ["", "Cash", "-0.1", "", "", "EUR"]])
    state.set_fund("a-2", "Two", FUND_LINES)
    depot = Depot(positions=[{"id": "a-1", "isin": "", "name": "One", "currency": "EUR", "value": D(2)},
                             {"id": "a-2", "isin": "", "name": "Two", "currency": "EUR", "value": D(1)}])
    fake, wf = _wf()
    assert lookthrough.write_taxonomies(state, wf, depot).written == 4
    one = {(a["taxonomyId"], a["categoryId"]): a["weight"] for a in fake.assignments if a["assetId"] == "a-1"}
    assert one == {("regions", "country_US"): 5454, ("regions", "country_DE"): 4545, ("industries_gics", "20"): 10000}
    fake.assignments = []
    real = wf.replace_assignments

    def refuse_one(asset_id, *args):
        if asset_id == "a-1":
            raise lookthrough.WealthfolioError("refused")
        real(asset_id, *args)

    wf.replace_assignments = refuse_one  # type: ignore[method-assign]
    assert lookthrough.write_taxonomies(state, wf, depot).written == 2
    assert {a["assetId"] for a in fake.assignments} == {"a-2"}


def test_our_assignments_go_when_the_list_no_longer_names_them_and_remove_finds_sold_funds(tmp_path):
    state = State(tmp_path)
    state.set_fund("a-world", "World", FUND_LINES)
    depot = Depot(positions=[{"id": "a-world", "isin": "", "name": "World", "currency": "EUR", "value": D(1)}])
    fake, wf = _wf()
    lookthrough.write_taxonomies(state, wf, depot)
    state.set_fund("a-world", "World", [[k, n, w, "", s, c] for k, n, w, _, s, c in FUND_LINES])  # no countries
    lookthrough.write_taxonomies(state, wf, depot)
    assert {a["taxonomyId"] for a in fake.assignments} == {"industries_gics"}
    fake.assets = [{"id": "a-world"}]  # sold since: no position any more, our assignments still go
    assert lookthrough.remove_taxonomies(wf) == 2 and fake.assignments == []


def test_an_xlsx_cell_beyond_the_last_column_is_ignored():
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as z:
        z.writestr("xl/worksheets/sheet1.xml", f'<worksheet {NS}><sheetData><row r="1"><c r="A1" t="inlineStr">'
                   '<is><t>Name</t></is></c><c r="ZZZZZZZZZZZZ1"><v>1</v></c></row></sheetData></worksheet>')
    assert lookthrough.xlsx_rows(data.getvalue()) == [["Name"]]


def test_a_pass_stops_when_its_time_is_up(tmp_path):
    depot = Depot(positions=[{"id": "a", "isin": "IE00B4L5Y983", "name": "World", "currency": "EUR",
                              "value": D(1)}])
    asked: list[str] = []
    result = lookthrough.update(State(tmp_path), depot, sources(lambda r: asked.append(str(r.url))), budget=-1)
    assert (result.fetched, result.none, asked) == (0, 0, [])


def test_one_position_can_be_fetched_alone_and_lists_are_due_again_the_next_day(tmp_path):
    depot = Depot(positions=[{"id": i, "isin": i, "name": i, "currency": "EUR", "value": D(1)}
                             for i in ("IE00B4L5Y983", "IE0000000017")])
    asked: list[str] = []

    def answer(request):
        asked.append(str(request.url))
        return httpx.Response(200, content=ISHARES_FULL)

    state = State(tmp_path)
    assert lookthrough.update(state, depot, sources(answer), force=True, only="IE0000000017").fetched == 1
    lists = [a for a in asked if "finanzfluss" not in a]  # and its Finanzfluss page, once a month
    assert len(lists) == 1 and "999999" in lists[0]  # only that fund's list was asked for
    assert lookthrough.REFRESH_DAYS < 1  # the daily timer finds yesterday's list due


def test_safe_mode_still_refreshes_the_lists_but_contacts_no_broker(tmp_path):
    from .fakes import FakeWealthfolio
    from .test_sync import setup

    wf = FakeWealthfolio()
    syncer, _ = setup(tmp_path, wf)
    syncer._fund_sources = lambda: sources(lambda r: httpx.Response(404))
    syncer.run()
    State(tmp_path).delete_meta(meta_keys.FUND("IE00B4L5Y983"))  # due again
    config_mod.update(tmp_path, lambda c: setattr(c, "sync_mode", "safe"))
    syncer._fund_sources = lambda: sources(lambda r: httpx.Response(200, content=ISHARES_FULL))
    created = len(wf.created)
    assert syncer.run() == []  # no broker in safe mode
    assert len(wf.created) == created and "IE00B4L5Y983" in State(tmp_path).funds()


def test_a_fund_finder_answer_without_products_is_an_error_with_its_reason():
    for content, reason in [(b"<html>splash</html>", "kein JSON"), (json.dumps({"a": {"x": 1}}).encode(),
                                                                    "ohne Produkte")]:
        s = Sources(transport=httpx.MockTransport(
            lambda r, c=content: httpx.Response(200, content=c) if "product-screener" in r.url.path
            else httpx.Response(200, json=[])))
        with pytest.raises(NoSource):
            fetch(s, "IE0000000017")  # the search asked instead, and it knows nothing
        assert reason in s.finder_error
    raw = json.dumps({"1": {"isin": {"d": "IE0000000017"}, "productPageUrl": {"r": "/uk/a/en/products/123456/x-etf"}}})
    s = Sources(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=raw.encode())))
    assert s.ishares_product("IE0000000017") == ("123456", "x-etf")  # values as {d, r} like its numbers


def test_an_ishares_fund_without_a_list_gets_its_reason_and_the_pass_is_recorded(tmp_path):
    state = State(tmp_path)
    depot = Depot(positions=[
        {"id": "a-unknown", "isin": "IE00B5BMR087", "name": "iShares Core S&P 500", "currency": "EUR", "value": D(9)},
        {"id": "a-noisin", "isin": "", "name": "iShares Example", "currency": "EUR", "value": D(8)},
        {"id": "a-other", "isin": "IE00BK5BQT80", "name": "Vanguard All-World", "currency": "EUR", "value": D(7)},
        {"id": "a-stock", "isin": "US0378331005", "name": "Apple", "currency": "USD", "value": D(6)}])
    r = lookthrough.update(state, depot, sources(lambda req: httpx.Response(404)))
    assert (r.fetched, r.none, len(r.errors)) == (0, 1, 3)
    assert "kennt die ISIN IE00B5BMR087 nicht" in lookthrough.status(state, "a-unknown")["error"]
    assert "keine ISIN" in lookthrough.status(state, "a-noisin")["error"]
    assert "Vanguard kennt die ISIN IE00BK5BQT80 nicht" in lookthrough.status(state, "a-other")["error"]
    assert lookthrough.status(state, "a-stock")["status"] == "none"  # a stock is no fund: no error
    assert lookthrough.last_pass(state) | {"at": ""} == {"fetched": 0, "none": 1, "failed": 3, "finder": "", "at": ""}


def test_a_stock_the_search_could_not_check_is_not_counted_as_failed(tmp_path):
    state = State(tmp_path)
    depot = Depot(positions=[
        {"id": "a-etf", "isin": "IE00B5BMR087", "name": "iShares Core S&P 500", "currency": "EUR", "value": D(9)},
        {"id": "a-stock", "isin": "US0378331005", "name": "Apple", "currency": "USD", "value": D(5)}])
    r = lookthrough.update(state, depot, search(b"<html>splash</html>"))
    assert [e.split(":")[0] for e in r.errors] == ["iShares Core S&P 500"]
    assert lookthrough.last_pass(state)["finder"] == "iShares-Fondsfinder: HTTP 404"


def test_a_product_address_without_the_pages_name_is_taken_and_the_german_site_is_asked_too():
    page = "https://www.ishares.com/de/privatanleger/de/produkte/308858/"
    assert lookthrough.ishares_page(page) == ("308858", "holdings") == lookthrough.ishares_page(page[:-1])
    asked = []

    def answer(request: httpx.Request) -> httpx.Response:
        asked.append(str(request.url))
        if "product-screener" in request.url.path:
            return httpx.Response(404)
        if "autoComplete" in request.url.path:  # the UK site doesn't know it, the German one does
            hit = [{"category": "productAutocomplete", "id": "/de/privatanleger/de/produkte/308858/"}]
            return httpx.Response(200, json=[] if "/uk/" in request.url.path else hit)
        return httpx.Response(200, content=ISHARES_FULL)

    lines, source = fetch(Sources(transport=httpx.MockTransport(answer)), "IE00BJ5JNY98")
    searches = [a for a in asked if "autoComplete" in a]
    assert source == "iShares" and "term=IE00BJ5JNY98" in searches[0] and "/de/privatanleger/de/" in searches[1]
    assert "portfolioId=308858&component=fundDownloadV2" in asked[-1]


def test_only_ishares_funds_are_searched_a_429_stops_the_pass_and_a_found_product_is_kept(tmp_path):
    state = State(tmp_path)
    depot = Depot(positions=[
        {"id": "a-stock", "isin": "US0378331005", "name": "Apple", "currency": "USD", "value": D(9)},
        {"id": "a-etf", "isin": "IE00BJ5JNY98", "name": "iShares MSCI Europe SRI", "currency": "EUR", "value": D(8)},
        {"id": "a-etf2", "isin": "IE00B5BMR087", "name": "iShares Core S&P 500", "currency": "EUR", "value": D(7)}])
    asked = []

    def answer(request: httpx.Request) -> httpx.Response:
        asked.append(str(request.url))
        if "product-screener" in request.url.path:
            return httpx.Response(404)
        if "autoComplete" in request.url.path:
            if "IE00B5BMR087" in str(request.url):
                return httpx.Response(429)
            return httpx.Response(200, json=[{"category": "productAutocomplete", "id": "/uk/a/en/products/308858/"}])
        return httpx.Response(200, content=ISHARES_FULL)

    r = lookthrough.update(state, depot, Sources(transport=httpx.MockTransport(answer)))
    assert not any("US0378331005" in a for a in asked)  # a stock is never searched
    assert r.fetched == 1 and lookthrough.status(state, "a-etf")["product"] == ["308858", "holdings"]
    assert "HTTP 429" in lookthrough.status(state, "a-etf2")["error"]
    asked.clear()
    lookthrough.update(state, depot, Sources(transport=httpx.MockTransport(answer)), force=True, only="a-etf")
    issuer = [a for a in asked if "finanzfluss" not in a]  # its TER: Finanzfluss, as iShares listed none here
    assert len(issuer) == 1 and "portfolioId=308858&" in issuer[0]  # no finder, no search: the kept product

    def limited(request: httpx.Request) -> httpx.Response:
        asked.append(str(request.url))
        return httpx.Response(429)

    asked.clear()
    lookthrough.update(state, depot, Sources(transport=httpx.MockTransport(limited)), force=True)
    assert len(asked) == 1  # the first 429 ends the pass's requests


# Fabricated, in the layout of the product page's download (Excel 2003 XML, 10/2026): sheets by name, the holdings
# below a line of fund details, numbers typed, a bare & in a name, an empty cell skipped with ss:Index.
def cell(value: str, kind: str = "String", index: int = 0) -> str:
    at = f' ss:Index="{index}"' if index else ""
    return f'<ss:Cell{at}><ss:Data ss:Type="{kind}">{value}</ss:Data></ss:Cell>'


def workbook() -> bytes:
    holdings = [  # the real layout (10/2026): German columns, decimal comma, no location or exchange
        ["All", "26.Okt.2026"],
        ["Emittententicker", "Name", "Sektor", "Anlageklasse", "Marktwert", "Gewichtung (%)", "Nominalwert",
         "Nominale", "Marktwährung"],
        ["NVDA", "NVIDIA", "IT", "Aktien", "USD 2.000", "24,50", "2.000,00", "1.000", "USD"],
        ["AAPL", "APPLE", "IT", "Aktien", "USD 1.600", "20,10", "1.600,00", "800", "USD"],
        ["MSFT", "MICROSOFT", "IT", "Aktien", "USD 1.500", "18,00", "1.500,00", "700", "USD"],
        ["T", "AT&T INC", "Kommunikation", "Aktien", None, "20,20", "1.700,00", "600", "USD"],
        ["ASML", "ASML HOLDING NV", "IT", "Aktien", "EUR 1.400", "16,90", "1.400,00", "300", "EUR"]]
    rows = ""
    for values in holdings:
        cells, skip = "", False
        for i, v in enumerate(values, 1):
            if v is None:
                skip = True
                continue
            cells += cell(str(v), "Number" if isinstance(v, float) else "String", i if skip else 0)
            skip = False
        rows += f"<ss:Row>{cells}</ss:Row>"
    return ('<?xml version="1.0"?><ss:Workbook xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet">'
            f'<ss:Worksheet ss:Name="Fund Header"><ss:Table><ss:Row>{cell("Example")}</ss:Row></ss:Table>'
            f'</ss:Worksheet><ss:Worksheet ss:Name="Holdings"><ss:Table>{rows}</ss:Table></ss:Worksheet>'
            '</ss:Workbook>').encode()


def test_the_product_pages_download_is_read_from_its_holdings_sheet():
    asked = []

    def answer(request: httpx.Request) -> httpx.Response:
        asked.append(str(request.url))
        return httpx.Response(200, content=workbook(), headers={"content-type": "application/vnd.ms-excel"})

    lines = Sources(transport=httpx.MockTransport(answer)).ishares("308858", "holdings")
    assert asked == [lookthrough.ISHARES_DOC.format(id="308858")]
    assert [c.key for c in lines][:2] == ["NVDA:", "AAPL:"] and lines[0].weight == D("0.245")
    assert lines[3].name == "AT&T INC" and lines[3].sector == "Kommunikation" and lines[3].weight == D("0.202")
    assert lines[4].currency == "EUR"
    page = b"<!doctype html><html><title>iShares</title></html>"
    with pytest.raises(SourceError, match="keine Positionsliste zu Produkt 1"):
        Sources(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=page))).ishares("1", "x")


def test_the_country_fund_finders_are_asked_in_turn_and_the_number_may_be_only_the_key():
    nl = {"111111": {"isin": {"d": "IE0000000017"}, "productPageUrl": "/nl/a/nl/producten/111111/other"}}
    it = {"308858": {"isin": "IE00BJ5JNY98", "fundName": "Example IT Sector"}}  # no page: the key is the number
    de = {"251911": {"isin": "DE000A0H0744", "fundName": "Example Dividend 50 UCITS ETF (DE)"}}  # German site only
    asked = []

    def answer(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        asked.append(url)
        if "product-screener" in url:
            return httpx.Response(200, json=de if "country=de" in url else nl if "/nl/" in url
                                  else it if "/it/" in url else {})
        return httpx.Response(200, content=workbook())

    s = Sources(transport=httpx.MockTransport(answer))
    lines, _ = fetch(s, "IE00BJ5JNY98")
    assert len(lines) == 5 and "portfolioId=308858&" in asked[-1] and not s.finder_down
    assert sum("product-screener" in a for a in asked) == 3  # German, Dutch, Italian; Spain and the UK not asked
    assert s.ishares_product("IE0000000017")[0] == "111111"  # a Dutch page address: the number from the key
    assert s.ishares_product("DE000A0H0744")[0] == "251911"  # a (DE) fund: only the German screener server lists it
    with pytest.raises(NoSource):
        fetch(s, "US0378331005", search=False)  # a stock: every list read once, none knows it
    assert sum("product-screener" in a for a in asked) == 5


def test_the_shipped_list_names_the_product_without_asking_a_fund_finder(monkeypatch):
    monkeypatch.setattr(lookthrough, "KNOWN", {"IE00BJ5JNY98": "308858"})
    asked = []

    def answer(request: httpx.Request) -> httpx.Response:
        asked.append(str(request.url))
        return httpx.Response(200, content=workbook())

    lines, _ = fetch(Sources(transport=httpx.MockTransport(answer)), "IE00BJ5JNY98")
    assert len(lines) == 5 and asked == [lookthrough.ISHARES_DOC.format(id="308858")]


def test_the_batch_script_keeps_old_numbers_and_adds_the_finders_ones(tmp_path, monkeypatch):
    import runpy
    import sys
    from pathlib import Path

    answer = tmp_path / "nl.json"
    answer.write_text(json.dumps({"308858": {"isin": "IE00BJ5JNY98"}, "280510": {"isin": "IE00B3WJKG14"}}))
    out = tmp_path / "products.json"
    monkeypatch.setattr(lookthrough, "PRODUCTS_FILE", out)
    monkeypatch.setattr(lookthrough, "KNOWN", {"IE0000000017": "111111"})
    monkeypatch.setattr(sys, "argv", ["ishares_products.py", str(answer)])
    runpy.run_path(str(Path(__file__).parents[1] / "scripts" / "ishares_products.py"))
    assert json.loads(out.read_text()) == {"IE0000000017": "111111", "IE00B3WJKG14": "280510",
                                           "IE00BJ5JNY98": "308858"}
    answer.write_text("{}")
    with pytest.raises(SystemExit, match="nothing written"):
        runpy.run_path(str(Path(__file__).parents[1] / "scripts" / "ishares_products.py"))


# ── other issuers (fabricated answers in each issuer's layout) ───────────────
FIVE = ["US0000000002", "US0000000010", "US0000000028", "US0000000036", "US0000000044"]


def issuers(answers: dict[str, httpx.Response]) -> tuple[Sources, list[str]]:
    """Sources whose requests are answered by the first key found in the URL; the rest 404."""
    asked: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        asked.append(str(request.url))
        return next((r for k, r in answers.items() if k in str(request.url)), httpx.Response(404))
    return Sources(transport=httpx.MockTransport(answer)), asked


def test_an_spdr_fund_is_found_by_its_ticker_in_the_fund_finder():
    kid = "/library-content/kids?isin=IE00B6YX5C33&documentType=kid&country=de&language=en&ticker=spy5-gy"
    finder = {"data": {"funds": {"etfs": {"datas": [{"fundName": "Example", "ter": ["0,12%", 0.12], "documentPdf": [
        {"docType": "Kid", "docs": [{"path": kid}]}]}]}}}}
    doc = xlsx([["Fund Name:", "SPDR Example"], ["ISIN:", "IE00B6YX5C33"], [],
                ["ISIN", "SEDOL", "Security Name", "Currency", "Percent of Fund", "Trade Country Name"]]
               + [[i, "1", f"Example {n} Corporation", "USD", "19.5", "United States"] for n, i in enumerate(FIVE)])
    s, asked = issuers({"fundfinder": httpx.Response(200, json=finder),
                        "en-spy5-gy.xlsx": httpx.Response(200, content=doc)})
    lines, source = fetch(s, "IE00B6YX5C33", by="SPDR")
    assert source == "SPDR" and len(lines) == 5 and lines[0].key == FIVE[0] and lines[0].weight == D("0.195")
    assert lines[0].country == "United States" and s.ter("IE00B6YX5C33") == D("0.0012")
    with pytest.raises(NoSource):
        fetch(s, "IE0000000017", by="SPDR")  # not in the finder
    assert sum("fundfinder" in a for a in asked) == 1  # read once a pass


def test_an_amundi_fund_comes_from_its_product_api_and_a_swap_says_so():
    rows = [{"compositionCharacteristics": {"name": f"EXAMPLE {n}", "isin": i, "weight": 0.19, "sector": "IT",
                                            "countryOfRisk": "France", "currency": "EUR"}, "weight": 0.19}
            for n, i in enumerate(FIVE)]
    answer = {"products": [{"productId": "LU1681043599", "characteristics": {
        "FUND_REPLICATION_METHODOLOGY": "Indirect(Swap Based)", "TER": 0.38},
        "composition": {"compositionData": rows}}]}
    s, asked = issuers({"getProductsData": httpx.Response(200, json=answer)})
    lines, source = fetch(s, "LU1681043599", by="Amundi")
    assert source == "Amundi" and len(lines) == 5 and lines[0].weight == D("0.19") and lines[0].country == "France"
    assert s.note == "swap" and s.ter("LU1681043599") == D("0.0038")
    s, _ = issuers({"getProductsData": httpx.Response(200, json={"products": [{"composition": {}}]})})
    with pytest.raises(NoSource):
        fetch(s, "LU0000000000", by="Amundi")


def test_an_invesco_fund_takes_its_index_first_and_an_unknown_isin_is_no_error():
    rows = [{"name": f"EXAMPLE {n} INC USD0.01", "isin": i, "weight": 19.6} for n, i in enumerate(FIVE)]
    s, asked = issuers({"/holdings/index": httpx.Response(200, json={"holdings": rows})})
    lines, source = fetch(s, "IE00B3YCGJ38", by="Invesco")
    assert source == "Invesco" and lines[0].weight == D("0.196") and "/holdings/index" in asked[0]
    s, asked = issuers({"/holdings/fund": httpx.Response(200, json={"holdings": rows}),
                        "/holdings/index": httpx.Response(500)})
    assert fetch(s, "IE0032077012", by="Invesco")[1] == "Invesco"  # a fund without an index list: its own
    s, _ = issuers({"invesco": httpx.Response(500)})
    with pytest.raises(NoSource):
        fetch(s, "IE0000000017", by="Invesco")


def test_a_vanguard_fund_is_read_page_by_page_over_its_portfolio_id(monkeypatch):
    monkeypatch.setattr(lookthrough, "PRODUCTS", {"Vanguard": {"IE00B3RBWM25": "9505"}})
    pages = []

    def answer(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert request.headers["X-Consumer-ID"] == "uk2" and body["variables"]["portIds"] == ["9505"]
        pages.append(body["variables"]["lastItemKey"])
        items = [{"issuerName": f"Example {n} Corp", "marketValuePercentage": 19.0, "ticker": f"EX{n}",
                  "bloombergIsoCountry": "US", "gicsSectorDescription": "Information Technology"}
                 for n in (range(3) if not pages[-1] else range(3, 5))]
        return httpx.Response(200, json={"data": {"borHoldings": [{"holdings": {
            "items": items, "lastItemKey": None if pages[-1] else "next"}}]}})
    lines, source = fetch(Sources(transport=httpx.MockTransport(answer)), "IE00B3RBWM25", by="Vanguard")
    assert source == "Vanguard" and len(lines) == 5 and pages == [None, "next"] and lines[0].key == "EX0:"


def test_an_isin_not_shipped_is_looked_up_once_among_every_vanguard_id():
    asked = []

    def answer(request: httpx.Request) -> httpx.Response:
        ports = json.loads(request.content)["variables"]["portIds"]
        asked.append(len(ports))
        return httpx.Response(200, json={"data": {"funds": [
            {"portId": "9506", "profile": {"identifiers": [{"altIdCode": "ISIN", "altIdValue": "IE00B8GKDB10"}]}}]
            if "9506" in ports else []}})
    s = Sources(transport=httpx.MockTransport(answer))
    assert s.vanguard_port("IE00B8GKDB10") == "9506" and s.vanguard_port("IE0000000017") is None
    assert asked == [1000] * 10  # once a pass


def test_the_issuer_comes_from_the_name_and_a_stock_is_no_fund():
    def p(name):
        return {"name": name}
    assert lookthrough.issuer(p("State Street SPDR S&P 500 UCITS ETF"), {}) == "SPDR"
    assert lookthrough.issuer(p("Xtrackers NASDAQ 100 UCITS ETF 1C"), {}) == "Xtrackers"
    assert lookthrough.issuer(p("L&G Cyber Security UCITS ETF"), {}) == "L&G"
    assert lookthrough.issuer(p("Vanguard FTSE All-World High Dividend Yield"), {}) == "Vanguard"
    assert lookthrough.issuer(p("Lyxor Core MSCI World"), {}) == "Amundi"
    assert lookthrough.issuer(p("Some fund"), {"url": "https://www.ishares.com/x"}) == "iShares"
    assert not lookthrough.is_fund(p("Allianz SE"), {}) and not lookthrough.is_fund(p("Mercedes-Benz Group AG"), {})
    assert lookthrough.is_fund(p("Unknown Issuer MSCI World UCITS ETF"), {})
    assert lookthrough.issuer(p("Unknown Issuer MSCI World UCITS ETF"), {}) == ""


def test_a_vaneck_fund_is_found_on_its_holdings_page_and_read_from_its_block():
    listing = '<a href="/de/de/anlagen/semiconductor-etf/">SMH</a><a href="/de/de/anlagen/gold-etp/">GOLD</a>'
    page = ('<ve-fundticker class="d-none">UCTSMH</ve-fundticker> ISIN IE00BMC38736 '
            '<ve-holdingsblock class="" data-blockid="194732" data-pageid="233164" data-template="">')
    rows = [{"HoldingName": f"Example {n} Inc", "Weight": "19.80", "Label": f"EX{n} US", "Country": "United States",
             "Sector": "Information Technology", "CurrencyCode": "USD"} for n in range(5)]
    s, asked = issuers({"factsheets": httpx.Response(200, text=listing),
                        "semiconductor-etf/positionen": httpx.Response(200, text=page),
                        "blockid=194732&pageid=233164&ticker=UCTSMH": httpx.Response(200, json={"data": {
                            "Holdings": rows}})})
    lines, source = fetch(s, "IE00BMC38736", by="VanEck")
    assert source == "VanEck" and len(lines) == 5 and lines[0].weight == D("0.198") and lines[0].key == "EX0 US:"
    with pytest.raises(NoSource):
        fetch(s, "IE0000000017", by="VanEck")
    assert sum("factsheets" in a for a in asked) == 1  # the site's list once a pass


def test_an_ark_fund_is_found_on_its_page_and_its_full_holdings_downloaded():
    listing = ('<a href="/de/funds/active-etfs/artificial-intelligence-robotics/">AI</a>'
               '<a href="/de/funds/index-etfs/cybersecurity-and-data-privacy/">Cyber</a>')
    doc = xlsx([["Data from 28/10/2026"], ["Name", "ISIN", "Weight", "Currency"]]
               + [[f"EXAMPLE {n} INC COM", i, "19.90%", "USD"] for n, i in enumerate(FIVE)])
    s, asked = issuers({"robotics/full-fund-holdings-download": httpx.Response(200, content=doc),
                        "/de/funds/active-etfs/artificial-intelligence-robotics/": httpx.Response(
                            200, text="<strong>ISIN:</strong> IE0003A512E4 <tr>\n<td>Gesamtkostenquote</td>\n"
                            "<td>0.75%</td>"),
                        "/de/funds/index-etfs/": httpx.Response(200, text="ISIN IE00BJXRZJ40"),
                        "/de/funds/": httpx.Response(200, text=listing)})
    lines, source = fetch(s, "IE0003A512E4", by="ARK")
    assert source == "ARK" and len(lines) == 5 and lines[0].key == FIVE[0] and lines[0].weight == D("0.199")
    assert s.ter("IE0003A512E4") == D("0.0075")
    with pytest.raises(NoSource):
        fetch(s, "IE0000000017", by="ARK")
    assert sum(a.endswith("/de/funds/") for a in asked) == 1
    assert lookthrough.issuer({"name": "ARK Artificial Intelligence & Robotics UCITS ETF"}, {}) == "ARK"
    assert lookthrough.issuer({"name": "Future of Defence UCITS ETF"}, {}) == "HANetf"
    assert lookthrough.issuer({"name": "EMQQ Emerging Markets Internet UCITS ETF"}, {}) == "HANetf"


def test_a_hanetf_fund_is_read_from_its_holdings_file_by_the_shipped_code(monkeypatch):
    monkeypatch.setattr(lookthrough, "PRODUCTS", {"HANetf": {"IE0000000017": "EXMP"}})
    doc = xlsx([["Security Description", "Shares", "Market Value (Base)", "Trading Currency", "SEDOL/CUSIP",
                 "Exposure Country", "Region", "ISIN", "Weight"]]
               + [[f"EXAMPLE {n} AG", "100", "1000", "EUR", "0000000", "Germany", "Europe", i, "0.199"]
                  for n, i in enumerate(FIVE)])
    products = xlsx([["ETFs/ETCs", "Ticker", "ISIN", "TER"],
                     ["Example UCITS ETF", "EXMP GY", "IE0000000017", "0.0049"]])
    s, asked = issuers({"Holdings-EXMP-IE0000000017-all-all.xlsx": httpx.Response(200, content=doc),
                        "productlist-all": httpx.Response(200, content=products)})
    lines, source = fetch(s, "IE0000000017", by="HANetf")
    assert s.ter("IE0000000017") == D("0.0049")
    assert source == "HANetf" and len(lines) == 5 and lines[0].key == FIVE[0] and lines[0].weight == D("0.199")
    assert lines[0].country == "Germany" and lines[0].currency == "EUR"
    with pytest.raises(NoSource):
        fetch(s, "IE0000000025", by="HANetf")  # no shipped code: nothing asked
    assert len(asked) == 2  # the holdings and, once a pass, the product list with the TERs
    assert lookthrough.issuer({"name": "Sprott Example Miners UCITS ETF", "isin": "IE0000000017"}, {}) == "HANetf"


LGIM_URL = "https://fundcentres.lgim.com/srp/documents-id/00000000-0000-4000-8000-000000000000/Fundholdings.csv"


def test_an_lgim_fund_is_read_daily_from_the_address_the_user_saved(tmp_path):
    csv = ("sep=,\nBasket NameL&G Example UCITS ETF\nETF Trading IDIE0000000017\nBasket Trade Date2026-10-28\n"
           "Security Description,ISIN,Trading Currency,Constituent Weight (Base)\n"
           + "".join(f"EXAMPLE {n} INC USD 0.01,{i},USD,0.199\n" for n, i in enumerate(FIVE))).encode()
    assert lookthrough.lgim_doc(LGIM_URL) and not lookthrough.lgim_doc(LGIM_URL.replace("lgim.com", "evil.com"))
    s, asked = issuers({"documents-id": httpx.Response(200, content=csv)})
    lines, source = fetch(s, "IE0000000017", LGIM_URL)
    assert source == "L&G" and len(lines) == 5 and lines[0].key == FIVE[0] and lines[0].weight == D("0.199")
    state = State(tmp_path)
    p = {"id": "a-lg", "isin": "IE00BYPLS672", "name": "L&G Cyber Security UCITS ETF", "currency": "EUR", "value": D(9)}
    lookthrough.update(state, Depot(positions=[p]), issuers({})[0])
    assert "Fundholdings.csv" in lookthrough.status(state, "a-lg")["error"]  # what to enter, at its card
    lookthrough.set_status(state, "a-lg", url=LGIM_URL, status="")
    lookthrough.update(state, Depot(positions=[p]), s)
    st = lookthrough.status(state, "a-lg")
    assert st["status"] == "ok" and lookthrough.issuer(p, st) == "L&G"


def test_a_fund_without_the_issuers_ter_gets_finanzfluss_and_asks_again_only_after_a_month(tmp_path):
    state = State(tmp_path)
    depot = Depot(positions=[
        {"id": "a-ubs", "isin": "LU0000000000", "name": "UBS Example UCITS ETF", "currency": "EUR", "value": D(9)},
        {"id": "a-own", "isin": "IE0000000017", "name": "Example UCITS ETF", "currency": "EUR", "value": D(8)}])
    lookthrough.set_status(state, "a-ubs", status="upload")
    lookthrough.set_status(state, "a-own", status="upload", ter="0.002", ter_source="SPDR")
    faq = '"text":"Der Example Fonds hat eine Gesamtkostenquote (TER) von 0,22%. Das bedeutet …"'
    s, asked = issuers({"finanzfluss.de/informer/etf/lu0000000000/": httpx.Response(200, text=faq)})
    lookthrough.update(state, depot, s)
    assert lookthrough.ter(lookthrough.status(state, "a-ubs")) == (D("0.0022"), "Finanzfluss")
    assert lookthrough.ter(lookthrough.status(state, "a-own")) == (D("0.002"), "SPDR")  # the issuer's stays
    assert sorted(asked) == ["https://www.finanzfluss.de/informer/etf/ie0000000017/",  # its breakdown
                             "https://www.finanzfluss.de/informer/etf/lu0000000000/"]
    asked.clear()
    lookthrough.update(state, depot, s)
    assert asked == []  # asked lately


def test_a_fund_without_countries_takes_its_issuers_breakdown_else_the_country_other_funds_name(tmp_path):
    rows = [["Sector"], ["Kategorie", "Fonds", ""], ["IT", "30,65"], [], ["Geography/Locations"],
            ["Per", "27.Okt.2026"], ["Kategorie", "Fonds", "", ""], ["Vereinigte Staaten", "72,85", "US"],
            ["Deutschland", "2,05", "DE"], ["Other", "4,91", "OTHR"], ["Cash und/oder Derivate", "0,46", "CASHD_C"],
            [], ["Allokationen können sich ändern."]]
    assert lookthrough.locations(rows) == [["US", "0.7285"], ["DE", "0.0205"]]
    state = State(tmp_path)
    state.set_fund("a-world", "World", [["~A", "APPLE INC", "0.5", "", "IT", "USD"]])  # iShares: no country
    lookthrough.set_status(state, "a-world", regions=[["US", "0.7285"], ["DE", "0.0205"]])
    state.set_fund("a-tech", "Tech", [["~A", "Apple Inc.", "0.6", "", "IT", "USD"],
                                     ["~X", "Unknown Corp", "0.4", "", "IT", "USD"]])
    state.set_fund("a-spdr", "S&P", [["US0378331005", "APPLE INC", "0.07", "United States", "IT", "USD"]])
    depot = Depot(positions=[{"id": k, "isin": "", "name": k, "currency": "EUR", "value": D(1)}
                             for k in ("a-world", "a-tech", "a-spdr")])
    fake, wf = _wf()
    lookthrough.write_taxonomies(state, wf, depot)
    regions = sorted((a["assetId"], a["categoryId"], a["weight"]) for a in fake.assignments
                     if a["taxonomyId"] == "regions")
    # World: the issuer's breakdown (Other and cash stay unassigned); Tech: Apple's country from the S&P fund.
    assert regions == [("a-spdr", "country_US", 700), ("a-tech", "country_US", 6000),
                       ("a-world", "country_DE", 205), ("a-world", "country_US", 7285)]


def test_issuers_spellings_without_commas_dws_sectors_and_a_single_country_fund(tmp_path, monkeypatch):
    state = State(tmp_path)
    state.set_fund("a-x", "X", [["US0000000002", "A", "0.5", "Taiwan Region", "Technologie", "USD"],
                                ["US0000000010", "B", "0.3", "Großbritannien (UK)", "Industrieunternehmen", "USD"]])
    depot = Depot(positions=[{"id": "a-x", "isin": "", "name": "X", "currency": "EUR", "value": D(1)}])
    fake, wf = _wf()
    fake.categories["regions"] += [{"id": "country_TW", "name": "Taiwan", "parentId": "R3020"},
                                   {"id": "country_GB", "name": "United Kingdom", "parentId": "R1010"}]
    fake.categories["industries_gics"].append({"id": "101020", "name": "Oil, Gas & Consumable Fuels",
                                               "parentId": "10"})
    fake.categories["industries_gics"].append({"id": "10", "name": "Energy", "parentId": None})
    lookthrough.write_taxonomies(state, wf, depot)
    mine = sorted((a["categoryId"], a["weight"]) for a in fake.assignments)
    assert mine == [("20", 3000), ("45", 5000), ("country_GB", 3000), ("country_TW", 5000)]
    fake.assignments.clear()
    state.set_fund("a-x", "X", [["US0000000002", "A", "0.5", "", "Oil Gas & Consumable Fuels", "USD"]])  # SPDR
    lookthrough.write_taxonomies(state, wf, depot)
    assert [(a["categoryId"], a["weight"]) for a in fake.assignments] == [("10", 5000)]
    fake.assignments.clear()
    fake.categories["industries_gics"] += [{"id": "2030", "name": "Transportation", "parentId": "20"},
                                           {"id": "203040", "name": "Road & Rail", "parentId": "2030"}]
    state.set_fund("a-x", "X", [["US0000000002", "A", "0.5", "", "Ground Transportation", "USD"]])  # GICS 2023
    lookthrough.write_taxonomies(state, wf, depot)
    assert [(a["categoryId"], a["weight"]) for a in fake.assignments] == [("20", 5000)]
    # iShares' fund finder names a single-country fund's country, also for a fund from the shipped list.
    monkeypatch.setattr(lookthrough, "KNOWN", {"IE00BZCQB185": "280510"})
    rows = {"1": {"isin": "IE00BZCQB185", "productPageUrl": "/de/produkte/280510/x", "aladdinCountry": "Indien"},
            "2": {"isin": "IE00B4L5Y983", "productPageUrl": "/de/produkte/251882/y", "aladdinCountry": "Weitere"}}
    s, asked = issuers({"product-screener": httpx.Response(200, json=rows)})
    assert s.fund_country("IE00BZCQB185") == [["Indien", "1"]] and s.fund_country("US0378331005") == []
    assert len(asked) == 1  # read once a pass, and only for a fund of the shipped list


# Fabricated, in the layout of a Finanzfluss ETF page (its country and industry blocks).
FF_PAGE = ('"text":"Der Example Fonds hat eine Gesamtkostenquote (TER) von 0,22%." <h3>Länder</h3>'
           + "".join(f'<span class="sr-only">{c}</span><p class="country-name">{n}</p>'
                     f'<progress class="progress-bar" value="{w}" max="100">{w}</progress>'
                     for c, n, w in (("US", "Vereinigte Staaten", "70"), ("DE", "Deutschland", "20")))
           + "<h3>Regionen</h3> <h3>Branchen</h3>"
           + "".join(f'<div class="label ml-[8px]">{n}</div><progress class="progress-bar" value="{w}" max="100">'
                     for n, w in (("Halbleiter", "50"), ("Fahrzeuge &amp; Komponenten", "30"), ("Exotisches", "5")))
           + "<h3>Anlageklassen</h3>")


def test_finanzfluss_breaks_down_a_fund_whose_list_names_no_countries_or_sectors(tmp_path):
    ff = lookthrough.finanzfluss_page(FF_PAGE)
    assert ff["ter"] == D("0.0022") and ff["regions"] == [["US", "0.7"], ["DE", "0.2"]]
    assert ff["sectors"] == [["Information Technology", "0.5"], ["Consumer Discretionary", "0.3"],
                             ["Exotisches", "0.05"]]
    state = State(tmp_path)
    state.set_fund("a-lg", "L&G", [["~A", "A CORP", "0.6", "", "", "USD"], ["~B", "B CORP", "0.4", "", "", "USD"]])
    lookthrough.set_status(state, "a-lg", ff_regions=ff["regions"], ff_sectors=ff["sectors"])
    state.set_fund("a-own", "Own", [["US0000000002", "C", "0.9", "Germany", "Industrials", "EUR"]])
    lookthrough.set_status(state, "a-own", ff_regions=[["US", "1"]], ff_sectors=[])
    depot = Depot(positions=[{"id": k, "isin": "", "name": k, "currency": "EUR", "value": D(1)}
                             for k in ("a-lg", "a-own")])
    fake, wf = _wf()
    r = lookthrough.write_taxonomies(state, wf, depot)
    mine = sorted((a["assetId"], a["categoryId"], a["weight"]) for a in fake.assignments)
    # L&G's list names nothing: Finanzfluss' breakdown; the own fund's list covers more than Finanzfluss: its own.
    assert mine == [("a-lg", "45", 5000), ("a-lg", "country_DE", 2000), ("a-lg", "country_US", 7000),
                    ("a-own", "20", 9000), ("a-own", "country_DE", 9000)]
    assert "Exotisches" in r.unmatched
    assert lookthrough.status(state, "a-lg")["from_finanzfluss"] == ["regions", "industries_gics"]
    assert not lookthrough.status(state, "a-own").get("from_finanzfluss")


def test_an_empty_finanzfluss_page_keeps_the_breakdown_read_before(tmp_path, caplog):
    """#134 D1: Finanzfluss changing its page must not wipe the countries and sectors it gave last time."""
    state = State(tmp_path)
    depot = Depot(positions=[{"id": "a-ubs", "isin": "LU0000000000", "name": "UBS Example UCITS ETF",
                              "currency": "EUR", "value": D(9)}])
    lookthrough.set_status(state, "a-ubs", status="upload", ff_regions=[["US", "0.6"]], ff_sectors=[["IT", "0.2"]])
    s, _ = issuers({"finanzfluss.de/informer/etf/lu0000000000/": httpx.Response(200, text="<html></html>")})
    lookthrough.update(state, depot, s, force=True)
    st = lookthrough.status(state, "a-ubs")
    assert st["ff_regions"] == [["US", "0.6"]] and st["ff_sectors"] == [["IT", "0.2"]]
    assert "gave nothing" in caplog.text


# Wealthfolio's GICS categories the #166 names map to (ids as in its seed), each under its sector.
GICS_166 = [("15", "Materials", None), ("151050", "Paper & Forest Products", "15"),
            ("20201050", "Environmental & Facilities Services", "20"),
            ("2020", "Commercial & Professional Services", "20"),
            ("201050", "Industrial Conglomerates", "20"), ("25", "Consumer Discretionary", None),
            ("25302010", "Education Services", "25"), ("25203010", "Apparel, Accessories & Luxury Goods", "25"),
            ("30", "Consumer Staples", None), ("30202010", "Agricultural Products", "30"),
            ("35", "Health Care", None), ("352030", "Life Sciences Tools & Services", "35")]


@pytest.mark.parametrize(("name", "sector"), [
    ("Abfallwirtschaft", "20"), ("Bildung", "25"), ("Forsterzeugnisse", "15"), ("Kleidung & Accessoires", "25"),
    ("Konglomerate", "20"), ("Landwirtschaft", "30"), ("Med. Diagnostik & Forschung", "35"),
    ("Unternehmensdienste", "20")])
def test_finanzfluss_industries_without_a_sector_of_their_own_find_their_gics_sector(tmp_path, name, sector):
    """#166: each industry goes to the GICS industry it belongs to, rolled up to its sector."""
    state = State(tmp_path)
    lookthrough.set_status(state, "a-ff", ff_regions=[], ff_sectors=[[name, "0.6"]])
    fake, wf = _wf()
    fake.categories["industries_gics"] += [{"id": i, "name": n, "parentId": p} for i, n, p in GICS_166]
    depot = Depot(positions=[{"id": "a-ff", "isin": "", "name": "F", "currency": "EUR", "value": D(1)}])
    r = lookthrough.write_taxonomies(state, wf, depot)
    assert [(a["categoryId"], a["weight"]) for a in fake.assignments] == [(sector, 6000)]
    assert r.unmatched == [] and r.deliberate == []


def test_cash_other_unknown_and_pseudo_codes_stay_unassigned_and_are_named_apart(tmp_path):
    """#166: what is no country or sector is never given a category and is listed as deliberate; a real country
    Wealthfolio has no category for (Côte d'Ivoire, Jersey) stays a plain "without a category"."""
    state = State(tmp_path)
    state.set_fund("a-w", "W", [["US0000000002", "A", "0.5", "US", "Information Technology", "USD"],
                                ["CI0000000001", "B", "0.1", "CI", "Other", "XOF"],
                                ["JE0000000001", "C", "0.1", "JE", "unbekannt", "GBP"],
                                ["-", "CASH", "0.1", "XE", "Barmittel & Derivate", "EUR"]])
    depot = Depot(positions=[{"id": "a-w", "isin": "", "name": "W", "currency": "EUR", "value": D(1)}])
    fake, wf = _wf()
    fake.categories["industries_gics"].append({"id": "x", "name": "Other", "parentId": None})  # never taken
    r = lookthrough.write_taxonomies(state, wf, depot)
    assert sorted((a["categoryId"], a["weight"]) for a in fake.assignments) == [("45", 5000), ("country_US", 5000)]
    assert r.unmatched == ["CI", "JE"] and r.deliberate == ["Barmittel & Derivate", "Other", "XE", "unbekannt"]
    assert json.loads(state.meta(meta_keys.TAXONOMIES_LAST()))["deliberate"] == r.deliberate


def test_a_fund_without_asset_class_gets_the_one_its_name_names_never_a_guessed_one(tmp_path):
    """wealthfolio-importer-addon#141: only Wealthfolio's own name rules, only with no asset class at all,
    as AUTO; everything else is listed for *Prüfung*."""
    state = State(tmp_path)
    pos = [("a-bond", "iShares Core Euro Govt Bond UCITS ETF"), ("a-gold", "Xtrackers Physical Gold ETC"),
           ("a-world", "iShares Core MSCI World UCITS ETF"), ("a-miners", "VanEck Gold Miners UCITS ETF"),
           ("a-user", "Amundi Euro Government Bond UCITS ETF"), ("a-stock", "Bondholder Corp")]
    depot = Depot(positions=[{"id": i, "isin": "", "name": n, "currency": "EUR", "value": D(1)} for i, n in pos])
    fake, wf = _wf()
    fake.assignments.append({"id": "u1", "assetId": "a-user", "taxonomyId": "asset_classes", "categoryId": "EQUITY",
                             "weight": 10000, "source": "MANUAL"})
    found = lookthrough.asset_classes(state, wf, depot, write=False)  # read only: nothing written
    assert len(fake.assignments) == 1 and found["named"] == {}
    assert [p["id"] for p in found["missing"]] == ["a-bond", "a-gold", "a-world", "a-miners", "a-stock"]
    found = lookthrough.asset_classes(state, wf, depot, write=True)
    mine = sorted((a["assetId"], a["categoryId"], a["weight"], a["source"]) for a in fake.assignments)
    assert mine == [("a-bond", "FIXED_INCOME", 10000, "AUTO"), ("a-gold", "COMMODITIES", 10000, "AUTO"),
                    ("a-user", "EQUITY", 10000, "MANUAL")]  # the user's own stays
    assert [p["id"] for p in found["missing"]] == ["a-world", "a-miners", "a-stock"]
    again = lookthrough.asset_classes(state, wf, depot, write=True)  # still shown as taken from the name
    assert sorted(again["named"]) == ["a-bond", "a-gold"] and len(fake.assignments) == 3
    # Wealthfolio's provider replaces ours: no longer "from the name"
    for a in fake.assignments:
        if a["assetId"] == "a-bond":
            a["categoryId"] = "EQUITY"
    assert sorted(lookthrough.asset_classes(state, wf, depot, write=True)["named"]) == ["a-gold"]
