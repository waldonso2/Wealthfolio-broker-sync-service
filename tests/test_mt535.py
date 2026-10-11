"""MT535 holdings (FinTS HKWPD) - fabricated answers, no real depot data."""

from decimal import Decimal as D

import fints.client

from brokersync.adapters import fints as fints_mod
from brokersync.adapters import mt535

# As python-fints hands it over: the first (empty) line already dropped.
ANSWER = """:16R:GENL
:28E:1/ONLY
:13A::STAT//535
:20C::SEME//NONREF
:23G:NEWM
:98A::STAT//20261011
:22F::STTY//CUST
:97A::SAFE//00009998880000
:17B::ACTI//Y
:16S:GENL
:16R:FIN
:35B:ISIN LU0000000001
/DE/A0TEST
TEST WORLD FONDS
:90B::MRKT//ACTU/EUR98,76
:98A::PRIC//20261010
:93B::AGGR//UNIT/12,5
:16R:SUBBAL
:93C::TAVI//UNIT/AVAI/12,5
:16S:SUBBAL
:19A::HOLD//EUR1234,5
:70E::HOLD//1STK90,12+EUR
:16S:FIN
:16R:FIN
:35B:ISIN DE0000000002
/DE/B0TEST
TEST AKTIE
:90B::MRKT//ACTU/EUR10,
:93B::AGGR//UNIT/7,
:16S:FIN
:16R:FIN
:35B:ISIN DE0000000003
TEST ANLEIHE
:90A::MRKT//PRCT/101,5
:93B::AGGR//FAMT/1000,
:19A::HOLD//NEUR5,
:16S:FIN
:16R:ADDINFO
:19A::HOLP//EUR2254,5
:16S:ADDINFO
-""".splitlines()


def test_holdings_are_read_even_without_a_total_value():
    fund, share, bond = mt535.parse(ANSWER)
    assert (fund.ISIN, fund.name, fund.pieces, fund.value_symbol, fund.total_value) == \
        ("LU0000000001", "TEST WORLD FONDS", D("12.5"), "EUR", D("1234.5"))
    # No :19A: - python-fints' own parser fails here; the value comes from price × shares.
    assert (share.ISIN, share.name, share.pieces, share.total_value) == ("DE0000000002", "TEST AKTIE", D("7"), D("70"))
    assert (bond.ISIN, bond.pieces, bond.value_symbol, bond.total_value) == ("DE0000000003", D("1000"), "EUR", D("-5"))


def test_the_structure_names_tags_but_no_values():
    text = mt535.structure(ANSWER)
    assert ":16R:FIN×3" in text and ":35B:×3" in text and ":93B::AGGR//UNIT×2" in text and ":93B::AGGR//FAMT×1" in text
    for value in ("LU0000000001", "A0TEST", "TEST", "1234", "12,5", "00009998880000", "20261011"):
        assert value not in text
    assert mt535.structure([]) == "leer"


def test_python_fints_uses_our_parser_while_holdings_are_read():
    seen: list[str] = []
    original = fints.client.MT535_Miniparser
    with fints_mod._mt535_parser(seen):
        holdings = fints.client.MT535_Miniparser().parse(ANSWER)
    assert fints.client.MT535_Miniparser is original
    assert [h.ISIN for h in holdings] == ["LU0000000001", "DE0000000002", "DE0000000003"]
    assert seen and ":35B:×3" in seen[0]
