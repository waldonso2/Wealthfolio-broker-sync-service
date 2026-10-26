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
