"""Builds contract/xray/cases.json: X-Ray's computations on fabricated depots, for the X-Ray addon to compute the same
(wealthfolio-importer-addon#146). Run after a change to xray.py; tests/test_xray_contract.py fails while it differs.

    python scripts/xray_cases.py > contract/xray/cases.json
"""

from __future__ import annotations

import json
import sys
from decimal import Decimal as D
from itertools import combinations

from brokersync import xray
from brokersync.xray import Constituent, Holding

ISIN_APPLE, ISIN_MSFT, ISIN_SAP, ISIN_NOVO = "US0378331005", "US5949181045", "DE0007164600", "DK0062498333"


def fund(id_: str, name: str, value: str, lines: list[tuple]) -> dict:
    return {"id": id_, "name": name, "value": value, "lines": [list(line) for line in lines]}


CASES = [
    {"name": "a stock held directly and through two ETFs",
     "holdings": [
         fund("w", "World ETF", "1000", [(ISIN_APPLE, "APPLE INC", "0.05", "United States", "IT", "USD"),
                                         (ISIN_MSFT, "MICROSOFT CORP", "0.04", "United States", "IT", "USD"),
                                         (ISIN_SAP, "SAP SE", "0.01", "Germany", "IT", "EUR")]),
         fund("t", "Tech ETF", "500", [(ISIN_APPLE, "Apple Inc.", "0.2", "United States", "IT", "USD"),
                                       (ISIN_MSFT, "Microsoft", "0.2", "United States", "IT", "USD")]),
         fund("s", "SAP SE", "200", [(ISIN_SAP, "SAP SE", "1", "", "", "EUR")])],
     "cash": {"Verrechnungskonto": "5000"}, "base": "EUR"},
    {"name": "share classes with one key add up; a fund covering 90 % leaves 10 % unassigned",
     "holdings": [fund("f", "Fund", "100", [("US02079K3059", "ALPHABET INC CLASS A", "0.3", "US", "IT", "USD"),
                                            ("US02079K3059", "ALPHABET INC CLASS A", "0.2", "US", "IT", "USD"),
                                            (ISIN_NOVO, "NOVO NORDISK", "0.4", "DK", "Health Care", "DKK")])],
     "cash": {}, "base": "EUR"},
    {"name": "a line without an ISIN takes the ISIN of the same company elsewhere (par value, class)",
     "holdings": [fund("i", "Invesco Nasdaq", "300", [("~AAPL", "APPLE INC USD0.00001", "0.5", "", "IT", "USD"),
                                                      ("~XYZ", "Unknown Holdings Corp CLASS B", "0.5", "", "", "")]),
                  fund("p", "S&P ETF", "300", [(ISIN_APPLE, "Apple Inc.", "0.07", "US", "IT", "USD")])],
     "cash": {}, "base": "EUR"},
    {"name": "two ETFs overlapping by 70 %, foreign currency over its limit, cash over the deposit guarantee",
     "holdings": [fund("a", "A ETF", "600", [(ISIN_APPLE, "APPLE", "0.5", "US", "IT", "USD"),
                                             (ISIN_MSFT, "MSFT", "0.3", "US", "IT", "USD"),
                                             (ISIN_SAP, "SAP", "0.2", "DE", "IT", "EUR")]),
                  fund("b", "B ETF", "400", [(ISIN_APPLE, "APPLE", "0.6", "US", "IT", "USD"),
                                             (ISIN_MSFT, "MSFT", "0.2", "US", "IT", "USD"),
                                             (ISIN_NOVO, "NOVO", "0.2", "DK", "Health Care", "DKK")])],
     "cash": {"Tagesgeld": "120000", "Giro": "100"}, "base": "EUR"},
    {"name": "names with umlauts and quotes; a fund without a list is a stock of its own",
     "holdings": [fund("m", 'Müller "Welt" ETF', "100", [("DE000A0D9PT0", 'MTU "Aero"', "0.6", "Deutschland",
                                                          "Industrie", "EUR")]),
                  fund("x", "UBS ETF without a list", "100", [("LU0000000000", "UBS ETF without a list", "1", "", "",
                                                               "EUR")])],
     "cash": {}, "base": "EUR"},
    {"name": "an empty depot", "holdings": [], "cash": {}, "base": "EUR"},
]


def _holdings(case: dict) -> list[Holding]:
    return [Holding(h["id"], h["name"], D(h["value"]), tuple(Constituent(k, n, D(w), co, se, cu)
                                                            for k, n, w, co, se, cu in h["lines"]))
            for h in case["holdings"]]


def expected(case: dict) -> dict:
    """What xray.py computes for the case; numbers as strings (exact decimals)."""
    holdings = xray.unify(_holdings(case))
    rows = xray.exposures(holdings)
    funds = [h for h in holdings if len(h.constituents) > 1]
    limits = {k: v for k, v in xray.LIMITS.items() if k != "cost"}
    return {
        "unify": {h.id: [c.key for c in h.constituents] for h in holdings},
        "exposures": [{"key": r.key, "name": r.name, "share": str(r.share),
                       "via": {k: str(v) for k, v in sorted(r.via.items())}} for r in rows],
        "overlap": {f"{a.id}:{b.id}": str(xray.overlap(a, b)) for a, b in combinations(funds, 2)},
        "own_share": {h.id: str(xray.own_share(h, holdings)) for h in funds},
        "effective_count": str(xray.effective_count(rows)),
        "findings": [{"key": f.key, "value": str(f.value)} for f in
                     xray.findings(holdings, {**limits, "cost": xray.LIMITS["cost"]}, case["base"],
                                   {k: D(v) for k, v in case["cash"].items()})],
    }


def cases() -> dict:
    limits = {k: str(v) for k, v in xray.LIMITS.items() if k != "cost"}
    return {"schema": 1, "limits": limits, "cases": [{**c, "expect": expected(c)} for c in CASES]}


if __name__ == "__main__":
    json.dump(cases(), sys.stdout, ensure_ascii=False, indent=1)
    sys.stdout.write("\n")
