"""Depot transactions over FinTS (HKWDU / MT536) - fabricated answers, no real depot data."""

import json
import logging
from collections import Counter
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

from brokersync import config as config_mod
from brokersync.adapters import fints as fints_mod
from brokersync.adapters import mt536
from brokersync.adapters.deutschebank import DeutscheBankAdapter
from brokersync.adapters.dkb import DkbAdapter
from brokersync.mapping import Accounts, to_activities
from brokersync.model import Kind, Transaction
from brokersync.sync import UNMATCHED_SECURITIES, Syncer
from brokersync.vault import Vault
from brokersync.wealthfolio import WealthfolioClient

from .fake_fints import FakeFinTS
from .fakes import PASSWORD, FakeWealthfolio
from .test_fints import PROFILES, RECORDING, Bpd, GiroAndDepot
from .test_sync import RecordingNotifier

ANSWER = """
:16R:GENL
:28E:1/ONLY
:13A::STAT//536
:20C::SEME//NONREF
:23G:NEWM
:98C::PREP//20261011120000
:69A::STAT//20260714/20261011
:97A::SAFE//12030000/0000999888
:22F::CODE//COMP
:22F::STBA//SETT
:17B::ACTI//Y
:16S:GENL
:16R:SUBSAFE
:16R:FIN
:35B:ISIN LU0000000001
/DE/A0TEST
TEST WORLD FONDS
:93B::FIOP//UNIT/10,
:93B::FICL//UNIT/12,5
:16R:TRAN
:16R:LINK
:20C::RELA//000012345678
:16S:LINK
:16R:TRANSDET
:36B::PSTA//UNIT/2,5
:19A::PSTA//NEUR251,9
:22F::TRAN//SETT
:22H::REDE//RECE
:22H::PAYM//APMT
:98A::ESET//20260903
:98A::SETT//20260905
:16S:TRANSDET
:16S:TRAN
:16S:FIN
:16R:FIN
:35B:ISIN DE0000000002
TEST AKTIE
:16R:TRAN
:16R:TRANSDET
:36B::PSTA//UNIT/7,
:19A::PSTA//EUR699,
:22F::TRAN//SETT
:22H::REDE//DELI
:22H::PAYM//APMT
:98C::ESET//20260910153000
:16S:TRANSDET
:16S:TRAN
:16R:TRAN
:16R:TRANSDET
:36B::PSTA//UNIT/7,
:22F::TRAN//SETT
:22H::REDE//RECE
:22H::PAYM//FREE
:98A::ESET//20260801
:16S:TRANSDET
:16S:TRAN
:16R:TRAN
:16R:TRANSDET
:36B::PSTA//UNIT/1,
:22F::TRAN//CORP
:22H::REDE//RECE
:22H::PAYM//FREE
:98A::ESET//20260820
:25D::MOVE//REVE
:16S:TRANSDET
:16S:TRAN
:16S:FIN
:16S:SUBSAFE
-""".replace("\n", "\r\n")


def test_movements_are_read_from_mt536():
    buy, sell, delivery, reversal = mt536.parse(ANSWER)
    assert (buy.isin, buy.name, buy.shares, buy.amount, buy.currency, buy.trade_date, buy.settle_date) == \
        ("LU0000000001", "TEST WORLD FONDS", D("2.5"), D("-251.9"), "EUR", date(2026, 9, 3), date(2026, 9, 5))
    assert (buy.what, buy.reference) == ("Kauf", "000012345678")
    assert (sell.isin, sell.shares, sell.amount, sell.trade_date, sell.what) == \
        ("DE0000000002", D("7"), D("699"), date(2026, 9, 10), "Verkauf")
    assert (delivery.what, delivery.amount) == ("Eingang ohne Zahlung", None)
    assert reversal.what == "Storno" and reversal.reversal


def test_summary_and_structure_show_no_values():
    text = mt536.summary(mt536.parse(ANSWER)) + " " + mt536.structure([ANSWER])
    assert "Kauf 1" in text and "Verkauf 1" in text and "Storno 1" in text and "Stückzahl 4/4" in text
    assert "Beträge negativ 1, positiv 1" in text
    assert ":22H::REDE//RECE×3" in text and ":22H::PAYM//APMT×2" in text and ":19A::PSTA//×2" in text
    assert ":36B::PSTA//UNIT×4" in text and ":16R:TRANSDET×4" in text
    for value in ("LU0000000001", "A0TEST", "TEST", "251", "699", "0000999888", "12030000", "20260903",
                  "000012345678"):
        assert value not in text
    assert mt536.summary([]) == "keine"


def test_the_request_is_built_for_the_depot(monkeypatch):
    seg = mt536.segments()

    class Client:
        sent = None

        @contextmanager
        def _get_dialog(self):
            yield "dialog"

        def _find_highest_supported_command(self, *classes):
            return classes[0]

        def _fetch_with_touchdowns(self, dialog, factory, processor, name):
            Client.sent = factory(None)
            return processor([seg["HIWDU5"](transactions=ANSWER.encode("latin-1"))])

    depot = fints_mod._sepa_account("0000999888", None, "12030000", "XXXXDEXXXXX")
    [text] = mt536.fetch(Client(), depot, date(2026, 7, 14), date(2026, 10, 11))
    assert text == ANSWER
    from fints.parser import FinTS3Serializer

    Client.sent.header.number = 3
    assert FinTS3Serializer().serialize_message([Client.sent]) == \
        b"HKWDU:3:5+0000999888::280:12030000+N++20260714+20261011'"


class GiroAndDepotWithMovements(GiroAndDepot):
    bpd = Bpd({"HIWPDS", "HIWDUS"})
    tan_for_movements = False

    def _need_twostep_tan_for_segment(self, seg):
        return self.tan_for_movements

    def get_depot_transactions(self, account, start, end):
        self.calls.append(("movements", account.accountnumber, start))
        return [ANSWER] if account.accountnumber == "0000999888" else []


def test_the_dkb_books_its_depot_transactions(monkeypatch, caplog):
    monkeypatch.setattr(fints_mod.FintsAdapter, "client_factory",
                        staticmethod(lambda *a: GiroAndDepotWithMovements(RECORDING, sca=False)))
    a = DkbAdapter(PROFILES[DkbAdapter])
    a.login()
    caplog.set_level(logging.INFO, logger="brokersync.adapters.fints")
    txs = a.get_transactions(datetime.now(UTC) - timedelta(days=3))  # within the 89 days, whatever today is
    first = datetime.now(fints_mod.BERLIN).date() - timedelta(days=fints_mod.NO_TAN_DAYS)
    assert ("movements", "0000999888", first) in a._client.calls  # the last 89 days, not just the run's window
    assert f"Depotumsätze von „maxblue Depot“ (Art 30) seit {first}: 4 - " in caplog.text
    assert "Kauf 1" in caplog.text and "LU0000000001" not in caplog.text and "0000999888" not in caplog.text
    # All before the giro history read (3 days): their money came from outside it.
    assert [(t.kind, t.isin, t.external_cash) for t in txs if t.id.startswith("mt536-")] == [
        (Kind.BUY, "LU0000000001", True), (Kind.SELL, "DE0000000002", True),
        (Kind.UNKNOWN, "DE0000000002", False), (Kind.UNKNOWN, "DE0000000002", False)]


def test_other_fints_banks_only_log_their_depot_transactions(monkeypatch, caplog):
    monkeypatch.setattr(fints_mod.FintsAdapter, "client_factory",
                        staticmethod(lambda *a: GiroAndDepotWithMovements(RECORDING, sca=False)))
    a = DeutscheBankAdapter(PROFILES[DeutscheBankAdapter])  # its depot trades come from the CSV export
    a.login()
    caplog.set_level(logging.INFO, logger="brokersync.adapters.fints")
    txs = a.get_transactions(None)
    assert "(nicht gebucht): 4 - " in caplog.text
    assert not any(t.id.startswith("mt536-") for t in txs)


def test_depot_transactions_that_need_a_tan_are_not_asked_for(monkeypatch, caplog):
    client = GiroAndDepotWithMovements(RECORDING, sca=False)
    client.tan_for_movements = True
    monkeypatch.setattr(fints_mod.FintsAdapter, "client_factory", staticmethod(lambda *a: client))
    a = DkbAdapter(PROFILES[DkbAdapter])
    a.login()
    caplog.set_level(logging.INFO, logger="brokersync.adapters.fints")
    a.get_transactions(None)
    assert "Depotumsätze verlangen eine Freigabe in der App - nicht abgerufen" in caplog.text
    assert not [c for c in client.calls if c[0] == "movements"]


def test_a_failing_request_never_stops_the_run(monkeypatch, caplog):
    class Broken(GiroAndDepotWithMovements):
        def get_depot_transactions(self, account, start, end):
            raise ValueError("Error response: Response(code='9010', reference_element=None, text='Nicht erlaubt')")

    monkeypatch.setattr(fints_mod.FintsAdapter, "client_factory",
                        staticmethod(lambda *a: Broken(RECORDING, sca=False)))
    a = DkbAdapter(PROFILES[DkbAdapter])
    a.login()
    caplog.set_level(logging.INFO, logger="brokersync.adapters.fints")
    assert a.get_transactions(None)
    assert "nicht lesbar: 9010 Nicht erlaubt" in caplog.text


# ── booking (#130) ──────────────────────────────────────────────────────────
def giro(day: str, amount: str, purpose: str = "") -> Transaction:
    d = date.fromisoformat(day)
    value = D(amount)
    return Transaction(id=f"g-{day}-{amount}", kind=Kind.SECURITIES_CASH, currency="EUR", net=abs(value),
                       signed=value, label="Wertpapierabrechnung", text=purpose,
                       datetime=datetime(d.year, d.month, d.day, 12, tzinfo=fints_mod.BERLIN).astimezone(UTC))


TODAY = date(2026, 10, 30)


def test_a_trade_takes_the_amount_of_its_giro_booking_and_a_fee_only_with_a_price():
    buy, sell, delivery, reversal = mt536.parse(ANSWER)
    buy.price = D("100.76")  # 2.5 × 100.76 = 251.90 → the debit's 1.50 more is the fee
    txs = mt536.to_transactions([buy, sell, delivery, reversal],
                                [giro("2026-09-05", "-253.40", "LU0000000001"), giro("2026-09-12", "695.10"),
                                 giro("2026-09-12", "-695.10")], date(2026, 8, 1), TODAY)
    b, s, d, r = txs
    assert (b.kind, b.net, b.gross, b.fee, b.shares, b.external_cash) == (Kind.BUY, D("253.40"), D("251.90"),
                                                                         D("1.50"), D("2.5"), False)
    # No price: the credit is the amount, nothing guessed as fee (fee and tax of a sale can't be told apart).
    assert (s.kind, s.net, s.gross, s.fee) == (Kind.SELL, D("695.10"), D("695.10"), D(0))
    assert d.kind == r.kind == Kind.UNKNOWN
    assert d.raw == {"buchungstext": "Eingang ohne Zahlung", "isin": "DE0000000002", "Bezeichnung": "TEST AKTIE",
                     "stück": "7", "handelstag": "2026-08-01", "hinweis": "REDE//RECE PAYM//FREE TRAN//SETT"}
    assert r.raw["hinweis"].endswith("TRAN//CORP MOVE//REVE")
    # Ids: stable, without depot number or reference.
    again = mt536.to_transactions(mt536.parse(ANSWER), [], date(2026, 8, 1), TODAY)
    assert [t.id for t in again] == [t.id for t in txs]
    assert b.id.startswith("mt536-20260903-") and b.id.endswith("-1")
    assert not any("0000999888" in t.id or "000012345678" in t.id for t in txs)
    for t in (b, s):
        to_activities(t, "dkb", Accounts("cash", "depot"))  # bookable


def test_settlement_needs_sign_window_and_plausible_costs():
    buy = mt536.parse(ANSWER)[0]  # 2026-09-03, posted -251.90, no price
    book = lambda *g: mt536.to_transactions([buy], list(g), date(2026, 8, 1), TODAY)[0]  # noqa: E731
    assert book(giro("2026-09-05", "251.90")).net == D("251.90")  # a credit doesn't pay a buy: posted amount
    assert book(giro("2026-09-10", "-251.90")).net == D("251.90")  # 7 days later: not its booking
    assert book(giro("2026-09-05", "-400.00")).net == D("251.90")  # costs over half the value
    assert book(giro("2026-09-05", "-250.00")).net == D("251.90")  # less than the posted amount
    paid = book(giro("2026-09-02", "-252.40"))
    assert (paid.net, paid.gross, paid.fee) == (D("252.40"), D("252.40"), D(0))
    # Two candidates: the one naming the ISIN first, then the smaller costs.
    many = [giro("2026-09-04", "-251.90"), giro("2026-09-05", "-252.90", "Kauf LU0000000001")]
    assert book(*many).net == D("252.90")


def test_a_young_trade_waits_for_its_giro_booking_an_old_one_counts_on_its_posted_amount():
    buy = mt536.parse(ANSWER)[0]  # 2026-09-03
    assert mt536.to_transactions([buy], [], date(2026, 8, 1), date(2026, 9, 6)) == []
    [late] = mt536.to_transactions([buy], [], date(2026, 8, 1), date(2026, 9, 10))
    assert (late.net, late.external_cash) == (D("251.9"), False)
    # Before the giro history: its money never showed up there, no wait.
    [early] = mt536.to_transactions([buy], [], date(2026, 9, 4), date(2026, 9, 4))
    assert early.external_cash


def test_the_sync_history_read_before_counts_too(monkeypatch):
    monkeypatch.setattr(fints_mod.FintsAdapter, "client_factory",
                        staticmethod(lambda *a: GiroAndDepotWithMovements(RECORDING, sca=False)))
    a = DkbAdapter(PROFILES[DkbAdapter])
    a.login()
    a.history_read = date(2026, 1, 1)  # read in an earlier run: the giro booking was there
    txs = a.get_transactions(datetime.now(UTC) - timedelta(days=3))
    assert not any(t.external_cash for t in txs if t.id.startswith("mt536-"))


def test_unreadable_movements_never_stop_the_giro(monkeypatch, caplog):
    monkeypatch.setattr(fints_mod.FintsAdapter, "client_factory",
                        staticmethod(lambda *a: GiroAndDepotWithMovements(RECORDING, sca=False)))
    monkeypatch.setattr(mt536, "to_transactions", lambda *a: 1 / 0)
    a = DkbAdapter(PROFILES[DkbAdapter])
    a.login()
    caplog.set_level(logging.INFO, logger="brokersync.adapters.fints")
    txs = a.get_transactions(datetime(2026, 8, 1, tzinfo=UTC))
    assert txs and not any(t.id.startswith("mt536-") for t in txs)
    assert "Depotumsätze nicht gebucht: ZeroDivisionError" in caplog.text


DEPOT = json.loads((Path(__file__).parent / "contract" / "dkb" / "depot.json").read_text())["recording"]


def depot_sync(tmp_path, monkeypatch, wf):
    monkeypatch.setattr(DkbAdapter, "client_factory", staticmethod(lambda *a: FakeFinTS(DEPOT, sca=False)))
    cfg = config_mod.Config(wealthfolio_url="http://wf", public_url="http://sync:8090")
    cfg.brokers["dkb"] = config_mod.BrokerConfig(enabled=True, cash_account_id="acc-cash",
                                                 portfolio_account_id="acc-depot", start_date="2026-08-25")
    config_mod.save(tmp_path, cfg)
    v = Vault(tmp_path)
    v.update(lambda d: d.__setitem__("wealthfolio_password", PASSWORD))
    v.set_broker_credentials("dkb", PROFILES[DkbAdapter])
    return Syncer(tmp_path, wealthfolio=lambda u, p: WealthfolioClient(u, p, transport=wf.transport()),
                  notifier=RecordingNotifier(), adapters={"dkb": DkbAdapter})


def test_a_run_books_buy_and_sell_and_matches_their_giro_bookings(tmp_path, monkeypatch):
    wf = FakeWealthfolio()
    syncer = depot_sync(tmp_path, monkeypatch, wf)
    [r] = syncer.run()
    # Salary, the buy before the giro history (its money from outside), buy, sell; delivery, corporate
    # action and reversal unknown; both giro settlements matched in this run.
    assert (r.status, r.created, r.existing, r.unknown) == ("ok", 4, 2, 3)
    unknown = syncer.state.unknown_events()
    assert UNMATCHED_SECURITIES not in [e["raw_type"] for e in unknown]
    assert sorted(e["raw_type"] for e in unknown) == ["MT536 Eingang ohne Zahlung", "MT536 Kapitalmaßnahme",
                                                       "MT536 Storno"]
    trades = [a for a in wf.activities if a["activityType"] in ("BUY", "SELL")]
    assert sorted((a["activityType"], a["amount"]) for a in trades) == [("BUY", "100"), ("BUY", "253.4"),
                                                                        ("SELL", "695.1")]
    transfers = [a for a in wf.activities if a["activityType"].startswith("TRANSFER")]
    groups = Counter(a["sourceGroupId"] for a in transfers)
    assert len(groups) == 3 and set(groups.values()) == {2}
    sign = {"DEPOSIT": 1, "TRANSFER_IN": 1, "WITHDRAWAL": -1, "TRANSFER_OUT": -1}
    cash = sum(sign[a["activityType"]] * D(a["amount"]) for a in wf.activities if a["accountId"] == "acc-cash")
    assert cash == D("2000") - D("253.40") + D("695.10") + D("100") - D("100")
    [r2] = syncer.run()
    assert (r2.created, r2.failed) == (0, 0)


def test_a_trade_imported_from_the_pdf_is_recognised(tmp_path, monkeypatch):
    wf = FakeWealthfolio()
    # The addon's PDF import of the buy of 3 September: BUY and its transfer pair.
    for account, kind, amount in (("acc-cash", "TRANSFER_OUT", "253.4"), ("acc-depot", "TRANSFER_IN", "253.4")):
        wf.add_existing(accountId=account, activityType=kind, date="2026-09-03T13:10:00.000Z", quantity="1",
                        unitPrice="1", amount=amount, currency="EUR", comment="Funds for buy [PDF 7]",
                        assetSymbol="", assetId="")
    wf.add_existing(accountId="acc-depot", activityType="BUY", date="2026-09-03T13:10:00.000Z", quantity="2.5",
                    unitPrice="100.76", amount="253.4", fee="1.5", currency="EUR", comment="Kauf [PDF 7]",
                    assetSymbol="WORLD", assetId="WORLD")
    syncer = depot_sync(tmp_path, monkeypatch, wf)
    [r] = syncer.run()
    # Salary, older buy, sale created; the buy and its giro debit found in the import.
    assert (r.status, r.created, r.existing, r.failed) == ("ok", 3, 3, 0)
    assert sum(1 for a in wf.activities if a["activityType"] == "BUY") == 2  # the PDF's and the older one
    assert UNMATCHED_SECURITIES not in [e["raw_type"] for e in syncer.state.unknown_events()]
