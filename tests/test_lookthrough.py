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
from brokersync import lookthrough, meta_keys  # noqa: E402
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


def sources(handler) -> Sources:
    return Sources(transport=httpx.MockTransport(handler))


def test_fetch_asks_ishares_by_its_product_id_and_xtrackers_by_isin():
    asked = []

    def answer(request: httpx.Request) -> httpx.Response:
        asked.append(str(request.url))
        assert "Mozilla" in request.headers["user-agent"]
        if request.url.host == "www.ishares.com":
            return httpx.Response(200, content=ISHARES_FULL)
        if "IE00BJ0KDQ92" in request.url.path:
            return httpx.Response(200, content=XTRACKERS)
        return httpx.Response(404)

    s = sources(answer)
    lines, source = fetch(s, "IE00B4L5Y983")
    assert source == "iShares" and len(lines) == 5 and "/products/251882/" in asked[0]
    lines, source = fetch(s, "IE00BJ0KDQ92")
    assert source == "Xtrackers" and lines[0].key == "US0000000002"
    lines, _ = fetch(s, "IE00XXXXXXXX", "https://www.ishares.com/de/privatanleger/de/produkte/123456/example-etf")
    assert "/products/123456/example-etf/" in asked[-1]
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
        ishares = request.url.host == "www.ishares.com"
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
    assert not any(t == "Klumpenrisiko" for t, _, _ in notifier.sent)  # off by default, nothing stored
    config_mod.update(tmp_path, lambda c: setattr(c, "lookthrough_enabled", True))
    syncer._fund_sources = lambda: sources(lambda r: httpx.Response(200, content=ISHARES_FULL))
    syncer.run()
    assert len(State(tmp_path).funds()["IE00B4L5Y983"]["constituents"]) == 5
    risks = [m for t, m, link in notifier.sent if t == "Klumpenrisiko"]
    assert len(risks) == 1 and "neue Klumpenrisiken" in risks[0]
    syncer.run()
    assert len([t for t, _, _ in notifier.sent if t == "Klumpenrisiko"]) == 1  # told once
