"""Binary sensors for EON Energia."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from pyeonenergia import EonEnergiaApiError, EonEnergiaClient

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up EON Energia binary sensors from a config entry."""
    data = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            EONEnergiaTerminationAvailableSensor(
                entry, data["pod"], data["api"], data.get("supply")
            )
        ]
    )


class EONEnergiaTerminationAvailableSensor(BinarySensorEntity):
    """Whether E.ON would accept a request to terminate this supply.

    Despite E.ON calling the endpoint "Disalimentazione", this is **not** a
    warning that the supply is about to be cut off. It backs the "terminate my
    contract" flow in their app and answers whether that request would be
    accepted, so `on` is the unremarkable state for a healthy contract.

    It is off by default in the entity registry for that reason: it answers a
    question almost nobody is asking, and an entity named after disconnection
    sitting `on` in a dashboard invites exactly the wrong conclusion.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "termination_available"
    _attr_entity_registry_enabled_default = False

    def __init__(
        self,
        entry: ConfigEntry,
        pod: str,
        api: EonEnergiaClient,
        supply: Any | None,
    ) -> None:
        """Bind to the supply this entry is configured for."""
        self._pod = pod
        self._api = api
        self._supply = supply
        self._message: str | None = None
        self._attr_unique_id = f"{pod}_termination_available"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, pod)},
            "name": f"EON Energia {pod}",
            "manufacturer": "EON Energia",
            "model": (supply.product_name if supply else None) or "Smart Meter",
        }

    @property
    def is_on(self) -> bool | None:
        """Whether a termination request would currently be accepted."""
        if self._message is None:
            return None
        return "possibile" in self._message.lower()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose E.ON's own wording, which is more precise than a boolean."""
        return {"message": self._message} if self._message else {}

    async def async_update(self) -> None:
        """Ask E.ON. Polled rarely: this changes with the contract, not the day."""
        try:
            response = await self._api.get_disconnection_status(self._pod)
        except EonEnergiaApiError as err:
            _LOGGER.debug("Termination availability unavailable for %s: %s", self._pod, err)
            return

        self._message = (response or {}).get("message")
