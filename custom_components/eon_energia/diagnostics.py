"""Diagnostics, for turning "it doesn't work" into something actionable."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

#: Never leaves the machine. The tokens are credentials; the rest identifies the
#: customer rather than the fault.
REDACT_CONFIG = {
    "access_token",
    "refresh_token",
    "username",
    "password",
}
REDACT_DATA = {
    "AccountID",
    "CustomerElectricityID",
    "CustomerGasID",
    "CustomerID",
    "DeliveryAddress",
    "Email",
    "FirstName",
    "IBAN",
    "LastName",
    "TaxIDCode",
    "codeline",
    "codiceIUV",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return everything worth seeing about one supply, minus the identifying parts."""
    data = entry.runtime_data
    consumption = data.coordinator
    invoices = data.invoice_coordinator

    return {
        "entry": async_redact_data(dict(entry.data), REDACT_CONFIG),
        "pod": data.pod,
        "tariff_type": data.tariff_type,
        "supply": async_redact_data(data.supply.raw, REDACT_DATA)
        if data.supply
        else None,
        "consumption": {
            "last_update_success": consumption.last_update_success,
            "import_state": consumption.import_state,
            # The readings themselves, which is what makes a statistics bug
            # diagnosable without asking the reporter to run anything.
            "latest": [
                async_redact_data(reading.raw, REDACT_DATA)
                for reading in (consumption.data or [])
            ],
        },
        "invoices": {
            "last_update_success": invoices.last_update_success,
            "count": len(invoices.data or []),
            "latest": [
                async_redact_data(invoice.raw, REDACT_DATA)
                for invoice in (invoices.data or [])[:3]
            ],
        },
    }
