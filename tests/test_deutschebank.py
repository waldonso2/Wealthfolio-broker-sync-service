"""Deutsche Bank: the giro account over FinTS (camt when MT940 is empty) and the depot from its CSV export.

All data fabricated: TEST securities, invented depot and account numbers."""

import re
from datetime import date, timedelta
from decimal import Decimal as D

from fastapi.testclient import TestClient

from brokersync import config as config_mod
from brokersync.adapters import deutschebank_csv
from brokersync.adapters.deutschebank import DeutscheBankAdapter
from brokersync.mapping import Accounts, to_activities
from brokersync.model import Kind
from brokersync.sync import UNMATCHED_SECURITIES, Syncer
from brokersync.vault import Vault
from brokersync.wealthfolio import WealthfolioClient

from .fake_fints import FakeFinTS
from .fakes import PASSWORD, FakeWealthfolio
from .test_sync import RecordingNotifier

HEADER = ("Buchungsdatum;Schlusstag / Zahltag;Valuta;Depot-Nr.;Konto-Nr.;Umsatzart;Nominal / Stück;Bezeichnung;WKN;"
          "ISIN;Handelswährung;Kurs;Devisenkurs;Stückzinsen;Kapitalertragssteuer inkl. Solidaritätszuschlag;"
          "Ausländische Quellensteuer;Vergütete Steuern;Transaktionspreis inkl. sonstiger Kosten;Eigene Spesen;"
          "Fremde Spesen;Ausmachender Betrag;Depot-/Portfolioname")
TODAY = date.today()
RECENT = TODAY - timedelta(days=10)
BUY_DAY = TODAY - timedelta(days=5)


def row(day, kind, shares, isin, amount, *, kurs="", kapest="0,00", spesen="0,00", konto="999 9999999 00 (EUR)"):
    return (f"{day};{day if kind != 'Dividende/Ausschüttung' else ''};{day};999 9999999 00;{konto};{kind};{shares};"
            f"TEST AG NAMENS-AKTIEN O.N.;TEST01;{isin};EUR;{kurs};1,00;0,00;{kapest};0,00;0,00;;{spesen};0,00;"
            f"{amount};PrivatDepot TEST")


CSV = "\n".join([
    HEADER,
    row(RECENT, "Dividende/Ausschüttung", "156,00", "DE000TEST001", "402,00", kapest="-144,00"),
    row(BUY_DAY, "Kauf", "10,00", "DE000TEST001", "-1.001,90", kurs="100,00", spesen="-1,90"),
    row("2025-03-28", "Eingang", "35,00", "DE000TEST001", "-1.793,40", kurs="51,24"),
    row("2024-05-14", "Dividende/Ausschüttung", "88,00", "DE000TEST001", "343,39", kapest="-123,01"),
    row("2021-12-10", "Kapitaltransaktion", "11,00", "DE000TEST002", "0,00", konto=""),
    row("2021-12-10", "Kapitaltransaktion", "11,00", "DE000TEST002", "0,00", konto=""),
]).encode()

GIRO = {
    "accounts": [{"iban": "DE02100700000000202051"}],
    "balance": {"amount": "1000.00", "currency": "EUR"},
    "transactions": [
        {"date": RECENT.isoformat(), "entry_date": RECENT.isoformat(), "amount": "402.00", "currency": "EUR",
         "posting_text": "Ertragsgutschrift", "purpose": "TEST AG Dividende", "applicant_name": "",
         "applicant_iban": ""},
        {"date": BUY_DAY.isoformat(), "entry_date": BUY_DAY.isoformat(), "amount": "-1001.90", "currency": "EUR",
         "posting_text": "Wertpapierkauf", "purpose": "Depot 999 TEST AG", "applicant_name": "",
         "applicant_iban": ""},
        {"date": BUY_DAY.isoformat(), "entry_date": BUY_DAY.isoformat(), "amount": "-50.00", "currency": "EUR",
         "posting_text": "Kartenzahlung", "purpose": "Supermarkt", "applicant_name": "Markt", "applicant_iban": ""},
    ],
}
CREDS = {"username": "1234567890", "pin": "1234", "blz": "10070000", "product_id": "TESTPRODUCT"}


def test_the_csv_is_read_without_account_numbers():
    rows = deutschebank_csv.parse(CSV)
    assert len(rows) == 6
    assert all("Depot-Nr." not in r and "Konto-Nr." not in r and "Depot-/Portfolioname" not in r for r in rows)
    assert "999 9999999" not in repr(rows)
    assert deutschebank_csv.parse(b"\xef\xbb\xbf" + CSV) == rows  # with BOM
    assert deutschebank_csv.parse(CSV.decode().encode("cp1252")) == rows


def test_rows_become_transactions():
    txs = deutschebank_csv.transactions(deutschebank_csv.parse(CSV), history_start=TODAY - timedelta(days=89))
    div, buy, delivery, old_div, cap1, cap2 = txs
    assert (div.kind, div.net, div.tax, div.gross, div.shares, div.external_cash) == \
        (Kind.DIVIDEND, D("402.00"), D("144.00"), D("546.00"), D("156.00"), False)
    assert (buy.kind, buy.net, buy.gross, buy.fee, buy.shares, buy.external_cash) == \
        (Kind.BUY, D("1001.90"), D("1000.00"), D("1.90"), D("10.00"), False)
    # Employee shares: delivered against payment, not paid from the giro account.
    assert (delivery.kind, delivery.net, delivery.gross, delivery.external_cash) == \
        (Kind.BUY, D("1793.40"), D("1793.40"), True)
    # Older than the giro history read: its money never shows up on the cash account.
    assert (old_div.kind, old_div.external_cash) == (Kind.DIVIDEND, True)
    assert cap1.kind == cap2.kind == Kind.UNKNOWN and cap1.id != cap2.id and cap1.raw_type == "Kapitaltransaktion"
    assert "999" not in repr(cap1.raw)
    # Stable ids: the same export again gives the same ids.
    again = deutschebank_csv.transactions(deutschebank_csv.parse(CSV), history_start=None)
    assert [t.id for t in again] == [t.id for t in txs]


def test_a_dividend_paid_outside_the_cash_accounts_history_leaves_no_money_there():
    old = deutschebank_csv.transactions(deutschebank_csv.parse(CSV), history_start=TODAY)[3]
    payloads = to_activities(old, "deutschebank", Accounts("cash", "depot"))
    assert [p["activityType"] for p in payloads] == ["DIVIDEND", "TRANSFER_OUT", "TRANSFER_IN", "WITHDRAWAL"]
    cash = sum((1 if p["activityType"] == "TRANSFER_IN" else -1) * D(p["amount"])
               for p in payloads if p["accountId"] == "cash")
    assert cash == 0


def setup(tmp_path, monkeypatch, recording=None, client=None):
    monkeypatch.setattr(DeutscheBankAdapter, "client_factory",
                        staticmethod(lambda *a: (client or FakeFinTS)(recording or GIRO, sca=False)))
    cfg = config_mod.Config(wealthfolio_url="http://wf", public_url="https://sync:8443")
    cfg.brokers["deutschebank"] = config_mod.BrokerConfig(enabled=True, cash_account_id="acc-cash",
                                                          portfolio_account_id="acc-depot")
    config_mod.save(tmp_path, cfg)
    v = Vault(tmp_path)
    v.update(lambda d: d.__setitem__("wealthfolio_password", PASSWORD))
    v.set_broker_credentials("deutschebank", CREDS)
    wf = FakeWealthfolio()
    make_wf = lambda u, p: WealthfolioClient(u, p, transport=wf.transport())  # noqa: E731
    return wf, make_wf


def test_upload_and_run_book_the_depot_and_match_the_giro_in_one_run(tmp_path, monkeypatch):
    from brokersync.web.app import create_app

    wf, make_wf = setup(tmp_path, monkeypatch)
    app = create_app(tmp_path, wealthfolio=make_wf, adapters={"deutschebank": DeutscheBankAdapter},
                     notifier=RecordingNotifier(), run_in_thread=False)
    client = TestClient(app, base_url="https://sync.local")
    client.post("/setup-password", data={"password": "geheim-123456", "password2": "geheim-123456"})
    page = client.get("/brokers/deutschebank").text
    assert "Depotumsätze (CSV-Export aus dem Online-Banking)" in page
    token = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    r = client.post("/brokers/deutschebank/statement", data={"csrf": token},
                    files={"file": ("Umsaetze.csv", CSV, "text/csv")})
    assert "6 Zeilen gelesen (2 Dividenden, 2 Käufe/Einbuchungen, 2 nicht automatisch buchbar), 6 davon neu" in r.text
    r = client.post("/brokers/deutschebank/statement", data={"csrf": token},
                    files={"file": ("Umsaetze.csv", CSV, "text/csv")})
    assert "0 davon neu" in r.text  # the same export again adds nothing
    r = client.post("/brokers/deutschebank/statement", data={"csrf": token},
                    files={"file": ("x.csv", b"a;b\n1;2", "text/csv")})
    assert "kein Depotumsatz-Export" in r.text

    syncer = Syncer(tmp_path, wealthfolio=make_wf, notifier=RecordingNotifier(),
                    adapters={"deutschebank": DeutscheBankAdapter})
    [res] = syncer.run()
    assert res.status == "ok"
    # Card payment, 2 dividends, purchase, employee shares; both giro settlements matched in this very run.
    assert res.created == 5 and res.unknown == 2
    assert not [e for e in syncer.state.unknown_events() if e["raw_type"] == UNMATCHED_SECURITIES]
    by_type = sorted(a["activityType"] for a in wf.activities)
    assert by_type.count("DIVIDEND") == 2 and by_type.count("BUY") == 2 and by_type.count("DEPOSIT") == 1
    cash = D(0)
    sign = {"DEPOSIT": 1, "TRANSFER_IN": 1, "WITHDRAWAL": -1, "TRANSFER_OUT": -1}
    for a in wf.activities:
        if a["accountId"] == "acc-cash":
            cash += sign[a["activityType"]] * D(a["amount"])
    # On the cash account only what the giro account read shows: dividend +402, purchase -1001.90, card -50.
    assert cash == D("402.00") - D("1001.90") - D("50.00")
    [res2] = syncer.run()
    assert res2.created == 0 and res2.failed == 0


class CamtOnly(FakeFinTS):
    """A bank that answers MT940 with nothing and has the bookings as camt."""

    def get_transactions(self, account, start_date=None, end_date=None):
        return []

    def get_information(self):
        from .test_fints import Op

        return {"bank": {"supported_operations": {Op("GET_TRANSACTIONS"): True, Op("GET_TRANSACTIONS_XML"): True}},
                "accounts": [{"iban": "DE02100700000000202051", "type": 1, "product_name": "AktivKonto"}]}

    def get_transactions_xml(self, account, start_date=None, end_date=None):
        return [CAMT.replace(b"DAY", RECENT.isoformat().encode())], []


CAMT = b"""<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.052.001.08"><BkToCstmrAcctRpt><GrpHdr><MsgId>1</MsgId></GrpHdr>
<Rpt><Id>1</Id><Acct><Id><IBAN>DE02100700000000202051</IBAN></Id><Ccy>EUR</Ccy></Acct>
<Ntry><Amt Ccy="EUR">2500.00</Amt><CdtDbtInd>CRDT</CdtDbtInd><BookgDt><Dt>DAY</Dt></BookgDt><ValDt><Dt>DAY</Dt></ValDt>
<NtryDtls><TxDtls><RltdPties><Dbtr><Pty><Nm>Muster GmbH</Nm></Pty></Dbtr></RltdPties>
<RmtInf><Ustrd>Gehalt</Ustrd></RmtInf></TxDtls></NtryDtls><AddtlNtryInf>SEPA-Gutschrift</AddtlNtryInf></Ntry>
</Rpt></BkToCstmrAcctRpt></Document>"""


def test_camt_when_the_bank_sends_no_mt940(tmp_path, monkeypatch, caplog):
    import logging

    caplog.set_level(logging.INFO)
    adapter = DeutscheBankAdapter(CREDS)
    monkeypatch.setattr(DeutscheBankAdapter, "client_factory", staticmethod(lambda *a: CamtOnly(GIRO, sca=False)))
    adapter.login()
    [tx] = adapter.get_transactions(None)
    assert (tx.kind, tx.net, tx.label, tx.counterparty, tx.text) == \
        (Kind.DEPOSIT, D("2500.00"), "SEPA-Gutschrift", "Muster GmbH", "Gehalt")
    assert "1 Umsätze per camt" in caplog.text and "Umsätze und Saldo von „AktivKonto“" in caplog.text
    assert adapter.history_start == TODAY - timedelta(days=89)


def test_camt_requests_name_the_account_nationally_too(monkeypatch):
    from fints.formals import KTI1
    from fints.models import SEPAAccount

    sent = []

    class NeedsNational(CamtOnly):
        def get_sepa_accounts(self):
            return [SEPAAccount(iban="DE02100700000000202051", bic="DEUTDEFFXXX", accountnumber="0202051",
                                subaccount="00", blz="10070000")]

        def get_transactions_xml(self, account, start_date=None, end_date=None):
            sent.append(KTI1.from_sepa_account(account))  # what python-fints puts into HKCAZ
            return super().get_transactions_xml(account, start_date, end_date)

    monkeypatch.setattr(DeutscheBankAdapter, "client_factory",
                        staticmethod(lambda *a: NeedsNational(GIRO, sca=False)))
    adapter = DeutscheBankAdapter(CREDS)
    adapter.login()
    assert len(adapter.get_transactions(None)) == 1
    [kti] = sent
    assert (kti.iban, kti.account_number, kti.subaccount_number, kti.bank_identifier.bank_code) == \
        ("DE02100700000000202051", "0202051", "00", "10070000")
    # Only for that request: python-fints' own behaviour is back afterwards.
    assert KTI1.from_sepa_account(NeedsNational(GIRO).get_sepa_accounts()[0]).account_number is None


def test_a_bank_error_reads_as_the_banks_message(monkeypatch):
    import pytest

    from brokersync.adapters.base import AdapterError

    class Refuses(CamtOnly):
        def get_transactions_xml(self, account, start_date=None, end_date=None):
            raise RuntimeError("Error response: fints.message.FinTSInstituteMessage([... HIRMG2(responses=["
                               "fints.formals.Response(code='9050', reference_element=None, text='Teilweise "
                               "fehlerhaft.')]), HIRMS2(responses=[fints.formals.Response(code='9210', "
                               "reference_element=None, text='Eindeutige Identifikation des Kontos nicht möglich.')"
                               "]) dialog_id='abc' user_id='1234567890'])")

    monkeypatch.setattr(DeutscheBankAdapter, "client_factory", staticmethod(lambda *a: Refuses(GIRO, sca=False)))
    adapter = DeutscheBankAdapter(CREDS)
    adapter.login()
    with pytest.raises(AdapterError) as e:
        adapter.get_transactions(None)
    assert str(e.value) == ("Deutsche Bank (FinTS): Die Deutsche Bank meldet: 9050 Teilweise fehlerhaft.; "
                            "9210 Eindeutige Identifikation des Kontos nicht möglich.")
