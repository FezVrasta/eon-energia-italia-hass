"""Typed views over E.ON's responses.

E.ON's JSON mixes Italian and English keys, returns numbers as strings, and varies
in shape between endpoints. Each model pulls out the fields that are actually
dependable and keeps the untouched payload in `raw`, so a caller that needs
something unusual is not forced to go round the library.

Nothing here raises on a missing field. A response that has lost a key should
degrade to `None` rather than take down a poll.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from .tariff import parse_italian_date


def _text(payload: dict[str, Any], *keys: str) -> str | None:
    """Return the first key present and non-empty, as a stripped string."""
    for key in keys:
        value = payload.get(key)
        if value not in (None, "", "null"):
            return str(value).strip()
    return None


def _number(payload: dict[str, Any], *keys: str) -> float | None:
    """Return the first key present that parses as a number.

    Amounts arrive as strings. Observed payloads use a plain `98.00`, but Italian
    billing systems also emit `1.234,50`, so a comma means the dots before it are
    thousands separators. Without that distinction the European form silently
    returns None and an invoice loses its amount.
    """
    for key in keys:
        value = payload.get(key)
        if value in (None, "", "null"):
            continue
        text = str(value).strip()
        if "," in text:
            text = text.replace(".", "").replace(",", ".")
        try:
            return float(text)
        except ValueError:
            continue
    return None


@dataclass(slots=True)
class Account:
    """A customer account."""

    account_id: str | None
    first_name: str | None
    last_name: str | None
    tax_code: str | None
    status: str | None
    commodity: str | None
    email: str | None
    raw: dict[str, Any] = field(repr=False, default_factory=dict)

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> Account:
        """Build from one entry of the accounts response."""
        primary = payload.get("PrimaryData") or {}
        customer = primary.get("CustomerID") or {}
        contacts = payload.get("Contacts") or {}
        return cls(
            account_id=_text(customer, "CustomerID"),
            first_name=_text(primary, "FirstName"),
            last_name=_text(primary, "LastName"),
            tax_code=_text(primary, "TaxIDCode"),
            status=_text(primary, "CustomerStatus"),
            commodity=_text(primary, "Commodity"),
            email=_text(contacts, "Email"),
            raw=payload,
        )

    @property
    def full_name(self) -> str | None:
        """First and last name together, when both are known."""
        parts = [p for p in (self.first_name, self.last_name) if p]
        return " ".join(parts).title() if parts else None


@dataclass(slots=True)
class PointOfDelivery:
    """A metering point. E.ON call the customer-facing code the PR."""

    pod_id: str | None
    account_id: str | None
    billing_profile_id: str | None
    external_installation_id: str | None
    commodity: str | None
    status: str | None
    address: str | None
    raw: dict[str, Any] = field(repr=False, default_factory=dict)

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> PointOfDelivery:
        """Build from one entry of the point-of-deliveries response."""
        installation = payload.get("Installation") or {}
        delivery = payload.get("DeliveryAddress") or {}
        street = " ".join(
            part
            for part in (
                _text(delivery, "Prefix"),
                _text(delivery, "Street"),
                _text(delivery, "Number"),
            )
            if part
        )
        city = " ".join(
            part
            for part in (_text(delivery, "ZIPCode"), _text(delivery, "City"))
            if part
        )
        return cls(
            pod_id=_text(payload, "PODID", "PRID"),
            account_id=_text(payload, "AccountID"),
            billing_profile_id=_text(payload, "BillingProfileID"),
            external_installation_id=_text(payload, "ExternalInstallationID"),
            commodity=_text(installation, "Type"),
            status=_text(installation, "Status"),
            address=", ".join(part for part in (street, city) if part) or None,
            raw=payload,
        )

    @property
    def is_active(self) -> bool:
        """Whether the supply is currently live."""
        return (self.status or "").upper() == "ACTIVE"

    @property
    def is_electricity(self) -> bool:
        """Whether this is an electricity supply rather than gas."""
        return (self.commodity or "").upper() == "POWER"


@dataclass(slots=True)
class Invoice:
    """One issued document. Not always a bill: `document_type` says which."""

    number: str | None
    year: str | None
    account_id: str | None
    commodity: str | None
    amount: float | None
    outstanding: float | None
    paid: float | None
    issued_on: date | None
    due_on: date | None
    period_start: date | None
    period_end: date | None
    payment_status: str | None
    document_type: str | None
    raw: dict[str, Any] = field(repr=False, default_factory=dict)

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> Invoice:
        """Build from one entry of `ListaFatture`."""
        return cls(
            number=_text(payload, "Numero"),
            year=_text(payload, "Anno"),
            account_id=_text(payload, "AccountID"),
            commodity=_text(payload, "Commodity"),
            amount=_number(payload, "Importo"),
            outstanding=_number(payload, "ImportoResiduo"),
            paid=_number(payload, "ImportoPagato"),
            issued_on=parse_italian_date(_text(payload, "DataEmissione")),
            due_on=parse_italian_date(_text(payload, "DataScadenza")),
            period_start=parse_italian_date(_text(payload, "PeriodoCompetenzaInizio")),
            period_end=parse_italian_date(_text(payload, "PeriodoCompetenzaFine")),
            payment_status=_text(payload, "StatoPagamento", "StatoDocumento"),
            document_type=_text(payload, "TipoDocumento"),
            raw=payload,
        )

    @property
    def is_paid(self) -> bool:
        """Whether E.ON consider this settled."""
        return (self.payment_status or "").upper() not in ("NOT_PAID", "UNPAID", "")

    @property
    def billing_month(self) -> str | None:
        """The `YYYY-MM` this document covers, taken from its competence period.

        Falls back to the issue date, which runs a month behind the consumption,
        so prefer the period when E.ON supply one.
        """
        anchor = self.period_start or self.issued_on
        return f"{anchor.year:04d}-{anchor.month:02d}" if anchor else None


@dataclass(slots=True)
class HourlyConsumption:
    """One day of quarter-hourly-derived hourly readings for a metering point.

    E.ON always send twenty-four `valore_hNN` fields regardless of what the local
    day actually contains, which matters twice a year; `hours()` resolves them
    against the real calendar rather than assuming.
    """

    pod: str | None
    pr: str | None
    account_id: str | None
    day: date | None
    measure: str | None
    consumption_type: str | None
    source: str | None
    values: dict[int, float] = field(default_factory=dict)
    raw: dict[str, Any] = field(repr=False, default_factory=dict)

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> HourlyConsumption:
        """Build from one entry of the daily-consumption response."""
        day: date | None = None
        if raw_day := _text(payload, "data"):
            try:
                day = datetime.strptime(raw_day, "%Y-%m-%d").date()
            except ValueError:
                day = parse_italian_date(raw_day)

        values: dict[int, float] = {}
        for hour in range(1, 25):
            value = _number(payload, f"valore_h{hour:02d}")
            if value is not None:
                values[hour] = value

        return cls(
            pod=_text(payload, "pod"),
            pr=_text(payload, "pr"),
            account_id=_text(payload, "codice_cliente"),
            day=day,
            measure=_text(payload, "misura"),
            consumption_type=_text(payload, "tipo_consumo"),
            source=_text(payload, "sorgente"),
            values=values,
            raw=payload,
        )

    @property
    def total(self) -> float:
        """The day's total, in kWh."""
        return sum(self.values.values())

    def hours(self, tz=None) -> list[tuple[int, datetime, float]]:
        """Return `(field number, instant, kWh)` for every hour that maps to one.

        The field number comes back because tariff banding needs it: `fascia_for_hour`
        works in E.ON's 1-based hour, not in wall-clock time.

        On the spring-forward day the twenty-fourth field has nowhere to go and is
        dropped; on the autumn one the final hour goes unreported. Both are left
        as they are rather than folded onto a neighbouring hour.
        """
        from .tariff import ITALY, hour_start_for_field

        if self.day is None:
            return []

        zone = tz or ITALY
        out: list[tuple[int, datetime, float]] = []
        for hour, value in sorted(self.values.items()):
            start = hour_start_for_field(self.day, hour, zone)
            if start is not None:
                out.append((hour, start, value))
        return out
