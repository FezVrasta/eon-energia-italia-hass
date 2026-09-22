"""Model tests, against the response shapes E.ON actually return.

The payloads below are real ones with the identifying values changed. Keeping them
verbatim in shape is the point: E.ON mix Italian and English keys, send numbers as
strings, and nest the interesting fields, and every one of those has broken a
naive parse at some stage.
"""

from __future__ import annotations

from datetime import date

from pyeonenergia import (
    Account,
    BillingProfile,
    HourlyConsumption,
    Invoice,
    PointOfDelivery,
)

POD_PAYLOAD = {
    "PODID": "10030006784668",
    "ExternalInstallationID": "IT001E12530948",
    "BillingProfileID": "PF-7293939",
    "AccountID": "A305402184",
    "AbilitatoFixing": "false",
    "DeliveryAddress": {
        "Prefix": "VIA",
        "Street": "ALDO MORO",
        "Number": "23",
        "ZIPCode": "28100",
        "City": "NOVARA",
        "Province": "NO",
    },
    "Installation": {
        "StartDate": "01/10/2017 00:00:00.000",
        "Status": "ACTIVE",
        "Type": "POWER",
    },
    "CreateDate": "18/07/2017 13:51:02.000",
}

# The per-POD detail endpoint, which carries the contract. The list endpoint
# returns none of this.
POD_DETAIL_PAYLOAD = {
    **POD_PAYLOAD,
    "Installation": {
        **POD_PAYLOAD["Installation"],
        "Fasce": "3 fasce AEEG",
        "TipoMercato": "Libero",
    },
    "TechDataElectricity": {
        "AvailablePower": "6.6",
        "ContractualPower": "6.0",
        "Tension": "220",
        "DSO": "e-distribuzione S.p.A.",
        "DSOEmergencyPhoneNumber": "803.500",
        "YearConsumption": "3504.0",
        "UsageType": "RESIDENTIAL",
    },
    "ProductInformation": {
        "ID": "L61AD5U_D_A_CLSA",
        "Name": "E.ON LuceClick - Amico new",
        "StartDate": "15/09/2025 00:00:00.000",
        "EndDate": "30/09/2026 00:00:00.000",
    },
}

BILLING_PROFILE_PAYLOAD = {
    "BillingProfileID": "PF-7293939",
    "Commodity": "POWER",
    "Status": "ACTIVE",
    "IBAN": "IT00X0000000000000000000000",
    "InvoiceDeliveryMethod": {"Email": "someone@example.com", "DeliveryMethod": "EMAIL"},
    "PaymentMethod": {"PaymentMethod": "DIRECT_DEBIT", "DirectDebitOwnerName": "FEDERICO"},
}

INVOICE_PAYLOAD = {
    "AccountID": "A305402184",
    "BillingProfile": "PF-7293939",
    "Commodity": "POWER",
    "Anno": "2026",
    "Numero": "13384141",
    "DataEmissione": "15/09/2026",
    "TipoDocumento": "B",
    "ModalitaPagamento": "RID",
    "Importo": "98.00",
    "DataScadenza": "30/09/2026",
    "PeriodoCompetenzaInizio": "01/08/2026",
    "PeriodoCompetenzaFine": "31/08/2026",
    "StatoPagamento": "NOT_PAID",
    "ImportoPagato": "0.00",
    "ImportoResiduo": "98.00",
    "Rateizzato": "N",
    "codeline": "9000000050415699",
    "codiceIUV": "300260301248813077",
    "ListaForniture": [{"CodiceFornitura": "10030006784668"}],
}

CONSUMPTION_PAYLOAD = {
    "pod": "IT001E12530948",
    "pr": "10030006784668",
    "codice_cliente": "A305402184",
    "data": "2026-09-18",
    "tipo_consumo": "E",
    "misura": "Ea",
    "tensione": 220,
    "trattamento": "O",
    "sorgente": "NXC",
    **{f"valore_h{hour:02d}": 0.25 for hour in range(1, 25)},
}

ACCOUNT_PAYLOAD = {
    "PrimaryData": {
        "CustomerID": {"CustomerID": "A305402184"},
        "FirstName": "GELTRUDE",
        "LastName": "BORGETTO",
        "TaxIDCode": "BRGGTR41S64L009J",
        "CustomerStatus": "ACTIVE",
        "Commodity": "DUAL",
    },
    "Contacts": {"Email": "someone@example.com"},
}


class TestPointOfDelivery:
    def test_reads_the_fields_that_matter(self):
        pod = PointOfDelivery.from_api(POD_PAYLOAD)
        assert pod.pod_id == "10030006784668"
        assert pod.external_installation_id == "IT001E12530948"
        assert pod.account_id == "A305402184"
        assert pod.is_active
        assert pod.is_electricity

    def test_composes_the_address(self):
        assert PointOfDelivery.from_api(POD_PAYLOAD).address == (
            "VIA ALDO MORO 23, 28100 NOVARA"
        )

    def test_gas_is_not_electricity(self):
        payload = {**POD_PAYLOAD, "Installation": {"Type": "GAS", "Status": "CLOSED"}}
        pod = PointOfDelivery.from_api(payload)
        assert not pod.is_electricity
        assert not pod.is_active

    def test_survives_a_hollow_payload(self):
        pod = PointOfDelivery.from_api({})
        assert pod.pod_id is None
        assert pod.address is None
        assert not pod.is_active


class TestInvoice:
    def test_parses_amounts_and_italian_dates(self):
        invoice = Invoice.from_api(INVOICE_PAYLOAD)
        assert invoice.amount == 98.0
        assert invoice.outstanding == 98.0
        assert invoice.issued_on == date(2026, 9, 15)
        assert invoice.due_on == date(2026, 9, 30)
        assert invoice.period_start == date(2026, 8, 1)

    def test_billing_month_follows_the_competence_period(self):
        # Issued in September, but it bills August. Using the issue date here is
        # what made a month's costs land against the wrong month.
        assert Invoice.from_api(INVOICE_PAYLOAD).billing_month == "2026-08"

    def test_billing_month_falls_back_to_the_issue_date(self):
        payload = {k: v for k, v in INVOICE_PAYLOAD.items() if k != "PeriodoCompetenzaInizio"}
        assert Invoice.from_api(payload).billing_month == "2026-09"

    def test_payment_status(self):
        assert not Invoice.from_api(INVOICE_PAYLOAD).is_paid
        assert Invoice.from_api({**INVOICE_PAYLOAD, "StatoPagamento": "PAID"}).is_paid

    def test_comma_decimals_parse(self):
        assert Invoice.from_api({**INVOICE_PAYLOAD, "Importo": "1.234,50"}).amount == 1234.50

    def test_raw_is_kept_for_fields_with_no_model(self):
        invoice = Invoice.from_api(INVOICE_PAYLOAD)
        assert invoice.raw["ListaForniture"][0]["CodiceFornitura"] == "10030006784668"


class TestHourlyConsumption:
    def test_collects_every_hour(self):
        reading = HourlyConsumption.from_api(CONSUMPTION_PAYLOAD)
        assert reading.day == date(2026, 9, 18)
        assert len(reading.values) == 24
        assert reading.total == 6.0

    def test_hours_map_to_real_instants(self):
        hours = HourlyConsumption.from_api(CONSUMPTION_PAYLOAD).hours()
        assert len(hours) == 24
        assert len({instant for _, instant, _ in hours}) == 24

    def test_the_long_day_reports_every_hour_once(self):
        # 25 October 2026: the clocks go back, so the local day has 25 hours and
        # E.ON's 24 fields cover all but the last. Nothing may collide.
        payload = {**CONSUMPTION_PAYLOAD, "data": "2026-10-25"}
        hours = HourlyConsumption.from_api(payload).hours()
        assert len(hours) == 24
        assert len({instant for _, instant, _ in hours}) == 24

    def test_the_short_day_drops_the_field_with_nowhere_to_go(self):
        # 29 March 2026: 23 local hours, so the 24th field has no instant.
        payload = {**CONSUMPTION_PAYLOAD, "data": "2026-03-29"}
        hours = HourlyConsumption.from_api(payload).hours()
        assert len(hours) == 23
        assert len({instant for _, instant, _ in hours}) == 23

    def test_missing_hours_are_simply_absent(self):
        payload = {k: v for k, v in CONSUMPTION_PAYLOAD.items() if k != "valore_h05"}
        reading = HourlyConsumption.from_api(payload)
        assert 5 not in reading.values
        assert len(reading.values) == 23

    def test_survives_an_unparseable_day(self):
        reading = HourlyConsumption.from_api({**CONSUMPTION_PAYLOAD, "data": "nonsense"})
        assert reading.day is None
        assert reading.hours() == []


class TestAccount:
    def test_digs_the_identity_out_of_the_nesting(self):
        account = Account.from_api(ACCOUNT_PAYLOAD)
        assert account.account_id == "A305402184"
        assert account.tax_code == "BRGGTR41S64L009J"
        assert account.email == "someone@example.com"
        assert account.full_name == "Geltrude Borgetto"

    def test_survives_a_hollow_payload(self):
        account = Account.from_api({})
        assert account.account_id is None
        assert account.full_name is None


class TestSupplyDetail:
    def test_the_list_endpoint_leaves_contract_fields_empty(self):
        # Only the per-POD endpoint carries them; nothing should invent a value.
        pod = PointOfDelivery.from_api(POD_PAYLOAD)
        assert pod.contractual_power is None
        assert pod.contract_end is None
        assert pod.distributor is None

    def test_the_detail_endpoint_fills_them_in(self):
        pod = PointOfDelivery.from_api(POD_DETAIL_PAYLOAD)
        assert pod.contractual_power == 6.0
        assert pod.available_power == 6.6
        assert pod.voltage == 220.0
        assert pod.distributor == "e-distribuzione S.p.A."
        assert pod.annual_consumption == 3504.0
        assert pod.usage_type == "RESIDENTIAL"
        assert pod.product_name == "E.ON LuceClick - Amico new"

    def test_contract_dates_drop_the_time_part(self):
        # E.ON send "15/09/2025 00:00:00.000", not a bare date.
        pod = PointOfDelivery.from_api(POD_DETAIL_PAYLOAD)
        assert pod.contract_start == date(2025, 9, 15)
        assert pod.contract_end == date(2026, 9, 30)

    def test_banded_tariff_is_read_rather_than_asked(self):
        assert PointOfDelivery.from_api(POD_DETAIL_PAYLOAD).is_multi_band
        flat = {
            **POD_DETAIL_PAYLOAD,
            "Installation": {**POD_DETAIL_PAYLOAD["Installation"], "Fasce": "Monoraria"},
        }
        assert not PointOfDelivery.from_api(flat).is_multi_band


class TestInvoicePayment:
    def test_payment_method_and_pagopa_codes(self):
        invoice = Invoice.from_api(INVOICE_PAYLOAD)
        assert invoice.payment_method == "RID"
        assert invoice.is_direct_debit
        assert invoice.iuv == "300260301248813077"
        assert invoice.codeline == "9000000050415699"
        assert not invoice.instalment_plan

    def test_a_bill_you_must_go_and_pay(self):
        invoice = Invoice.from_api({**INVOICE_PAYLOAD, "ModalitaPagamento": "BOLLETTINO"})
        assert not invoice.is_direct_debit

    def test_instalment_plan_is_flagged(self):
        assert Invoice.from_api({**INVOICE_PAYLOAD, "Rateizzato": "S"}).instalment_plan


class TestBillingProfile:
    def test_reads_payment_and_delivery(self):
        profile = BillingProfile.from_api(BILLING_PROFILE_PAYLOAD)
        assert profile.profile_id == "PF-7293939"
        assert profile.payment_method == "DIRECT_DEBIT"
        assert profile.invoice_delivery_method == "EMAIL"
        assert profile.is_direct_debit

    def test_the_iban_is_not_modelled(self):
        # It is in the response and stays in raw, but nothing downstream should
        # be able to pick up a bank account number by accident.
        profile = BillingProfile.from_api(BILLING_PROFILE_PAYLOAD)
        assert "IBAN" not in {f for f in profile.__slots__}
        assert "IT00X" not in repr(profile)
        assert profile.raw["IBAN"].startswith("IT00X")
