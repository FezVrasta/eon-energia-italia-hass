"""Shared entity bases.

One place that decides what a unique ID looks like and what the device is. Both
used to be copy-pasted into every entity class, which is how a set of entities
ended up keyed on the config entry id instead of the POD and appeared twice on an
instance that had two entries for one supply.
"""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity, DataUpdateCoordinator

from pyeonenergia import PointOfDelivery

from .const import DOMAIN


def unique_id(pod: str, key: str) -> str:
    """Return the permanent ID for one entity on one supply.

    Keyed on the POD, never on the config entry: the POD is what E.ON bill and
    what the meter is, and it survives the entry being removed and re-added.
    Changing this scheme orphans every entity's history, so do not.
    """
    return f"{pod}_{key}"


def build_device_info(pod: str, supply: PointOfDelivery | None = None) -> DeviceInfo:
    """One Home Assistant device per metering point."""
    info = DeviceInfo(
        identifiers={(DOMAIN, pod)},
        name=f"EON Energia {pod}",
        manufacturer="EON Energia",
        model=(supply.product_name if supply else None) or "Smart Meter",
    )
    if supply:
        # Shown on the device page, and the closest thing E.ON expose to a
        # serial: the distributor's own identifier for the connection.
        if supply.external_installation_id:
            info["serial_number"] = supply.external_installation_id
        if supply.address:
            info["suggested_area"] = supply.address.split(",")[-1].strip()
    return info


class EonEnergiaEntity(CoordinatorEntity[DataUpdateCoordinator]):
    """Base for entities backed by one of the two polls."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: DataUpdateCoordinator,
        pod: str,
        key: str,
        supply: PointOfDelivery | None = None,
    ) -> None:
        """Bind to a coordinator and take a permanent ID from the supply."""
        super().__init__(coordinator)
        self._pod = pod
        self._attr_unique_id = unique_id(pod, key)
        self._attr_device_info = build_device_info(pod, supply)


class EonEnergiaSupplyEntity:
    """Mixin for entities describing the contract rather than the consumption.

    These have no coordinator: the values behind them change when the contract
    does, which is roughly annually, so they are read once at setup.
    """

    _attr_has_entity_name = True

    def _init_supply(self, pod: str, key: str, supply: PointOfDelivery | None) -> None:
        """Apply the shared identity to a coordinator-less entity."""
        self._pod = pod
        self._supply = supply
        self._attr_unique_id = unique_id(pod, key)
        self._attr_device_info = build_device_info(pod, supply)
