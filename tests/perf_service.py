"""Performance report (KENNZAHLEN.md): measured, compared with the reference values, never failing.

Not collected by ``pytest -q`` (no ``test_`` prefix); the CI job ``performance`` runs it on its own:
``pytest tests/perf_service.py -q``. The report goes to ``$GITHUB_STEP_SUMMARY`` (the run's summary page)
and to ``$PERF_REPORT`` (default ``perf-report.md``).
"""

from __future__ import annotations

import collections
import os
import statistics
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from brokersync.dedup import ExistingIndex
from brokersync.model import Kind, Transaction
from brokersync.wealthfolio import WealthfolioClient

from .fake_broker import ISIN, NAME, FakeBroker
from .fakes import FakeWealthfolio
from .test_sync import setup
from .test_ui_browser import PAGES, PHONE, UI_PASSWORD, browser, site  # noqa: F401 - fixtures

ROWS: list[tuple[str, str, str, str, str]] = []  # area, measure, value, reference, verdict


def record(area: str, measure: str, value: float, unit: str, good: float | None = None, source: str = "") -> None:
    verdict = "–" if good is None else ("✅" if value <= good else "⚠️")
    reference = "" if good is None else f"≤ {good:g} {unit} ({source})"
    ROWS.append((area, measure, f"{value:,.1f} {unit}", reference, verdict))


@pytest.fixture(scope="module", autouse=True)
def report():
    yield
    lines = ["## Performance (nur Bericht, hält die CI nicht an)", "",
             "| Bereich | Messung | Wert | Referenz | |", "|---|---|---|---|---|"]
    lines += [f"| {' | '.join(r)} |" for r in ROWS]
    text = "\n".join(lines) + "\n"
    print("\n" + text)
    Path(os.environ.get("PERF_REPORT", "perf-report.md")).write_text(text, encoding="utf-8")
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a", encoding="utf-8") as f:
            f.write(text)


# ── web UI: Core Web Vitals (lab values) of every page, at phone width ──────────────────────────────

VITALS = """() => new Promise(done => {
  const nav = performance.getEntriesByType('navigation')[0];
  let lcp = 0, cls = 0;
  new PerformanceObserver(l => l.getEntries().forEach(e => lcp = e.startTime))
    .observe({type: 'largest-contentful-paint', buffered: true});
  new PerformanceObserver(l => l.getEntries().forEach(e => { if (!e.hadRecentInput) cls += e.value; }))
    .observe({type: 'layout-shift', buffered: true});
  const bytes = performance.getEntriesByType('resource').reduce((s, r) => s + r.encodedBodySize, nav.encodedBodySize);
  setTimeout(() => done({ttfb: nav.responseStart - nav.requestStart, lcp, cls, bytes}), 300);
})"""


def test_web_vitals(site, browser):  # noqa: F811 - the fixtures imported above
    context = browser.new_context(viewport=PHONE)
    page = context.new_page()
    page.goto(site + "/login")
    page.fill("#pw", UI_PASSWORD)
    page.click("main form button")
    page.wait_for_url(site + "/")
    vitals = {}
    for path in PAGES:
        page.goto(site + path)
        vitals[path] = page.evaluate(VITALS)
    context.close()
    worst = {k: max(v[k] for v in vitals.values()) for k in ("ttfb", "lcp", "cls", "bytes")}
    slowest = max(vitals, key=lambda p: vitals[p]["ttfb"])
    # Core Web Vitals "good" (web.dev): TTFB ≤ 800 ms, LCP ≤ 2.5 s, CLS ≤ 0.1 - here on the CI runner, no network.
    record("Web-UI", f"TTFB, langsamste Seite ({slowest})", worst["ttfb"], "ms", 800, "Core Web Vitals")
    record("Web-UI", "TTFB, Median aller Seiten", statistics.median(v["ttfb"] for v in vitals.values()), "ms",
           200, "Nielsen 0,1 s / Server")
    record("Web-UI", "LCP, schlechteste Seite", worst["lcp"], "ms", 2500, "Core Web Vitals")
    record("Web-UI", "CLS, schlechteste Seite", worst["cls"], "", 0.1, "Core Web Vitals")
    record("Web-UI", "Übertragen, größte Seite (inkl. CSS, Schriften)", worst["bytes"] / 1024, "KiB", 500,
           "Seitengewicht")


# ── communication and data: one big sync run ────────────────────────────────────────────────────────

TRANSACTIONS = 300
EXISTING = 3000  # activities already in Wealthfolio (imports) the run has to check against


class BigBroker(FakeBroker):
    """The fake broker with a few hundred deposits and buys - for the measurement only."""

    def get_transactions(self, since: datetime | None) -> list[Transaction]:
        start = datetime(2026, 1, 5, 8, tzinfo=UTC) - timedelta(hours=TRANSACTIONS)
        txs = []
        for i in range(TRANSACTIONS):
            at = start + timedelta(hours=i)
            if i % 2:
                txs.append(Transaction(id=f"BIG-{i}", kind=Kind.BUY, datetime=at, currency="EUR",
                                       net=Decimal("101"), isin=ISIN, name=NAME, shares=Decimal("1"),
                                       gross=Decimal("100"), fee=Decimal("1"), label="TEST Kauf"))
            else:
                txs.append(Transaction(id=f"BIG-{i}", kind=Kind.DEPOSIT, datetime=at, currency="EUR",
                                       net=Decimal(100 + i), label="TEST Einzahlung"))
        return txs


def test_sync_run(tmp_path):
    wf = FakeWealthfolio()
    start = datetime(2026, 1, 5, tzinfo=UTC) - timedelta(hours=TRANSACTIONS)
    for i in range(EXISTING):  # imports in the same weeks that match nothing the broker sends
        wf.add_existing(accountId="acc-cash", activityType="DEPOSIT", amount=str(5000 + i), currency="EUR",
                        date=(start + timedelta(minutes=7 * i)).isoformat(), comment=f"TEST Import {i}")
    requests = []
    handle = wf.handle
    wf.handle = lambda r: (requests.append(r.url.path), handle(r))[1]
    syncer, _ = setup(tmp_path, wf)
    syncer.adapters["fake"] = BigBroker
    syncer.wealthfolio = lambda url, pw: WealthfolioClient(url, pw, transport=wf.transport())
    statements: list[str] = []
    syncer.state.db.set_trace_callback(statements.append)
    began = time.perf_counter()
    [result] = syncer.run()
    seconds = time.perf_counter() - began
    syncer.state.db.set_trace_callback(None)
    assert result.created, result  # the run worked; how fast is only reported

    record("Abruf", f"Dauer eines Abrufs ({TRANSACTIONS} Buchungen, {EXISTING} vorhandene)", seconds, "s", 10,
           "Nielsen 10 s, dann Fortschritt")
    record("Kommunikation", "Wealthfolio-Aufrufe je Buchung", len(requests) / TRANSACTIONS, "", 4,
           "eigenes Budget: Anlegen + Übertrag")
    reads = collections.Counter(p.removeprefix("/api/v1/") for p in requests if p != "/api/v1/activities")
    ROWS.append(("Kommunikation", "Lesende Aufrufe (wachsen mit den vorhandenen, nicht mit neuen Buchungen)",
                 ", ".join(f"{k} {n}" for k, n in reads.most_common()), "kein Aufruf je Buchung (N+1)", "–"))
    record("Datenbasis", "SQL-Anweisungen je Buchung", len(statements) / TRANSACTIONS, "", 10, "eigenes Budget")
    scans = sorted({s.split()[0] + " … " + _table(s) for s in statements
                    if s.lstrip().upper().startswith(("SELECT", "UPDATE", "DELETE")) and _scans(syncer, s)})
    record("Datenbasis", "Abfragen ohne Index (volle Tabellensuche)", len(scans), "", 0, "EXPLAIN QUERY PLAN")
    ROWS.extend(("Datenbasis", f"ohne Index: `{s}`", "", "", "⚠️") for s in scans)


def _table(sql: str) -> str:
    words = sql.replace("\n", " ").split()
    for keyword in ("FROM", "UPDATE"):
        if keyword in (w.upper() for w in words):
            i = [w.upper() for w in words].index(keyword)
            return words[i + 1] if i + 1 < len(words) else ""
    return ""


def _scans(syncer, sql: str) -> bool:
    try:
        plan = syncer.state.db.execute("EXPLAIN QUERY PLAN " + sql).fetchall()
    except Exception:  # a statement with parameters already bound into the text that EXPLAIN can't take
        return False
    return any("SCAN" in str(row[-1]) and "USING" not in str(row[-1]) for row in plan)


# ── the matching against imports, alone ─────────────────────────────────────────────────────────────

def test_dedup_index():
    base = datetime(2026, 1, 1, tzinfo=UTC)
    existing = [{"id": f"e{i}", "accountId": "acc-cash", "activityType": "DEPOSIT",
                 "date": (base + timedelta(minutes=i)).isoformat(), "amount": str(1000 + i), "currency": "EUR",
                 "comment": "TEST Import"} for i in range(EXISTING)]
    index = ExistingIndex(existing)
    times = []
    for i in range(200):
        payload = {"accountId": "acc-cash", "activityType": "DEPOSIT", "amount": str(1000 + i), "currency": "EUR",
                   "activityDate": (base + timedelta(minutes=i)).isoformat(), "comment": f"x [SYNC fake:{i}]"}
        began = time.perf_counter()
        index.find(f"[SYNC fake:{i}]", [payload])
        times.append((time.perf_counter() - began) * 1000)
    record("Duplikat-Erkennung", f"Suche je Buchung bei {EXISTING} vorhandenen", statistics.median(times), "ms",
           10, "eigenes Budget")
