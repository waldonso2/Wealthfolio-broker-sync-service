"""Depot transactions over FinTS (HKWDU / MT536) - fabricated answers, no real depot data."""

import logging
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D

from brokersync.adapters import fints as fints_mod
from brokersync.adapters import mt536
from brokersync.adapters.dkb import DkbAdapter

from .test_fints import PROFILES, RECORDING, Bpd, GiroAndDepot

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
        return [ANSWER]


def test_the_dkb_logs_its_depot_transactions_without_booking_them(monkeypatch, caplog):
    monkeypatch.setattr(fints_mod.FintsAdapter, "client_factory",
                        staticmethod(lambda *a: GiroAndDepotWithMovements(RECORDING, sca=False)))
    a = DkbAdapter(PROFILES[DkbAdapter])
    a.login()
    caplog.set_level(logging.INFO, logger="brokersync.adapters.fints")
    txs = a.get_transactions(datetime(2026, 8, 1, tzinfo=UTC))
    first = datetime.now(fints_mod.BERLIN).date() - timedelta(days=fints_mod.NO_TAN_DAYS)
    assert ("movements", "0000999888", first) in a._client.calls  # the last 89 days, not just the run's window
    assert f"Depotumsätze von „maxblue Depot“ (Art 30) seit {first} (Test, nichts gebucht): 4 - " in caplog.text
    assert "Kauf 1" in caplog.text and "LU0000000001" not in caplog.text and "0000999888" not in caplog.text
    assert not any(t.isin for t in txs)  # nothing booked from them yet


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
