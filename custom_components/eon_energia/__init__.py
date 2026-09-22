"""The EON Energia (Italy) integration."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed

from pyeonenergia import EonEnergiaApiError, EonEnergiaClient, PointOfDelivery

from .const import (
    CONF_ACCESS_TOKEN,
    CONF_POD,
    CONF_REFRESH_TOKEN,
    CONF_TARIFF_TYPE,
    DOMAIN,
    TARIFF_MULTIORARIA,
)
from .coordinator import EonConsumptionCoordinator, EonInvoiceCoordinator
from .services import async_register_services

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.SENSOR]


@dataclass
class EonEnergiaData:
    """What one configured supply needs at runtime."""

    api: EonEnergiaClient
    pod: str
    tariff_type: str
    coordinator: EonConsumptionCoordinator
    invoice_coordinator: EonInvoiceCoordinator
    supply: PointOfDelivery | None


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Register services, which exist whether or not an entry is configured."""
    hass.data.setdefault(DOMAIN, {})
    async_register_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up one E.ON Energia supply."""
    hass.data.setdefault(DOMAIN, {})

    pod = entry.data[CONF_POD]
    tariff_type = entry.data.get(CONF_TARIFF_TYPE, TARIFF_MULTIORARIA)

    api = EonEnergiaClient(
        access_token=entry.data[CONF_ACCESS_TOKEN],
        refresh_token=entry.data.get(CONF_REFRESH_TOKEN),
        token_callback=_token_callback(hass, entry),
        username=entry.data.get(CONF_USERNAME),
        password=entry.data.get(CONF_PASSWORD),
    )

    if not await api.validate_token():
        await api.close()
        raise ConfigEntryAuthFailed("EON Energia rejected the stored credentials")

    invoice_coordinator = EonInvoiceCoordinator(hass, api, pod)
    coordinator = EonConsumptionCoordinator(hass, api, pod, tariff_type)

    # Invoices first: costs are derived from them, so the consumption import
    # needs the prices already in hand. A failure here is not fatal - costs are
    # optional, consumption is the point.
    try:
        await invoice_coordinator.async_config_entry_first_refresh()
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning(
            "Could not fetch invoice data during setup (will retry later): %s", err
        )

    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = EonEnergiaData(
        api=api,
        pod=pod,
        tariff_type=tariff_type,
        coordinator=coordinator,
        invoice_coordinator=invoice_coordinator,
        supply=await _fetch_supply(api, pod),
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Tear one supply down."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        await entry.runtime_data.api.close()
    return unload_ok


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload when the entry's options change.

    Skips the reload when the change was only a rotated token, which the client
    writes back routinely and which nothing needs reloading for.
    """
    pending: set[str] = hass.data.setdefault(DOMAIN, {}).setdefault(
        "token_only_updates", set()
    )
    if entry.entry_id in pending:
        pending.discard(entry.entry_id)
        _LOGGER.debug("Config entry updated with refreshed tokens, not reloading")
        return

    await hass.config_entries.async_reload(entry.entry_id)


def _token_callback(hass: HomeAssistant, entry: ConfigEntry):
    """Return a callback that persists rotated tokens without a reload.

    async_update_entry fires the update listener, which reloads the integration
    and re-imports several days of statistics. That was tolerable when a refresh
    was rare; tokens are now refreshed shortly before they expire, so it would
    happen every couple of hours. Flagging the entry lets async_reload_entry tell
    this update apart from a real options change.
    """

    def persist(new_access_token: str, new_refresh_token: str) -> None:
        _LOGGER.debug("Tokens refreshed, updating config entry")
        hass.data.setdefault(DOMAIN, {}).setdefault("token_only_updates", set()).add(
            entry.entry_id
        )
        hass.config_entries.async_update_entry(
            entry,
            data={
                **entry.data,
                CONF_ACCESS_TOKEN: new_access_token,
                CONF_REFRESH_TOKEN: new_refresh_token,
            },
        )

    return persist


async def _fetch_supply(api: EonEnergiaClient, pod: str) -> PointOfDelivery | None:
    """Read the contract behind this supply.

    Contracted power, distributor, product and when the offer ends. It changes
    about once a year, so it is read at setup rather than polled, and failing to
    read it must not block the integration.
    """
    try:
        supply = await api.get_point_of_delivery(pod)
    except EonEnergiaApiError as err:
        _LOGGER.warning("Could not fetch supply details for %s: %s", pod, err)
        return None

    _LOGGER.debug(
        "Supply %s: %s, %s kW contracted, offer ends %s",
        pod,
        supply.product_name,
        supply.contractual_power,
        supply.contract_end,
    )
    return supply
