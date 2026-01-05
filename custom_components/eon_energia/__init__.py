"""EON Energia integration for Home Assistant."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

import voluptuous as vol

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CURRENCY_EURO, Platform, UnitOfEnergy
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import EONEnergiaApi, EONEnergiaApiError, EONEnergiaAuthError
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_POD,
    CONF_REFRESH_TOKEN,
    CONF_TARIFF_TYPE,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    TARIFF_MULTIORARIA,
    INVOICE_SCAN_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up EON Energia from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    access_token = entry.data[CONF_ACCESS_TOKEN]
    refresh_token = entry.data.get(CONF_REFRESH_TOKEN)
    pod = entry.data[CONF_POD]

    def token_refresh_callback(new_access_token: str, new_refresh_token: str) -> None:
        """Handle token refresh by updating the config entry."""
        _LOGGER.info("Tokens refreshed, updating config entry")
        new_data = {
            **entry.data,
            CONF_ACCESS_TOKEN: new_access_token,
            CONF_REFRESH_TOKEN: new_refresh_token,
        }
        hass.config_entries.async_update_entry(entry, data=new_data)

    api = EONEnergiaApi(
        access_token=access_token,
        refresh_token=refresh_token,
        token_callback=token_refresh_callback,
    )

    # Validate the token (will auto-refresh if needed and refresh token is available)
    if not await api.validate_token():
        _LOGGER.error("Invalid EON Energia access token")
        await api.close()
        return False

    # Get tariff type, default to multioraria for backwards compatibility
    tariff_type = entry.data.get(CONF_TARIFF_TYPE, TARIFF_MULTIORARIA)

    # Track the last imported date to avoid re-importing
    last_imported_date: dict[str, str | None] = {"date": None}

    async def async_update_data():
        """Fetch data from EON Energia API and import statistics."""
        try:
            # EON data has a 2-day delay, try multiple days to find the most recent data
            all_data = []
            for days_ago in range(2, 8):  # Try from 2 to 7 days ago
                target_date = datetime.now() - timedelta(days=days_ago)
                data = await api.get_daily_consumption(
                    pod=pod,
                    start_date=target_date,
                    end_date=target_date,
                )
                if data and len(data) > 0:
                    all_data.append((target_date, data[0] if isinstance(data, list) else data))

            if not all_data:
                _LOGGER.warning("No consumption data found for the last 7 days")
                return []

            # Sort by date (oldest first for correct running sum calculation)
            all_data.sort(key=lambda x: x[0])
            most_recent_date, most_recent_data = all_data[-1]

            _LOGGER.debug(
                "Found consumption data for %s",
                most_recent_date.strftime("%Y-%m-%d"),
            )

            # Auto-import statistics for any new days we haven't processed yet
            for target_date, day_data in all_data:
                date_str = target_date.strftime("%Y-%m-%d")
                data_date = day_data.get("data", date_str)

                # Skip if we've already imported this date
                if last_imported_date["date"] and data_date <= last_imported_date["date"]:
                    continue

                # Import this day's hourly statistics
                await _import_day_statistics(
                    hass, day_data, target_date, pod, tariff_type
                )

            # Update the last imported date (use last item = most recent)
            if all_data:
                last_imported_date["date"] = all_data[-1][1].get(
                    "data", all_data[-1][0].strftime("%Y-%m-%d")
                )

            # Return the most recent day's data for the sensors
            return [most_recent_data]

        except EONEnergiaAuthError as err:
            raise UpdateFailed(f"Authentication failed: {err}") from err
        except EONEnergiaApiError as err:
            raise UpdateFailed(f"Error communicating with API: {err}") from err

    # Invoice coordinator (updates less frequently)
    # NOTE: Invoice coordinator is set up BEFORE consumption coordinator so that
    # the average €/kWh price is calculated before consumption statistics are imported.
    # This ensures cost statistics are available from the first data import.
    async def async_update_invoices():
        """Fetch invoice data from EON Energia API and import cost statistics."""
        try:
            invoices = await api.get_invoices_for_pod(pod)
            _LOGGER.debug("Fetched %d invoices for POD %s", len(invoices), pod)

            # Import cost statistics from invoices (also fetches per-fascia pricing)
            if invoices:
                await _import_invoice_cost_statistics(hass, api, invoices, pod)

            return invoices
        except EONEnergiaAuthError as err:
            raise UpdateFailed(f"Authentication failed: {err}") from err
        except EONEnergiaApiError as err:
            raise UpdateFailed(f"Error fetching invoices: {err}") from err

    invoice_coordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        name=f"{DOMAIN}_invoices",
        update_method=async_update_invoices,
        update_interval=timedelta(hours=INVOICE_SCAN_INTERVAL),
    )

    # Fetch initial invoice data first (to calculate average €/kWh for cost statistics)
    await invoice_coordinator.async_config_entry_first_refresh()

    coordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        name=DOMAIN,
        update_method=async_update_data,
        update_interval=timedelta(hours=DEFAULT_SCAN_INTERVAL),
    )

    # Fetch initial consumption data (will now include cost statistics if price was calculated)
    await coordinator.async_config_entry_first_refresh()

    hass.data[DOMAIN][entry.entry_id] = {
        "api": api,
        "coordinator": coordinator,
        "invoice_coordinator": invoice_coordinator,
        "pod": pod,
        "tariff_type": tariff_type,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register update listener for config entry changes
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    return True


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload config entry when options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        data = hass.data[DOMAIN].pop(entry.entry_id)
        await data["api"].close()

    return unload_ok


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the EON Energia component."""
    hass.data.setdefault(DOMAIN, {})

    async def handle_import_statistics(call: ServiceCall) -> None:
        """Handle the import_statistics service call."""
        days = call.data.get("days", 90)

        _LOGGER.info("Starting historical data import for the last %d days", days)

        # Get all configured entries
        for entry_id, entry_data in hass.data[DOMAIN].items():
            if not isinstance(entry_data, dict) or "api" not in entry_data:
                continue

            api = entry_data["api"]
            pod = entry_data["pod"]
            tariff_type = entry_data.get("tariff_type", TARIFF_MULTIORARIA)

            await _import_historical_statistics(hass, api, pod, days, tariff_type)

    hass.services.async_register(
        DOMAIN,
        "import_statistics",
        handle_import_statistics,
        schema=vol.Schema({
            vol.Optional("days", default=90): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=365)
            ),
        }),
    )

    return True


async def _import_day_statistics(
    hass: HomeAssistant,
    day_data: dict[str, Any],
    date: datetime,
    pod: str,
    tariff_type: str = TARIFF_MULTIORARIA,
) -> None:
    """Import a single day's hourly statistics to the recorder.

    This function imports hourly energy consumption data as external statistics,
    which can be used by the Energy Dashboard. It also imports cost statistics
    if an average price per kWh has been calculated from invoices.
    """
    is_multioraria = tariff_type == TARIFF_MULTIORARIA

    # Get pricing - prefer per-fascia prices for multioraria, fallback to single price
    price_per_kwh = hass.data[DOMAIN].get("price_per_kwh", {}).get(pod)
    fascia_prices = hass.data[DOMAIN].get("price_per_kwh_fascia", {}).get(pod, {})

    # Define statistics based on tariff type
    stat_configs: dict[str, dict[str, Any]] = {
        "total": {
            "id": f"{DOMAIN}:{pod}_consumption",
            "name": f"EON Energia {pod} Consumption",
            "unit": UnitOfEnergy.KILO_WATT_HOUR,
            "unit_class": SensorDeviceClass.ENERGY,
        },
    }

    if is_multioraria:
        stat_configs.update({
            "F1": {
                "id": f"{DOMAIN}:{pod}_consumption_f1",
                "name": f"EON Energia {pod} F1 (Peak)",
                "unit": UnitOfEnergy.KILO_WATT_HOUR,
                "unit_class": SensorDeviceClass.ENERGY,
            },
            "F2": {
                "id": f"{DOMAIN}:{pod}_consumption_f2",
                "name": f"EON Energia {pod} F2 (Mid-peak)",
                "unit": UnitOfEnergy.KILO_WATT_HOUR,
                "unit_class": SensorDeviceClass.ENERGY,
            },
            "F3": {
                "id": f"{DOMAIN}:{pod}_consumption_f3",
                "name": f"EON Energia {pod} F3 (Off-peak)",
                "unit": UnitOfEnergy.KILO_WATT_HOUR,
                "unit_class": SensorDeviceClass.ENERGY,
            },
        })

    # Add cost statistic if we have any price (single rate or per-fascia)
    if price_per_kwh or fascia_prices:
        stat_configs["cost"] = {
            "id": f"{DOMAIN}:{pod}_cost",
            "name": f"EON Energia {pod} Cost",
            "unit": CURRENCY_EURO,
            "unit_class": None,
        }

    # Get current running sums from existing statistics
    running_sums: dict[str, float] = {}
    for key, config in stat_configs.items():
        statistic_id = config["id"]
        last_stats = await get_instance(hass).async_add_executor_job(
            get_last_statistics, hass, 1, statistic_id, True, {"sum"}
        )
        if last_stats and statistic_id in last_stats:
            running_sums[key] = last_stats[statistic_id][0]["sum"]
        else:
            running_sums[key] = 0.0

    # Process each hourly value and create statistics
    statistics: dict[str, list[StatisticData]] = {key: [] for key in stat_configs}

    for hour in range(1, 25):
        field_key = f"valore_h{hour:02d}"
        if field_key not in day_data:
            continue

        try:
            hourly_value = float(day_data[field_key])
            if hourly_value <= 0:
                continue

            # Create statistic timestamp (hour 1 = 00:00-01:00)
            stat_time = dt_util.as_utc(
                datetime.combine(date.date(), datetime.min.time())
                + timedelta(hours=hour - 1)
            )

            # Update total consumption
            running_sums["total"] += hourly_value
            statistics["total"].append(
                StatisticData(
                    start=stat_time,
                    sum=running_sums["total"],
                    state=hourly_value,
                )
            )

            # Update fascia-specific statistics
            fascia = None
            if is_multioraria:
                fascia = _get_fascia_for_hour(date, hour)
                running_sums[fascia] += hourly_value
                statistics[fascia].append(
                    StatisticData(
                        start=stat_time,
                        sum=running_sums[fascia],
                        state=hourly_value,
                    )
                )

            # Update cost statistics - use per-fascia price if available
            if price_per_kwh or fascia_prices:
                # Determine price to use: per-fascia if available, otherwise single rate
                if fascia and fascia in fascia_prices:
                    hourly_price = fascia_prices[fascia]
                elif price_per_kwh:
                    hourly_price = price_per_kwh
                else:
                    hourly_price = None

                if hourly_price:
                    hourly_cost = hourly_value * hourly_price
                    running_sums["cost"] += hourly_cost
                    statistics["cost"].append(
                        StatisticData(
                            start=stat_time,
                            sum=running_sums["cost"],
                            state=hourly_cost,
                        )
                    )

        except (ValueError, TypeError):
            continue

    # Import statistics for each type
    for key, config in stat_configs.items():
        if statistics[key]:
            metadata = StatisticMetaData(
                has_mean=False,
                has_sum=True,
                mean_type=StatisticMeanType.NONE,
                name=config["name"],
                source=DOMAIN,
                statistic_id=config["id"],
                unit_of_measurement=config["unit"],
                unit_class=config["unit_class"],
            )
            async_add_external_statistics(hass, metadata, statistics[key])

    data_date = day_data.get("data", date.strftime("%Y-%m-%d"))
    if price_per_kwh or fascia_prices:
        _LOGGER.info(
            "Auto-imported %d hourly statistics for %s (total: %.3f kWh, cost: €%.2f%s)",
            len(statistics["total"]),
            data_date,
            running_sums["total"],
            running_sums.get("cost", 0),
            " using per-fascia pricing" if fascia_prices else "",
        )
    else:
        _LOGGER.info(
            "Auto-imported %d hourly statistics for %s (total: %.3f kWh)",
            len(statistics["total"]),
            data_date,
            running_sums["total"],
        )


def _get_fascia_for_hour(dt: datetime, hour: int) -> str:
    """Determine the tariff band (fascia) for a given datetime and hour.

    F1: Peak hours (Mon-Fri 8:00-19:00)
    F2: Mid-peak hours (Mon-Fri 7:00-8:00, 19:00-23:00, Sat 7:00-23:00)
    F3: Off-peak hours (nights 23:00-7:00, Sundays, holidays)

    Note: hour is 1-24 where hour 1 = 00:00-01:00, hour 24 = 23:00-00:00
    """
    # Convert hour (1-24) to 0-23 format for the START of the hour period
    hour_0_based = hour - 1

    weekday = dt.weekday()  # 0=Monday, 6=Sunday

    # Sunday is always F3
    if weekday == 6:
        return "F3"

    # Saturday
    if weekday == 5:
        if 7 <= hour_0_based < 23:
            return "F2"
        else:
            return "F3"

    # Monday to Friday
    if 8 <= hour_0_based < 19:
        return "F1"
    elif hour_0_based == 7 or 19 <= hour_0_based < 23:
        return "F2"
    else:
        return "F3"


async def _import_historical_statistics(
    hass: HomeAssistant,
    api: EONEnergiaApi,
    pod: str,
    days: int,
    tariff_type: str = TARIFF_MULTIORARIA,
) -> None:
    """Import historical statistics from EON Energia API."""
    is_multioraria = tariff_type == TARIFF_MULTIORARIA

    # Get pricing - prefer per-fascia prices for multioraria, fallback to single price
    price_per_kwh = hass.data[DOMAIN].get("price_per_kwh", {}).get(pod)
    fascia_prices = hass.data[DOMAIN].get("price_per_kwh_fascia", {}).get(pod, {})

    # Define statistics based on tariff type
    stat_configs: dict[str, dict[str, Any]] = {
        "total": {
            "id": f"{DOMAIN}:{pod}_consumption",
            "name": f"EON Energia {pod} Consumption",
            "unit": UnitOfEnergy.KILO_WATT_HOUR,
            "unit_class": SensorDeviceClass.ENERGY,
        },
    }

    # Only add fascia statistics for multioraria tariffs
    if is_multioraria:
        stat_configs.update({
            "F1": {
                "id": f"{DOMAIN}:{pod}_consumption_f1",
                "name": f"EON Energia {pod} F1 (Peak)",
                "unit": UnitOfEnergy.KILO_WATT_HOUR,
                "unit_class": SensorDeviceClass.ENERGY,
            },
            "F2": {
                "id": f"{DOMAIN}:{pod}_consumption_f2",
                "name": f"EON Energia {pod} F2 (Mid-peak)",
                "unit": UnitOfEnergy.KILO_WATT_HOUR,
                "unit_class": SensorDeviceClass.ENERGY,
            },
            "F3": {
                "id": f"{DOMAIN}:{pod}_consumption_f3",
                "name": f"EON Energia {pod} F3 (Off-peak)",
                "unit": UnitOfEnergy.KILO_WATT_HOUR,
                "unit_class": SensorDeviceClass.ENERGY,
            },
        })

    # Add cost statistic if we have any price (single rate or per-fascia)
    if price_per_kwh or fascia_prices:
        stat_configs["cost"] = {
            "id": f"{DOMAIN}:{pod}_cost",
            "name": f"EON Energia {pod} Cost",
            "unit": CURRENCY_EURO,
            "unit_class": None,
        }

    # Initialize running sums and statistics lists
    running_sums: dict[str, float] = {}
    statistics: dict[str, list[StatisticData]] = {}

    for key, config in stat_configs.items():
        statistic_id = config["id"]

        # Get last known statistic to continue from there
        last_stats = await get_instance(hass).async_add_executor_job(
            get_last_statistics, hass, 1, statistic_id, True, {"sum"}
        )

        if last_stats and statistic_id in last_stats:
            running_sums[key] = last_stats[statistic_id][0]["sum"]
            _LOGGER.debug("Last known sum for %s: %s", statistic_id, running_sums[key])
        else:
            running_sums[key] = 0.0
            _LOGGER.debug("No previous statistics found for %s, starting from 0", statistic_id)

        statistics[key] = []

    end_date = datetime.now() - timedelta(days=2)  # API has 2-day delay
    start_date = end_date - timedelta(days=days)

    price_info = "not available"
    if fascia_prices:
        price_info = f"F1=€{fascia_prices.get('F1', 0):.4f}, F2=€{fascia_prices.get('F2', 0):.4f}, F3=€{fascia_prices.get('F3', 0):.4f}/kWh"
    elif price_per_kwh:
        price_info = f"€{price_per_kwh:.4f}/kWh"

    _LOGGER.info(
        "Fetching EON Energia data from %s to %s (tariff: %s, price: %s)",
        start_date.strftime("%Y-%m-%d"),
        end_date.strftime("%Y-%m-%d"),
        tariff_type,
        price_info,
    )

    # Fetch data day by day
    current_date = start_date
    while current_date <= end_date:
        try:
            data = await api.get_daily_consumption(
                pod=pod,
                start_date=current_date,
                end_date=current_date,
            )

            if data and len(data) > 0:
                day_data = data[0]

                # Process each hourly value
                for hour in range(1, 25):
                    field_key = f"valore_h{hour:02d}"
                    if field_key in day_data:
                        try:
                            hourly_value = float(day_data[field_key])
                            if hourly_value > 0:
                                # Create statistic timestamp
                                # hour 1 = 00:00-01:00, hour 24 = 23:00-00:00
                                stat_time = dt_util.as_utc(
                                    datetime.combine(
                                        current_date.date(),
                                        datetime.min.time()
                                    ) + timedelta(hours=hour - 1)
                                )

                                # Update total
                                running_sums["total"] += hourly_value
                                statistics["total"].append(
                                    StatisticData(
                                        start=stat_time,
                                        sum=running_sums["total"],
                                        state=hourly_value,
                                    )
                                )

                                # Update fascia-specific statistic (only for multioraria)
                                fascia = None
                                if is_multioraria:
                                    fascia = _get_fascia_for_hour(current_date, hour)
                                    running_sums[fascia] += hourly_value
                                    statistics[fascia].append(
                                        StatisticData(
                                            start=stat_time,
                                            sum=running_sums[fascia],
                                            state=hourly_value,
                                        )
                                    )

                                # Update cost statistics - use per-fascia price if available
                                if price_per_kwh or fascia_prices:
                                    # Determine price to use: per-fascia if available, otherwise single rate
                                    if fascia and fascia in fascia_prices:
                                        hourly_price = fascia_prices[fascia]
                                    elif price_per_kwh:
                                        hourly_price = price_per_kwh
                                    else:
                                        hourly_price = None

                                    if hourly_price:
                                        hourly_cost = hourly_value * hourly_price
                                        running_sums["cost"] += hourly_cost
                                        statistics["cost"].append(
                                            StatisticData(
                                                start=stat_time,
                                                sum=running_sums["cost"],
                                                state=hourly_cost,
                                            )
                                        )

                        except (ValueError, TypeError):
                            pass

                if is_multioraria:
                    _LOGGER.debug(
                        "Processed %s: total=%.3f, F1=%.3f, F2=%.3f, F3=%.3f kWh",
                        current_date.strftime("%Y-%m-%d"),
                        running_sums["total"],
                        running_sums.get("F1", 0),
                        running_sums.get("F2", 0),
                        running_sums.get("F3", 0),
                    )
                else:
                    _LOGGER.debug(
                        "Processed %s: total=%.3f kWh",
                        current_date.strftime("%Y-%m-%d"),
                        running_sums["total"],
                    )

        except EONEnergiaApiError as err:
            _LOGGER.warning(
                "Failed to fetch data for %s: %s",
                current_date.strftime("%Y-%m-%d"),
                err,
            )

        current_date += timedelta(days=1)

    # Import statistics for each type
    for key, config in stat_configs.items():
        if statistics[key]:
            metadata = StatisticMetaData(
                has_mean=False,
                has_sum=True,
                mean_type=StatisticMeanType.NONE,
                name=config["name"],
                source=DOMAIN,
                statistic_id=config["id"],
                unit_of_measurement=config["unit"],
                unit_class=config["unit_class"],
            )
            _LOGGER.info("Importing %d hourly statistics for %s", len(statistics[key]), config["name"])
            async_add_external_statistics(hass, metadata, statistics[key])

    if price_per_kwh or fascia_prices:
        _LOGGER.info(
            "Historical data import completed for %s (total: %.3f kWh, cost: €%.2f%s)",
            pod,
            running_sums["total"],
            running_sums.get("cost", 0),
            " using per-fascia pricing" if fascia_prices else "",
        )
    else:
        _LOGGER.info("Historical data import completed for %s", pod)


def _calculate_average_price_per_kwh(
    invoices: list[dict[str, Any]],
    pod: str,
) -> float | None:
    """Calculate average price per kWh from invoices.

    This examines all invoices and calculates an average €/kWh rate
    that can be used to estimate costs from consumption data.

    Returns:
        Average price per kWh in EUR, or None if cannot be calculated.
    """
    total_cost = 0.0
    total_kwh = 0.0

    for invoice in invoices:
        forniture = invoice.get("ListaForniture", [])
        for fornitura in forniture:
            # Check both CodiceFornitura and CodicePDR_POD
            codice_fornitura = fornitura.get("CodiceFornitura", "")
            codice_pdr_pod = fornitura.get("CodicePDR_POD", "")
            if pod in (codice_fornitura, codice_pdr_pod):
                try:
                    cost = float(fornitura.get("ImportoFornitura", fornitura.get("Importo", 0)))
                    # Try to get consumption from the invoice if available
                    kwh = float(fornitura.get("Consumo", 0))
                    if cost > 0 and kwh > 0:
                        total_cost += cost
                        total_kwh += kwh
                except (ValueError, TypeError):
                    continue
                break

    if total_kwh > 0:
        return total_cost / total_kwh

    # Fallback: estimate from total invoice amounts
    # Use a typical household average if we can't calculate
    if total_cost > 0:
        _LOGGER.debug(
            "Could not calculate €/kWh from consumption data, using invoice totals"
        )

    return None


async def _import_invoice_cost_statistics(
    hass: HomeAssistant,
    api: EONEnergiaApi,
    invoices: list[dict[str, Any]],
    pod: str,
) -> None:
    """Import invoice cost statistics to the recorder.

    This function fetches per-fascia pricing from the energy wallet endpoint
    and calculates average €/kWh rates for each fascia (F1, F2, F3) and total.
    Fixed costs (transport, system charges, taxes) are distributed per-kWh and
    added to the energy price to get accurate total costs.
    The rates are stored in hass.data for use when importing consumption statistics.
    """
    if not invoices:
        return

    # Try to get per-fascia pricing from the energy wallet endpoint
    fascia_prices: dict[str, list[float]] = {"F0": [], "F1": [], "F2": [], "F3": []}
    # Track fixed costs per kWh to add to energy prices
    fixed_costs_per_kwh: list[float] = []

    # Get the most recent invoice to fetch energy wallet data
    for invoice in invoices[:5]:  # Check the 5 most recent invoices
        invoice_number = invoice.get("Numero") or invoice.get("NumeroDocumento")
        invoice_date = invoice.get("DataEmissione") or invoice.get("DataDocumento", "")

        if not invoice_number:
            continue

        # Extract year from invoice date (format: "DD/MM/YYYY")
        try:
            if "/" in invoice_date:
                year = invoice_date.split("/")[-1]
            else:
                year = str(datetime.now().year)
        except (IndexError, ValueError):
            year = str(datetime.now().year)

        try:
            wallet_data = await api.get_energy_wallet(invoice_number, year)

            # Get the POD-specific data from codicePR array
            codice_pr_list = wallet_data.get("codicePR", [])

            for pr_data in codice_pr_list:
                # Extract per-fascia pricing from componenteEnergia (nested in codicePR)
                componente_energia = pr_data.get("componenteEnergia", [])
                for component in componente_energia:
                    time_slot = component.get("timeSlot", "")
                    price_str = component.get("price", "0")

                    try:
                        price = float(price_str)
                        if price > 0 and time_slot in fascia_prices:
                            fascia_prices[time_slot].append(price)
                    except (ValueError, TypeError):
                        continue

                # Calculate fixed costs per kWh from other cost components
                # These include: fixed charges, power charges, taxes, etc.
                total_fixed_cost = 0.0
                total_kwh = 0.0

                # Get total consumption from consumoTotaleFatturato
                consumo_list = pr_data.get("consumoTotaleFatturato", [])
                for consumo in consumo_list:
                    try:
                        kwh = float(consumo.get("consume", 0))
                        total_kwh += kwh
                    except (ValueError, TypeError):
                        pass

                # Get fixed costs from amountResume
                # Fixed costs = "Quota fissa e quota potenza" + "Accise e IVA"
                # (consumption-based costs are already in the per-kWh price)
                amount_resume = pr_data.get("amountResume", [])
                for cost_group in amount_resume:
                    macro_group = cost_group.get("macroGroup", "")
                    # Include fixed charges and taxes, exclude consumption-based costs
                    if macro_group in [
                        "Quota fissa e quota potenza",
                        "Accise e IVA",
                        "Canone Rai",  # RAI TV license if present
                    ]:
                        try:
                            amount = float(cost_group.get("macroGroupAmount", 0))
                            total_fixed_cost += amount
                        except (ValueError, TypeError):
                            pass

                # Calculate fixed cost per kWh if we have both values
                if total_kwh > 0 and total_fixed_cost > 0:
                    fixed_per_kwh = total_fixed_cost / total_kwh
                    fixed_costs_per_kwh.append(fixed_per_kwh)
                    _LOGGER.debug(
                        "Invoice %s: fixed costs €%.2f / %.2f kWh = €%.4f/kWh",
                        invoice_number,
                        total_fixed_cost,
                        total_kwh,
                        fixed_per_kwh,
                    )

            _LOGGER.debug(
                "Fetched energy wallet for invoice %s: %s",
                invoice_number,
                {k: v for k, v in fascia_prices.items() if v},
            )

        except EONEnergiaApiError as err:
            _LOGGER.debug(
                "Could not fetch energy wallet for invoice %s: %s",
                invoice_number,
                err,
            )
            continue

    # Calculate average fixed cost per kWh
    avg_fixed_cost_per_kwh = 0.0
    if fixed_costs_per_kwh:
        avg_fixed_cost_per_kwh = sum(fixed_costs_per_kwh) / len(fixed_costs_per_kwh)
        _LOGGER.info(
            "Average fixed costs for %s: €%.4f/kWh",
            pod,
            avg_fixed_cost_per_kwh,
        )

    # Calculate average prices per fascia (energy price + fixed costs distributed per kWh)
    avg_prices: dict[str, float] = {}

    # Check if we have F1/F2/F3 pricing (multioraria) or just F0 (monoraria)
    has_fascia_pricing = any(fascia_prices.get(f) for f in ["F1", "F2", "F3"])

    if has_fascia_pricing:
        # Use per-fascia pricing + fixed costs
        for fascia in ["F1", "F2", "F3"]:
            if fascia_prices[fascia]:
                energy_price = sum(fascia_prices[fascia]) / len(fascia_prices[fascia])
                # Add fixed costs per kWh to get total effective price
                avg_prices[fascia] = energy_price + avg_fixed_cost_per_kwh

        if avg_prices:
            _LOGGER.info(
                "Calculated per-fascia electricity prices for %s (including fixed costs €%.4f/kWh): "
                "F1=€%.4f/kWh, F2=€%.4f/kWh, F3=€%.4f/kWh",
                pod,
                avg_fixed_cost_per_kwh,
                avg_prices.get("F1", 0),
                avg_prices.get("F2", 0),
                avg_prices.get("F3", 0),
            )
            # Store per-fascia prices (already include fixed costs)
            hass.data[DOMAIN].setdefault("price_per_kwh_fascia", {})[pod] = avg_prices

            # Also calculate a weighted average for total cost (using typical distribution)
            # F1: ~30%, F2: ~25%, F3: ~45% (typical household)
            if all(f in avg_prices for f in ["F1", "F2", "F3"]):
                weighted_avg = (
                    avg_prices["F1"] * 0.30 +
                    avg_prices["F2"] * 0.25 +
                    avg_prices["F3"] * 0.45
                )
                hass.data[DOMAIN].setdefault("price_per_kwh", {})[pod] = weighted_avg
    elif fascia_prices["F0"]:
        # Single rate (monoraria) - use F0 price + fixed costs
        energy_price = sum(fascia_prices["F0"]) / len(fascia_prices["F0"])
        total_price = energy_price + avg_fixed_cost_per_kwh
        _LOGGER.info(
            "Calculated single-rate electricity price for %s: €%.4f/kWh "
            "(energy: €%.4f + fixed: €%.4f)",
            pod,
            total_price,
            energy_price,
            avg_fixed_cost_per_kwh,
        )
        hass.data[DOMAIN].setdefault("price_per_kwh", {})[pod] = total_price
    else:
        # Fallback: calculate from invoice totals
        avg_price = _calculate_average_price_per_kwh(invoices, pod)

        if avg_price is not None:
            _LOGGER.info(
                "Calculated average electricity price for %s: €%.4f/kWh (from invoices)",
                pod,
                avg_price,
            )
            hass.data[DOMAIN].setdefault("price_per_kwh", {})[pod] = avg_price
        else:
            # Try to estimate from total costs and consumption statistics
            consumption_stat_id = f"{DOMAIN}:{pod}_consumption"
            last_stats = await get_instance(hass).async_add_executor_job(
                get_last_statistics, hass, 1, consumption_stat_id, True, {"sum"}
            )

            total_kwh = 0.0
            if last_stats and consumption_stat_id in last_stats:
                total_kwh = last_stats[consumption_stat_id][0].get("sum", 0)

            # Calculate total invoice cost for this POD
            total_cost = 0.0
            for invoice in invoices:
                forniture = invoice.get("ListaForniture", [])
                for fornitura in forniture:
                    codice_fornitura = fornitura.get("CodiceFornitura", "")
                    codice_pdr_pod = fornitura.get("CodicePDR_POD", "")
                    if pod in (codice_fornitura, codice_pdr_pod):
                        try:
                            total_cost += float(fornitura.get("ImportoFornitura", fornitura.get("Importo", 0)))
                        except (ValueError, TypeError):
                            pass
                        break

            if total_kwh > 0 and total_cost > 0:
                avg_price = total_cost / total_kwh
                _LOGGER.info(
                    "Estimated average electricity price for %s: €%.4f/kWh "
                    "(from €%.2f / %.2f kWh)",
                    pod,
                    avg_price,
                    total_cost,
                    total_kwh,
                )
                hass.data[DOMAIN].setdefault("price_per_kwh", {})[pod] = avg_price
            else:
                _LOGGER.debug(
                    "Could not calculate average price for %s "
                    "(total_cost=€%.2f, total_kwh=%.2f)",
                    pod,
                    total_cost,
                    total_kwh,
                )
