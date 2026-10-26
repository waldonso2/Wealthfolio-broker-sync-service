from decimal import Decimal as D

from brokersync.xray import (
    LIMITS,
    UNASSIGNED,
    Constituent,
    Holding,
    effective_count,
    exposures,
    findings,
    overlap,
    own_share,
    stock,
    unify,
)

# Fabricated funds and weights (not real index data).
EXA = "US0000000AA1"  # one example company in every fund


def fund(id: str, value: str, *lines: tuple[str, str]) -> Holding:
    return Holding(id, id, D(value), tuple(Constituent(k, f"Company {k}", D(w), "US", "IT", "USD") for k, w in lines))


WORLD = fund("world", "6000", (EXA, "0.05"), ("US0000000BB2", "0.45"), ("JP0000000CC3", "0.50"))
SP500 = fund("sp500", "2000", (EXA, "0.07"), ("US0000000BB2", "0.93"))
TECH = fund("tech", "1000", (EXA, "0.10"), ("US0000000DD4", "0.85"))  # 5 % left open
DIRECT = stock("exa", EXA, "Company EXA", D("1000"), country="US")


def test_a_company_in_three_funds_and_held_directly_is_one_row():
    rows = exposures([WORLD, SP500, TECH, DIRECT])
    assert [r.key for r in rows[:2]] == ["US0000000BB2", "JP0000000CC3"]  # largest first
    (top,) = [r for r in rows if r.key == EXA]
    assert top.value == D("300") + D("140") + D("100") + D("1000")
    assert top.share == D("0.154")
    assert top.via == {"world": D("0.03"), "sp500": D("0.014"), "tech": D("0.01"), "exa": D("0.1")}


def test_what_a_fund_leaves_open_is_unassigned_and_shares_add_up_to_one():
    rows = exposures([WORLD, SP500, TECH, DIRECT])
    assert rows[-1].key == UNASSIGNED
    assert rows[-1].value == D("50")
    assert sum(r.share for r in rows) == 1


def test_share_classes_of_one_fund_are_added_up():
    two = Holding("f", "f", D("100"), (Constituent("X", "X A", D("0.6")), Constituent("X", "X C", D("0.4"))))
    (row,) = exposures([two])
    assert (row.name, row.value) == ("X A", D("100"))


def test_overlap_is_the_sum_of_the_smaller_weights():
    assert overlap(WORLD, SP500) == D("0.05") + D("0.45")
    assert overlap(SP500, TECH) == D("0.07")
    assert overlap(WORLD, DIRECT) == D("0.05")


def test_own_share_counts_only_companies_no_other_holding_brings():
    holdings = [WORLD, SP500, TECH]
    assert own_share(SP500, holdings) == 0
    assert own_share(WORLD, holdings) == D("0.50")
    assert own_share(TECH, holdings) == D("0.85")


def test_effective_count():
    rows = exposures([fund("a", "100", ("A", "0.5"), ("B", "0.5"))])
    assert effective_count(rows) == 2
    assert effective_count([]) == 0


def test_unify_gives_a_line_without_isin_the_isin_of_the_same_company_elsewhere():
    ishares = Holding("i", "i", D("100"), (Constituent("AAA:NASDAQ", "EXAMPLE ONE INC", D("0.5")),
                                           Constituent("BBB:XETRA", "EXAMPLE TWO AG", D("0.5"))))
    other = Holding("o", "o", D("100"), (Constituent("US0000000AA1", "Example One, Inc.", D("1")),))
    third = Holding("t", "t", D("100"), (Constituent("CCC:LSE", "Example Two AG Reg.", D("1")),))
    a, b, c = unify([ishares, other, third])
    assert [x.key for x in a.constituents] == ["US0000000AA1", "~EXAMPLE TWO"]
    assert b == other
    assert c.constituents[0].key == "~EXAMPLE TWO"
    assert exposures([a, b, c])[0].value == 150


def test_findings_name_each_cluster_over_its_limit():
    us = fund("us", "9000", ("US0000000AA1", "0.10"), ("US0000000BB2", "0.90"))
    us2 = Holding("us2", "us2", D("1000"), (Constituent("US0000000BB2", "Company BB2", D("1"), "US", "IT", "USD"),
                                            Constituent("JP0000000CC3", "Company CC3", D("0"), "JP", "", "JPY")))
    found = findings([us, us2], LIMITS, "EUR", {"Tagesgeld": D("150000"), "Giro": D("5000")})
    kinds = {(f.kind, f.name) for f in found}
    assert {f.key for f in found if f.kind == "stock"} == {"stock:US0000000BB2", "stock:US0000000AA1"}
    assert ("country", "US") in kinds and ("sector", "IT") in kinds and ("currency", "EUR") in kinds
    assert ("overlap", "us / us2") in kinds and ("effective", "") in kinds and ("cash", "Tagesgeld") in kinds
    assert ("cash", "Giro") not in kinds
    stock = next(f for f in found if f.key == "stock:US0000000BB2")
    assert stock.value == D("0.91") and stock.limit == D("0.05")


def test_no_findings_for_a_broad_depot():
    lines = tuple((f"US{n:09d}0", "0.01") for n in range(100))
    broad = Holding("b", "b", D("100"), tuple(Constituent(k, k, D(w), f"C{n % 10}", f"S{n % 5}", "EUR")
                                             for n, (k, w) in enumerate(lines)))
    assert findings([broad], LIMITS, "EUR") == []
