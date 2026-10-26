import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "scripts" / "lookthrough_probe.py"
spec = importlib.util.spec_from_file_location("probe", SCRIPT)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)

# Fabricated, in the layout of an iShares holdings file: fund details above the header.
ISHARES_CSV = b"""\xef\xbb\xbfiShares Example UCITS ETF
Fund Holdings as of,"01.Oct.2026"
\xc2\xa0
Ticker,Name,Sector,Asset Class,Market Value,Weight (%),Location,Exchange,Currency
AAA,EXAMPLE ONE INC,Information Technology,Equity,"1,000.00",4.50,United States,NASDAQ,USD
BBB,EXAMPLE TWO AG,Industrials,Equity,"500.00",2.25,Germany,Xetra,EUR
"""


def test_classify_reads_the_header_below_the_fund_details():
    result = probe.classify(ISHARES_CSV, "text/csv")
    assert result["kind"] == "csv"
    assert result["rows"] == 2
    assert result["isin"] is False
    assert result["header"].startswith("Ticker,Name")


def test_classify_tells_html_xlsx_and_empty_json_apart():
    assert probe.classify(b"<!DOCTYPE html><html></html>", "text/html")["kind"] == "html"
    assert probe.classify(b"PK\x03\x04rest", "application/octet-stream")["kind"] == "xlsx/zip"
    assert probe.classify(b"[]", "application/json")["kind"] == "json (empty)"
    assert probe.classify(b'{"products": [1]}', "application/json")["kind"] == "json"


def test_row_keeps_the_isin_apart_from_the_isin_column():
    line = probe.row("IE00B4L5Y983", {"source": "DWS xlsx", "status": 200, "kind": "csv", "isin": True})
    assert line.startswith("| IE00B4L5Y983 | DWS xlsx | 200 |")
    assert "| True |" in line
