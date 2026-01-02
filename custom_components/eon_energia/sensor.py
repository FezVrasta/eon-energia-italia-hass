"""Sensor platform for EON Energia integration."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.helpers.entity import EntityCategory
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)

from .api import EONEnergiaApi
from .const import DOMAIN, CONF_TARIFF_TYPE, TARIFF_MULTIORARIA

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up EON Energia sensors from a config entry."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator = data["coordinator"]
    pod = data["pod"]
    api = data["api"]
    tariff_type = data.get("tariff_type", TARIFF_MULTIORARIA)

    entities = [
        EONEnergiaDailyConsumptionSensor(coordinator, entry, pod),
        EONEnergiaLastReadingSensor(coordinator, entry, pod),
        EONEnergiaTokenStatusSensor(coordinator, entry, pod, api),
        EONEnergiaCumulativeEnergySensor(coordinator, entry, pod, api),
    ]

    # Add fascia-specific cumulative sensors for multioraria tariffs
    if tariff_type == TARIFF_MULTIORARIA:
        entities.extend([
            EONEnergiaCumulativeEnergySensor(coordinator, entry, pod, api, fascia="F1"),
            EONEnergiaCumulativeEnergySensor(coordinator, entry, pod, api, fascia="F2"),
            EONEnergiaCumulativeEnergySensor(coordinator, entry, pod, api, fascia="F3"),
        ])

    async_add_entities(entities)


class EONEnergiaBaseSensor(CoordinatorEntity, SensorEntity):
    """Base class for EON Energia sensors."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: DataUpdateCoordinator,
        entry: ConfigEntry,
        pod: str,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._pod = pod
        self._entry = entry
        self._attr_device_info = {
            "identifiers": {(DOMAIN, pod)},
            "name": f"EON Energia {pod}",
            "manufacturer": "EON Energia",
            "model": "Smart Meter",
        }

    def _get_hourly_values(self) -> list[tuple[int, float]]:
        """Extract hourly values from API response.

        Returns list of (hour, value) tuples.
        """
        if not self.coordinator.data:
            return []

        # API returns a list with one item per day
        data = self.coordinator.data
        if isinstance(data, list) and len(data) > 0:
            day_data = data[0]
        else:
            day_data = data

        hourly_values = []
        for hour in range(1, 25):
            key = f"valore_h{hour:02d}"
            if key in day_data:
                try:
                    value = float(day_data[key])
                    hourly_values.append((hour, value))
                except (ValueError, TypeError):
                    continue

        return hourly_values


class EONEnergiaDailyConsumptionSensor(EONEnergiaBaseSensor):
    """Sensor for daily energy consumption - compatible with HA Energy Dashboard."""

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_name = "Daily Consumption"

    def __init__(
        self,
        coordinator: DataUpdateCoordinator,
        entry: ConfigEntry,
        pod: str,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry, pod)
        self._attr_unique_id = f"{pod}_daily_consumption"

    @property
    def native_value(self) -> float | None:
        """Return the total daily consumption."""
        hourly_values = self._get_hourly_values()
        if not hourly_values:
            return None

        total = sum(value for _, value in hourly_values)
        return round(total, 3)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes."""
        attrs = {
            "pod": self._pod,
        }

        if self.coordinator.data:
            data = self.coordinator.data
            if isinstance(data, list) and len(data) > 0:
                day_data = data[0]
            else:
                day_data = data

            # Add metadata from the response
            if "data" in day_data:
                attrs["data_date"] = day_data["data"]
            if "pod" in day_data:
                attrs["pod_code"] = day_data["pod"]
            if "codice_cliente" in day_data:
                attrs["customer_code"] = day_data["codice_cliente"]
            if "sorgente" in day_data:
                attrs["data_source"] = day_data["sorgente"]
            if "trattamento" in day_data:
                attrs["treatment"] = day_data["trattamento"]

            # Add hourly breakdown
            hourly_values = self._get_hourly_values()
            if hourly_values:
                attrs["hourly_breakdown"] = {
                    f"h{hour:02d}": value for hour, value in hourly_values
                }

        return attrs


class EONEnergiaLastReadingSensor(EONEnergiaBaseSensor):
    """Sensor for the last hourly reading."""

    _attr_device_class = SensorDeviceClass.ENERGY
    # No state_class - this is a snapshot of the last available reading, not tracked over time
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_name = "Last Hourly Reading"

    def __init__(
        self,
        coordinator: DataUpdateCoordinator,
        entry: ConfigEntry,
        pod: str,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry, pod)
        self._attr_unique_id = f"{pod}_last_reading"

    @property
    def native_value(self) -> float | None:
        """Return the last hourly reading."""
        hourly_values = self._get_hourly_values()
        if not hourly_values:
            return None

        # Return the last available hourly value
        _, last_value = hourly_values[-1]
        return last_value

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes."""
        attrs = {
            "pod": self._pod,
        }

        hourly_values = self._get_hourly_values()
        if hourly_values:
            last_hour, _ = hourly_values[-1]
            attrs["reading_hour"] = f"{last_hour:02d}:00"

            if self.coordinator.data:
                data = self.coordinator.data
                if isinstance(data, list) and len(data) > 0:
                    day_data = data[0]
                else:
                    day_data = data

                if "data" in day_data:
                    attrs["reading_date"] = day_data["data"]

        return attrs


class EONEnergiaTokenStatusSensor(SensorEntity):
    """Sensor for bearer token status.

    This sensor doesn't extend CoordinatorEntity because it must always
    be available to report the token/API status, even when updates fail.
    """

    _attr_has_entity_name = True
    _attr_name = "Token Status"
    _attr_icon = "mdi:key"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False

    def __init__(
        self,
        coordinator: DataUpdateCoordinator,
        entry: ConfigEntry,
        pod: str,
        api: EONEnergiaApi,
    ) -> None:
        """Initialize the sensor."""
        self.coordinator = coordinator
        self._pod = pod
        self._entry = entry
        self._api = api
        self._attr_unique_id = f"{pod}_token_status_v4"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, pod)},
            "name": f"EON Energia {pod}",
            "manufacturer": "EON Energia",
            "model": "Smart Meter",
        }

    async def async_added_to_hass(self) -> None:
        """Run when entity about to be added to hass."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self.coordinator.async_add_listener(self._handle_coordinator_update)
        )

    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self.async_write_ha_state()

    @property
    def native_value(self) -> str:
        """Return the token status."""
        if self.coordinator.last_update_success:
            return "valid"
        elif self.coordinator.last_exception:
            return "invalid"
        return "unknown"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes."""
        attrs: dict[str, Any] = {
            "pod": self._pod,
        }

        if self.coordinator.last_exception:
            attrs["last_error"] = str(self.coordinator.last_exception)

        return attrs


class EONEnergiaCumulativeEnergySensor(RestoreEntity, SensorEntity):
    """Cumulative energy sensor that displays total consumption.

    This sensor shows the running total of energy consumption and persists
    across Home Assistant restarts. The actual statistics for the Energy Dashboard
    are automatically imported by the coordinator using external statistics,
    which provides proper hourly granularity.

    This sensor is useful for:
    - Displaying the current total on dashboards
    - Tracking consumption since installation
    - Quick reference without needing to query statistics
    """

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_should_poll = False

    def __init__(
        self,
        coordinator: DataUpdateCoordinator,
        entry: ConfigEntry,
        pod: str,
        api: EONEnergiaApi,
        fascia: str | None = None,
    ) -> None:
        """Initialize the cumulative energy sensor.

        Args:
            coordinator: The data update coordinator.
            entry: The config entry.
            pod: The Point of Delivery code.
            api: The EON Energia API client (kept for potential future use).
            fascia: Optional tariff band (F1, F2, F3). If None, tracks total.
        """
        self.coordinator = coordinator
        self._entry = entry
        self._pod = pod
        self._api = api
        self._fascia = fascia

        # State tracking
        self._cumulative_total: float = 0.0
        self._last_processed_date: str | None = None

        # Set up names and IDs based on fascia
        if fascia:
            fascia_names = {
                "F1": "Peak (F1)",
                "F2": "Mid-peak (F2)",
                "F3": "Off-peak (F3)",
            }
            self._attr_name = f"Cumulative Energy {fascia_names.get(fascia, fascia)}"
            self._attr_unique_id = f"{pod}_cumulative_energy_{fascia.lower()}"
        else:
            self._attr_name = "Cumulative Energy"
            self._attr_unique_id = f"{pod}_cumulative_energy_total"

        self._attr_device_info = {
            "identifiers": {(DOMAIN, pod)},
            "name": f"EON Energia {pod}",
            "manufacturer": "EON Energia",
            "model": "Smart Meter",
        }

    async def async_added_to_hass(self) -> None:
        """Run when entity about to be added to hass."""
        await super().async_added_to_hass()

        # Restore previous state
        if (last_state := await self.async_get_last_state()) is not None:
            try:
                if last_state.state not in (None, "unknown", "unavailable"):
                    self._cumulative_total = float(last_state.state)
                    _LOGGER.debug(
                        "Restored cumulative total for %s: %s",
                        self._attr_unique_id,
                        self._cumulative_total,
                    )
            except (ValueError, TypeError):
                _LOGGER.warning(
                    "Could not restore state for %s: %s",
                    self._attr_unique_id,
                    last_state.state,
                )

            # Restore last processed date from attributes
            if last_state.attributes:
                self._last_processed_date = last_state.attributes.get(
                    "last_processed_date"
                )

        # Listen to coordinator updates
        self.async_on_remove(
            self.coordinator.async_add_listener(self._handle_coordinator_update)
        )

        # Process current data if available
        if self.coordinator.data:
            self._process_new_data()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self._process_new_data()
        self.async_write_ha_state()

    def _process_new_data(self) -> None:
        """Process new data from the coordinator and update cumulative total."""
        if not self.coordinator.data:
            return

        data = self.coordinator.data
        if isinstance(data, list) and len(data) > 0:
            day_data = data[0]
        else:
            day_data = data

        # Get the date from the data
        data_date = day_data.get("data")
        if not data_date:
            return

        # Skip if we've already processed this date
        if self._last_processed_date == data_date:
            return

        # Parse the date for fascia calculation
        try:
            current_date = datetime.strptime(data_date, "%Y-%m-%d")
        except ValueError:
            _LOGGER.warning("Could not parse date: %s", data_date)
            return

        # Calculate this day's consumption
        day_total = self._calculate_day_total(day_data, current_date)
        if day_total > 0:
            self._cumulative_total += day_total
            self._last_processed_date = data_date
            _LOGGER.debug(
                "Added %.3f kWh for %s to %s, new total: %.3f kWh",
                day_total,
                data_date,
                self._attr_unique_id,
                self._cumulative_total,
            )

    def _calculate_day_total(self, day_data: dict[str, Any], date: datetime) -> float:
        """Calculate the total consumption for a day, optionally filtered by fascia."""
        total = 0.0
        for hour in range(1, 25):
            key = f"valore_h{hour:02d}"
            if key in day_data:
                try:
                    value = float(day_data[key])
                    if value > 0:
                        # If tracking a specific fascia, check if this hour belongs to it
                        if self._fascia:
                            hour_fascia = self._get_fascia_for_hour(date, hour)
                            if hour_fascia == self._fascia:
                                total += value
                        else:
                            total += value
                except (ValueError, TypeError):
                    continue
        return round(total, 3)

    @staticmethod
    def _get_fascia_for_hour(dt: datetime, hour: int) -> str:
        """Determine the tariff band (fascia) for a given datetime and hour."""
        hour_0_based = hour - 1
        weekday = dt.weekday()

        if weekday == 6:  # Sunday
            return "F3"
        if weekday == 5:  # Saturday
            return "F2" if 7 <= hour_0_based < 23 else "F3"
        # Monday to Friday
        if 8 <= hour_0_based < 19:
            return "F1"
        elif hour_0_based == 7 or 19 <= hour_0_based < 23:
            return "F2"
        return "F3"

    @property
    def native_value(self) -> float:
        """Return the cumulative energy consumption."""
        return round(self._cumulative_total, 3)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes."""
        attrs: dict[str, Any] = {
            "pod": self._pod,
            "last_processed_date": self._last_processed_date,
        }

        if self._fascia:
            attrs["fascia"] = self._fascia

        # Point users to the external statistics for Energy Dashboard
        if self._fascia:
            attrs["statistic_id"] = f"{DOMAIN}:{self._pod}_consumption_{self._fascia.lower()}"
        else:
            attrs["statistic_id"] = f"{DOMAIN}:{self._pod}_consumption"

        return attrs
