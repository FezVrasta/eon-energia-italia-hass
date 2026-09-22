"""The `eon_energia.import_statistics` service."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

import voluptuous as vol

from homeassistant.components.recorder import get_instance
from homeassistant.core import HomeAssistant, ServiceCall

from .const import DOMAIN
from .statistics import import_historical_statistics, import_invoice_cost_statistics

_LOGGER = logging.getLogger(__name__)

SERVICE_IMPORT_STATISTICS = "import_statistics"

SERVICE_SCHEMA = vol.Schema(
    {
        vol.Optional("days", default=90): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=365)
        ),
        vol.Optional("clear_existing", default=False): bool,
    }
)


def _statistic_ids(pod: str) -> list[str]:
    """Every statistic this integration owns for one supply."""
    return [
        f"{DOMAIN}:{pod}_consumption",
        f"{DOMAIN}:{pod}_consumption_f1",
        f"{DOMAIN}:{pod}_consumption_f2",
        f"{DOMAIN}:{pod}_consumption_f3",
        f"{DOMAIN}:{pod}_cost",
    ]


async def _import_for_entry(hass: HomeAssistant, data, days: int, clear: bool) -> None:
    """Run a historical import for one configured supply."""
    _LOGGER.info(
        "Importing statistics for POD %s (tariff: %s, days: %d, clear_existing: %s)",
        data.pod,
        data.tariff_type,
        days,
        clear,
    )

    if clear:
        _LOGGER.info("Clearing existing statistics for %s", data.pod)
        # Must go through the recorder's own scheduling, not a generic executor
        # job: statistics_meta_manager.delete() asserts it is running on the
        # recorder thread and raises "Detected unsafe call not in recorder
        # thread" otherwise, which took the whole service call down with a 500.
        get_instance(hass).async_clear_statistics(_statistic_ids(data.pod))

    # Stop the six-hourly poll writing the same statistic ids underneath us.
    state = data.coordinator.import_state
    state["importing_historical"] = True

    try:
        await data.invoice_coordinator.async_request_refresh()
    except Exception as err:  # noqa: BLE001 - costs are optional, consumption is not
        _LOGGER.warning(
            "Could not refresh invoice data (will import without cost): %s", err
        )

    try:
        # Consumption first, because deriving a price needs to know how much was
        # consumed in the month the invoice covers.
        daily = await import_historical_statistics(
            hass, data.api, data.pod, days, data.tariff_type
        )

        if data.invoice_coordinator.data:
            _LOGGER.info(
                "Calculating invoice prices using %d days of consumption data",
                len(daily),
            )
            await import_invoice_cost_statistics(
                hass, data.api, data.invoice_coordinator.data, data.pod, daily
            )
            # Again, now that there are prices to attach.
            _LOGGER.info("Re-importing statistics with cost data")
            await import_historical_statistics(
                hass, data.api, data.pod, days, data.tariff_type
            )

        # Keep the poll from re-importing what was just written, which would
        # rebase the series for no reason.
        state["last_date"] = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")
    finally:
        state["importing_historical"] = False


def async_register_services(hass: HomeAssistant) -> None:
    """Register the integration's services. Safe to call more than once."""
    if hass.services.has_service(DOMAIN, SERVICE_IMPORT_STATISTICS):
        return

    async def handle_import_statistics(call: ServiceCall) -> None:
        """Import historical statistics for every configured supply."""
        days = call.data["days"]
        clear = call.data["clear_existing"]
        _LOGGER.info("Starting historical data import for the last %d days", days)

        entries = [
            entry
            for entry in hass.config_entries.async_entries(DOMAIN)
            if getattr(entry, "runtime_data", None) is not None
        ]
        if not entries:
            _LOGGER.warning("No loaded EON Energia entries to import for")
            return

        for entry in entries:
            await _import_for_entry(hass, entry.runtime_data, days, clear)

    hass.services.async_register(
        DOMAIN,
        SERVICE_IMPORT_STATISTICS,
        handle_import_statistics,
        schema=SERVICE_SCHEMA,
    )
