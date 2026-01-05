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
from homeassistant.const import CURRENCY_EURO, UnitOfEnergy
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
    invoice_coordinator = data["invoice_coordinator"]
    pod = data["pod"]
    api = data["api"]
    tariff_type = data.get("tariff_type", TARIFF_MULTIORARIA)

    entities = [
        # Consumption sensors
        EONEnergiaDailyConsumptionSensor(coordinator, entry, pod),
        EONEnergiaLastReadingSensor(coordinator, entry, pod),
        EONEnergiaTokenStatusSensor(coordinator, entry, pod, api),
        EONEnergiaCumulativeEnergySensor(coordinator, entry, pod, api),
        # Invoice sensors
        EONEnergiaLatestInvoiceSensor(invoice_coordinator, entry, pod),
        EONEnergiaInvoicePaymentStatusSensor(invoice_coordinator, entry, pod),
        EONEnergiaUnpaidInvoicesSensor(invoice_coordinator, entry, pod),
        EONEnergiaTotalInvoicedSensor(invoice_coordinator, entry, pod),
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


class EONEnergiaLatestInvoiceSensor(CoordinatorEntity, SensorEntity):
    """Sensor for the latest invoice amount."""

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_native_unit_of_measurement = CURRENCY_EURO
    _attr_name = "Latest Invoice"
    _attr_icon = "mdi:receipt-text"

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
        self._attr_unique_id = f"{pod}_latest_invoice"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, pod)},
            "name": f"EON Energia {pod}",
            "manufacturer": "EON Energia",
            "model": "Smart Meter",
        }

    def _get_latest_invoice(self) -> dict[str, Any] | None:
        """Get the most recent invoice from coordinator data."""
        if not self.coordinator.data:
            return None

        invoices = self.coordinator.data
        if not invoices:
            return None

        # Sort by issue date (DataEmissione) to get the latest
        sorted_invoices = sorted(
            invoices,
            key=lambda x: datetime.strptime(x.get("DataEmissione", "01/01/1970"), "%d/%m/%Y"),
            reverse=True,
        )
        return sorted_invoices[0] if sorted_invoices else None

    @property
    def native_value(self) -> float | None:
        """Return the latest invoice amount."""
        invoice = self._get_latest_invoice()
        if not invoice:
            return None

        try:
            return float(invoice.get("Importo", 0))
        except (ValueError, TypeError):
            return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes."""
        attrs: dict[str, Any] = {
            "pod": self._pod,
        }

        invoice = self._get_latest_invoice()
        if invoice:
            attrs["invoice_number"] = invoice.get("Numero") or invoice.get("NumeroDocumento")
            attrs["issue_date"] = invoice.get("DataEmissione")
            attrs["due_date"] = invoice.get("DataScadenza")
            attrs["payment_status"] = invoice.get("StatoPagamento")
            attrs["amount_paid"] = invoice.get("ImportoPagato")
            attrs["amount_remaining"] = invoice.get("ImportoResiduo")

            # Get period and amount from ListaForniture if available
            forniture = invoice.get("ListaForniture", [])
            for fornitura in forniture:
                codice_fornitura = fornitura.get("CodiceFornitura", "")
                codice_pdr_pod = fornitura.get("CodicePDR_POD", "")
                if self._pod in (codice_fornitura, codice_pdr_pod):
                    attrs["billing_period_start"] = fornitura.get("PeriodoCompetenzaInizio") or fornitura.get("DataInizio")
                    attrs["billing_period_end"] = fornitura.get("PeriodoCompetenzaFine") or fornitura.get("DataFine")
                    attrs["pod_amount"] = fornitura.get("ImportoFornitura") or fornitura.get("Importo")
                    break

        return attrs


class EONEnergiaInvoicePaymentStatusSensor(CoordinatorEntity, SensorEntity):
    """Sensor for invoice payment status."""

    _attr_has_entity_name = True
    _attr_name = "Invoice Payment Status"
    _attr_icon = "mdi:credit-card-check"

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
        self._attr_unique_id = f"{pod}_invoice_payment_status"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, pod)},
            "name": f"EON Energia {pod}",
            "manufacturer": "EON Energia",
            "model": "Smart Meter",
        }

    def _get_latest_invoice(self) -> dict[str, Any] | None:
        """Get the most recent invoice from coordinator data."""
        if not self.coordinator.data:
            return None

        invoices = self.coordinator.data
        if not invoices:
            return None

        sorted_invoices = sorted(
            invoices,
            key=lambda x: datetime.strptime(x.get("DataEmissione", "01/01/1970"), "%d/%m/%Y"),
            reverse=True,
        )
        return sorted_invoices[0] if sorted_invoices else None

    @property
    def native_value(self) -> str | None:
        """Return the payment status."""
        invoice = self._get_latest_invoice()
        if not invoice:
            return None

        status = invoice.get("StatoPagamento", "")
        # Translate common statuses
        status_map = {
            "PAID": "paid",
            "NOT_PAID": "unpaid",
            "PAGATO": "paid",
            "NON_PAGATO": "unpaid",
            "DA_PAGARE": "unpaid",
            "PARZIALMENTE_PAGATO": "partial",
        }
        return status_map.get(status.upper(), status.lower())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes."""
        attrs: dict[str, Any] = {
            "pod": self._pod,
        }

        invoice = self._get_latest_invoice()
        if invoice:
            attrs["invoice_number"] = invoice.get("Numero") or invoice.get("NumeroDocumento")
            attrs["due_date"] = invoice.get("DataScadenza")
            attrs["total_amount"] = invoice.get("Importo")
            attrs["amount_paid"] = invoice.get("ImportoPagato")
            attrs["amount_remaining"] = invoice.get("ImportoResiduo")
            attrs["raw_status"] = invoice.get("StatoPagamento")

        return attrs


class EONEnergiaUnpaidInvoicesSensor(CoordinatorEntity, SensorEntity):
    """Sensor for total unpaid invoice amount."""

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_native_unit_of_measurement = CURRENCY_EURO
    _attr_name = "Unpaid Invoices"
    _attr_icon = "mdi:cash-clock"

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
        self._attr_unique_id = f"{pod}_unpaid_invoices"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, pod)},
            "name": f"EON Energia {pod}",
            "manufacturer": "EON Energia",
            "model": "Smart Meter",
        }

    @property
    def native_value(self) -> float:
        """Return the total unpaid amount."""
        if not self.coordinator.data:
            return 0.0

        total_unpaid = 0.0
        for invoice in self.coordinator.data:
            try:
                remaining = float(invoice.get("ImportoResiduo", 0))
                if remaining > 0:
                    total_unpaid += remaining
            except (ValueError, TypeError):
                continue

        return round(total_unpaid, 2)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes."""
        attrs: dict[str, Any] = {
            "pod": self._pod,
        }

        if self.coordinator.data:
            unpaid_invoices = []
            for invoice in self.coordinator.data:
                try:
                    remaining = float(invoice.get("ImportoResiduo", 0))
                    if remaining > 0:
                        unpaid_invoices.append({
                            "number": invoice.get("Numero") or invoice.get("NumeroDocumento"),
                            "due_date": invoice.get("DataScadenza"),
                            "amount": remaining,
                        })
                except (ValueError, TypeError):
                    continue

            attrs["unpaid_count"] = len(unpaid_invoices)
            attrs["unpaid_invoices"] = unpaid_invoices

        return attrs


class EONEnergiaTotalInvoicedSensor(RestoreEntity, SensorEntity):
    """Sensor tracking total invoiced amount over time."""

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = CURRENCY_EURO
    _attr_name = "Total Invoiced"
    _attr_icon = "mdi:cash-multiple"
    _attr_should_poll = False

    def __init__(
        self,
        coordinator: DataUpdateCoordinator,
        entry: ConfigEntry,
        pod: str,
    ) -> None:
        """Initialize the sensor."""
        self.coordinator = coordinator
        self._pod = pod
        self._entry = entry
        self._attr_unique_id = f"{pod}_total_invoiced"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, pod)},
            "name": f"EON Energia {pod}",
            "manufacturer": "EON Energia",
            "model": "Smart Meter",
        }

        # State tracking
        self._total_invoiced: float = 0.0
        self._processed_invoices: set[str] = set()

    async def async_added_to_hass(self) -> None:
        """Run when entity about to be added to hass."""
        await super().async_added_to_hass()

        # Restore previous state
        if (last_state := await self.async_get_last_state()) is not None:
            try:
                if last_state.state not in (None, "unknown", "unavailable"):
                    self._total_invoiced = float(last_state.state)
                    _LOGGER.debug(
                        "Restored total invoiced for %s: %s",
                        self._attr_unique_id,
                        self._total_invoiced,
                    )
            except (ValueError, TypeError):
                _LOGGER.warning(
                    "Could not restore state for %s: %s",
                    self._attr_unique_id,
                    last_state.state,
                )

            # Restore processed invoices from attributes
            if last_state.attributes:
                processed = last_state.attributes.get("processed_invoice_numbers", [])
                if processed:
                    self._processed_invoices = set(processed)

        # Listen to coordinator updates
        self.async_on_remove(
            self.coordinator.async_add_listener(self._handle_coordinator_update)
        )

        # Process current data if available
        if self.coordinator.data:
            self._process_new_invoices()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self._process_new_invoices()
        self.async_write_ha_state()

    def _process_new_invoices(self) -> None:
        """Process new invoices and update total."""
        if not self.coordinator.data:
            return

        for invoice in self.coordinator.data:
            invoice_number = invoice.get("Numero") or invoice.get("NumeroDocumento")
            if not invoice_number or invoice_number in self._processed_invoices:
                continue

            # Get the amount for this POD from the invoice
            # Check both CodiceFornitura and CodicePDR_POD since either might match
            amount = 0.0
            forniture = invoice.get("ListaForniture", [])
            for fornitura in forniture:
                codice_fornitura = fornitura.get("CodiceFornitura", "")
                codice_pdr_pod = fornitura.get("CodicePDR_POD", "")
                if self._pod in (codice_fornitura, codice_pdr_pod):
                    try:
                        amount = float(fornitura.get("ImportoFornitura", fornitura.get("Importo", 0)))
                    except (ValueError, TypeError):
                        amount = 0.0
                    break

            if amount > 0:
                self._total_invoiced += amount
                self._processed_invoices.add(invoice_number)
                _LOGGER.debug(
                    "Added invoice %s (€%.2f) to total, new total: €%.2f",
                    invoice_number,
                    amount,
                    self._total_invoiced,
                )

    @property
    def native_value(self) -> float:
        """Return the total invoiced amount."""
        return round(self._total_invoiced, 2)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes."""
        return {
            "pod": self._pod,
            "invoice_count": len(self._processed_invoices),
            "processed_invoice_numbers": list(self._processed_invoices),
        }
