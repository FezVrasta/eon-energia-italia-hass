"""EON Energia integration for Home Assistant."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
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

# Fixed Italian national holidays (month, day)
ITALIAN_HOLIDAYS_FIXED = [
    (1, 1),    # Capodanno (New Year's Day)
    (1, 6),    # Epifania (Epiphany)
    (4, 25),   # Festa della Liberazione (Liberation Day)
    (5, 1),    # Festa dei Lavoratori (Labour Day)
    (6, 2),    # Festa della Repubblica (Republic Day)
    (8, 15),   # Ferragosto (Assumption of Mary)
    (11, 1),   # Ognissanti (All Saints' Day)
    (12, 8),   # Immacolata Concezione (Immaculate Conception)
    (12, 25),  # Natale (Christmas Day)
    (12, 26),  # Santo Stefano (St. Stephen's Day)
]


def _calculate_easter(year: int) -> date:
    """Calculate Easter Sunday for a given year using Anonymous Gregorian algorithm."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _is_italian_holiday(dt: datetime) -> bool:
    """Check if a date is an Italian national holiday."""
    # Check fixed holidays
    if (dt.month, dt.day) in ITALIAN_HOLIDAYS_FIXED:
        return True

    # Check Easter Monday (Pasquetta) - the only moving holiday on a weekday
    easter = _calculate_easter(dt.year)
    easter_monday = easter + timedelta(days=1)
    if dt.month == easter_monday.month and dt.day == easter_monday.day:
        return True

    return False


def _parse_italian_date(date_str: str | None) -> date | None:
    """Parse Italian date format DD/MM/YYYY."""
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str, "%d/%m/%Y").date()
    except ValueError:
        return None


def _get_price_for_date(
    hass: HomeAssistant,
    pod: str,
    target_date: date,
    fascia: str | None = None,
) -> tuple[float | None, bool]:
    """Get the price per kWh for a POD for a specific month.

    Looks up the per-month price calculated from invoices.
    Returns None for months that haven't been invoiced yet.

    Returns:
        Tuple of (price, True) if price is available for this month,
        (None, False) otherwise.
    """
    monthly_prices = hass.data[DOMAIN].get("price_per_kwh_monthly", {}).get(pod, {})
    month_key = (target_date.year, target_date.month)
    if month_key in monthly_prices:
        return (monthly_prices[month_key], True)
    return (None, False)


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
    # Also track if historical import is in progress to prevent conflicts
    import_state: dict[str, Any] = {"last_date": None, "importing_historical": False}

    async def async_update_data():
        """Fetch data from EON Energia API and import statistics."""
        # Skip auto-import if historical import is in progress
        if import_state.get("importing_historical"):
            _LOGGER.debug("Skipping auto-import: historical import in progress")
            # Still fetch and return data for sensors, just don't import statistics
            try:
                for days_ago in range(2, 8):
                    target_date = datetime.now() - timedelta(days=days_ago)
                    data = await api.get_daily_consumption(
                        pod=pod,
                        start_date=target_date,
                        end_date=target_date,
                    )
                    if data and len(data) > 0:
                        return [data[0] if isinstance(data, list) else data]
                return []
            except EONEnergiaApiError:
                return []

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
            # Filter to only days we haven't imported
            days_to_import = []
            for target_date, day_data in all_data:
                date_str = target_date.strftime("%Y-%m-%d")
                data_date = day_data.get("data", date_str)

                # Skip if we've already imported this date
                if import_state["last_date"] and data_date <= import_state["last_date"]:
                    continue
                days_to_import.append((target_date, day_data))

            # Import all new days as a batch to maintain correct running sums
            if days_to_import:
                await _import_days_batch(
                    hass, days_to_import, pod, tariff_type
                )

            # Update the last imported date (use last item = most recent)
            if all_data:
                import_state["last_date"] = all_data[-1][1].get(
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
    # Use non-blocking refresh so integration still loads if invoice API is down
    try:
        await invoice_coordinator.async_config_entry_first_refresh()
    except Exception as err:
        _LOGGER.warning(
            "Could not fetch invoice data during setup (will retry later): %s",
            err,
        )
        # Don't fail setup - invoices are optional, consumption data is the priority

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
        "import_state": import_state,  # Share import state with service handler
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

        # Get all configured entries - create a copy to avoid "dictionary changed size during iteration"
        entries_found = 0
        for entry_id, entry_data in list(hass.data[DOMAIN].items()):
            _LOGGER.debug(
                "Checking entry %s: is_dict=%s, has_api=%s",
                entry_id,
                isinstance(entry_data, dict),
                "api" in entry_data if isinstance(entry_data, dict) else False,
            )
            if not isinstance(entry_data, dict) or "api" not in entry_data:
                continue

            entries_found += 1
            api = entry_data["api"]
            pod = entry_data["pod"]
            tariff_type = entry_data.get("tariff_type", TARIFF_MULTIORARIA)

            _LOGGER.info(
                "Importing statistics for POD %s (tariff: %s, days: %d)",
                pod,
                tariff_type,
                days,
            )

            # Set flag to prevent concurrent imports from coordinator
            import_state = entry_data.get("import_state", {})
            import_state["importing_historical"] = True

            # Ensure invoice data is loaded for pricing (but don't fail if it errors)
            invoice_coordinator = entry_data.get("invoice_coordinator")
            if invoice_coordinator:
                _LOGGER.debug("Refreshing invoice data before import...")
                try:
                    await invoice_coordinator.async_request_refresh()
                except Exception as err:
                    _LOGGER.warning(
                        "Could not refresh invoice data (will import without cost): %s",
                        err,
                    )

            try:
                # First import consumption statistics (without cost)
                # This returns daily consumption data we can use for price calculation
                daily_consumption = await _import_historical_statistics(
                    hass, api, pod, days, tariff_type
                )

                # Now that we have consumption data, calculate invoice prices
                if invoice_coordinator and invoice_coordinator.data:
                    _LOGGER.info(
                        "Calculating invoice prices using %d days of consumption data...",
                        len(daily_consumption),
                    )
                    await _import_invoice_cost_statistics(
                        hass, api, invoice_coordinator.data, pod, daily_consumption
                    )

                    # Re-import statistics to include cost data
                    _LOGGER.info("Re-importing statistics with cost data...")
                    await _import_historical_statistics(hass, api, pod, days, tariff_type)

                # Update last imported date to prevent coordinator from re-importing
                # these dates with potentially different sums
                end_date = datetime.now() - timedelta(days=2)
                import_state["last_date"] = end_date.strftime("%Y-%m-%d")
            finally:
                import_state["importing_historical"] = False

        if entries_found == 0:
            _LOGGER.warning(
                "No configured EON Energia entries found. "
                "Available keys in domain data: %s",
                list(hass.data[DOMAIN].keys()),
            )

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


async def _import_days_batch(
    hass: HomeAssistant,
    days_data: list[tuple[datetime, dict[str, Any]]],
    pod: str,
    tariff_type: str = TARIFF_MULTIORARIA,
) -> None:
    """Import multiple days' statistics in a single batch with correct running sums.

    This function processes multiple days sequentially, maintaining proper cumulative
    sums across all days. It retrieves the last known sum once at the start and
    then builds on it for all subsequent entries.
    """
    if not days_data:
        return

    is_multioraria = tariff_type == TARIFF_MULTIORARIA

    # Check if we have any pricing available
    monthly_prices = hass.data[DOMAIN].get("price_per_kwh_monthly", {}).get(pod, {})
    has_pricing = bool(monthly_prices)

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

    if has_pricing:
        stat_configs["cost"] = {
            "id": f"{DOMAIN}:{pod}_cost",
            "name": f"EON Energia {pod} Cost",
            "unit": CURRENCY_EURO,
            "unit_class": None,
        }

    # Get current running sums from existing statistics ONCE at the start
    running_sums: dict[str, float] = {}
    for key, config in stat_configs.items():
        statistic_id = config["id"]
        last_stats = await get_instance(hass).async_add_executor_job(
            get_last_statistics, hass, 1, statistic_id, True, {"sum", "start"}
        )
        if last_stats and statistic_id in last_stats:
            last_entry = last_stats[statistic_id][0]
            running_sums[key] = last_entry["sum"]
            _LOGGER.debug(
                "Batch import: found existing %s sum=%.3f from start=%s",
                statistic_id,
                last_entry["sum"],
                last_entry.get("start"),
            )
        else:
            running_sums[key] = 0.0
            _LOGGER.debug("Batch import: no existing data for %s, starting from 0", statistic_id)

    # Process all days and build statistics
    statistics: dict[str, list[StatisticData]] = {key: [] for key in stat_configs}

    for date, day_data in days_data:
        for hour in range(1, 25):
            field_key = f"valore_h{hour:02d}"
            if field_key not in day_data:
                continue

            try:
                hourly_value = float(day_data[field_key])
                if hourly_value <= 0:
                    continue

                # Create statistic timestamp
                local_day_start = dt_util.start_of_local_day(date)
                stat_time = local_day_start + timedelta(hours=hour - 1)

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

                # Update cost statistics
                if has_pricing:
                    hourly_price, _ = _get_price_for_date(hass, pod, date.date(), fascia)
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

    # Import all statistics at once
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

    _LOGGER.info(
        "Batch imported %d days of statistics (total: %.3f kWh)",
        len(days_data),
        running_sums["total"],
    )


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
    using date-specific pricing from invoices when available.
    """
    is_multioraria = tariff_type == TARIFF_MULTIORARIA

    # Check if we have any pricing available (for cost statistic setup)
    monthly_prices = hass.data[DOMAIN].get("price_per_kwh_monthly", {}).get(pod, {})
    has_pricing = bool(monthly_prices)

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

    # Add cost statistic if we have any pricing available
    if has_pricing:
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
            get_last_statistics, hass, 1, statistic_id, True, {"sum", "start"}
        )
        if last_stats and statistic_id in last_stats:
            last_entry = last_stats[statistic_id][0]
            running_sums[key] = last_entry["sum"]
            _LOGGER.debug(
                "Day import: found existing %s sum=%.3f from start=%s",
                statistic_id,
                last_entry["sum"],
                last_entry.get("start"),
            )
        else:
            running_sums[key] = 0.0
            _LOGGER.debug("Day import: no existing data for %s, starting from 0", statistic_id)

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
            # Use dt_util.start_of_local_day for proper timezone handling
            local_day_start = dt_util.start_of_local_day(date)
            stat_time = local_day_start + timedelta(hours=hour - 1)

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

            # Update cost statistics - use date-specific pricing from invoices
            if has_pricing:
                hourly_price, is_from_invoice = _get_price_for_date(
                    hass, pod, date.date(), fascia
                )

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

    # Track whether we used invoice pricing or fallback
    _, is_from_invoice = _get_price_for_date(hass, pod, date.date())
    pricing_source = "from invoice" if is_from_invoice else "estimated"

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
    if has_pricing:
        _LOGGER.info(
            "Auto-imported %d hourly statistics for %s (total: %.3f kWh, cost: €%.2f - %s)",
            len(statistics["total"]),
            data_date,
            running_sums["total"],
            running_sums.get("cost", 0),
            pricing_source,
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
    F3: Off-peak hours (nights 23:00-7:00, Sundays, Italian national holidays)

    Note: hour is 1-24 where hour 1 = 00:00-01:00, hour 24 = 23:00-00:00
    """
    # Convert hour (1-24) to 0-23 format for the START of the hour period
    hour_0_based = hour - 1

    weekday = dt.weekday()  # 0=Monday, 6=Sunday

    # Sundays and Italian national holidays are always F3
    if weekday == 6 or _is_italian_holiday(dt):
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
) -> dict[str, float]:
    """Import historical statistics from EON Energia API.

    Returns:
        Dict mapping date strings (YYYY-MM-DD) to daily kWh consumption totals.
        This can be used to calculate invoice prices.
    """
    _LOGGER.info(
        "Starting _import_historical_statistics for POD %s (days: %d, tariff: %s)",
        pod,
        days,
        tariff_type,
    )

    is_multioraria = tariff_type == TARIFF_MULTIORARIA

    # Check if we have any pricing available (for cost statistic setup)
    monthly_prices = hass.data[DOMAIN].get("price_per_kwh_monthly", {}).get(pod, {})
    has_pricing = bool(monthly_prices)

    _LOGGER.info(
        "Pricing info: %d months with prices: %s",
        len(monthly_prices),
        list(monthly_prices.keys()) if monthly_prices else "none",
    )

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

    # Add cost statistic if we have any pricing available
    if has_pricing:
        stat_configs["cost"] = {
            "id": f"{DOMAIN}:{pod}_cost",
            "name": f"EON Energia {pod} Cost",
            "unit": CURRENCY_EURO,
            "unit_class": None,
        }

    # Initialize running sums and statistics lists
    # For historical import, we always start from 0 since we're importing a fresh set of data
    # The async_add_external_statistics will handle merging/replacing existing data
    running_sums: dict[str, float] = {}
    statistics: dict[str, list[StatisticData]] = {}

    for key, config in stat_configs.items():
        # Start from 0 for historical import - we're rebuilding the statistics
        running_sums[key] = 0.0
        statistics[key] = []
        _LOGGER.debug("Initializing %s sum to 0 for historical import", config["id"])

    # Use timezone-aware dates to avoid DST issues
    # The API dates are in local Italian time, so we use that for consistency
    now = dt_util.now()  # Timezone-aware datetime
    end_date = now - timedelta(days=2)  # API has 2-day delay
    start_date = end_date - timedelta(days=days)

    # Count invoiced vs estimated days for logging
    invoiced_days = 0
    estimated_days = 0

    # Track daily consumption for invoice price calculation
    daily_consumption: dict[str, float] = {}

    _LOGGER.info(
        "Fetching EON Energia data from %s to %s (tariff: %s)",
        start_date.strftime("%Y-%m-%d"),
        end_date.strftime("%Y-%m-%d"),
        tariff_type,
    )

    # Fetch all data in one API call
    try:
        all_data = await api.get_daily_consumption(
            pod=pod,
            start_date=start_date,
            end_date=end_date,
        )
    except EONEnergiaApiError as err:
        _LOGGER.error("Failed to fetch consumption data: %s", err)
        return daily_consumption

    if not all_data:
        _LOGGER.warning("No consumption data returned from API")
        return daily_consumption

    _LOGGER.info("Received %d days of consumption data", len(all_data))

    # Process each day's data
    for day_data in all_data:
        # Parse the date from the data
        date_str = day_data.get("data")
        if not date_str:
            continue

        try:
            current_date = datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            continue

        day_total = 0.0

        # Process each hourly value
        for hour in range(1, 25):
            field_key = f"valore_h{hour:02d}"
            if field_key not in day_data:
                continue

            try:
                raw_value = day_data[field_key]
                hourly_value = float(raw_value) if raw_value is not None else 0.0

                if hourly_value <= 0:
                    continue

                day_total += hourly_value

                # Create statistic timestamp
                local_day_start = dt_util.start_of_local_day(current_date)
                stat_time = local_day_start + timedelta(hours=hour - 1)

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

                # Update cost statistics
                if has_pricing:
                    hourly_price, _ = _get_price_for_date(hass, pod, current_date.date(), fascia)
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

        # Store daily consumption for invoice price calculation
        if day_total > 0:
            daily_consumption[date_str] = day_total

        # Track pricing status
        _, is_from_invoice = _get_price_for_date(hass, pod, current_date.date())
        if is_from_invoice:
            invoiced_days += 1
        else:
            estimated_days += 1

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

    if has_pricing:
        _LOGGER.info(
            "Historical data import completed for %s (total: %.3f kWh, cost: €%.2f) - "
            "%d days from invoices, %d days estimated",
            pod,
            running_sums["total"],
            running_sums.get("cost", 0),
            invoiced_days,
            estimated_days,
        )
    else:
        _LOGGER.info(
            "Historical data import completed for %s (total: %.3f kWh, %d days)",
            pod,
            running_sums["total"],
            len(daily_consumption),
        )

    return daily_consumption


async def _import_invoice_cost_statistics(
    hass: HomeAssistant,
    api: EONEnergiaApi,
    invoices: list[dict[str, Any]],
    pod: str,
    daily_consumption: dict[str, float] | None = None,
) -> None:
    """Calculate per-month €/kWh from invoices and official monthly consumption.

    Uses the ExtMonthlyConsumption API to get official monthly kWh values,
    then matches each invoice to its month and calculates €/kWh.
    """
    if not invoices:
        _LOGGER.warning("Cannot calculate prices: no invoices")
        return

    # Fetch official monthly consumption from API
    start_date = datetime.now() - timedelta(days=365 * 2)
    end_date = datetime.now()

    try:
        monthly_data = await api.get_monthly_consumption(
            pod=pod,
            start_date=start_date,
            end_date=end_date,
        )
    except EONEnergiaApiError as err:
        _LOGGER.error("Failed to fetch monthly consumption: %s", err)
        return

    # Build monthly consumption lookup: (year, month) -> kWh
    monthly_consumption: dict[tuple[int, int], float] = {}
    for record in monthly_data:
        data_str = record.get("data")  # Format: "2025-11-01"
        kwh = record.get("valore_mensile", 0)
        if data_str and kwh:
            try:
                record_date = datetime.strptime(data_str, "%Y-%m-%d")
                month_key = (record_date.year, record_date.month)
                monthly_consumption[month_key] = float(kwh)
            except (ValueError, TypeError):
                continue

    _LOGGER.info(
        "Monthly consumption from API: %s",
        {f"{y}-{m:02d}": f"{kwh:.2f} kWh" for (y, m), kwh in sorted(monthly_consumption.items())},
    )

    # Process each invoice
    monthly_prices: dict[tuple[int, int], float] = {}

    for invoice in invoices:
        invoice_date_str = (
            invoice.get("DataDocumento")
            or invoice.get("DataEmissione")
            or invoice.get("Data")
        )
        invoice_date = _parse_italian_date(invoice_date_str)
        if not invoice_date:
            continue

        forniture = invoice.get("ListaForniture", [])
        for fornitura in forniture:
            codice_fornitura = fornitura.get("CodiceFornitura", "")
            codice_pdr_pod = fornitura.get("CodicePDR_POD", "")
            if pod in (codice_fornitura, codice_pdr_pod):
                try:
                    amount = float(
                        fornitura.get("ImportoFornitura") or fornitura.get("Importo", 0)
                    )
                    if amount <= 0:
                        break

                    # Invoice emitted in month M covers month M-1
                    target_month = invoice_date.month - 1
                    target_year = invoice_date.year
                    if target_month == 0:
                        target_month = 12
                        target_year -= 1

                    month_key = (target_year, target_month)
                    month_kwh = monthly_consumption.get(month_key, 0.0)

                    if month_kwh <= 0:
                        _LOGGER.debug(
                            "No consumption data for invoice %s month %d-%02d",
                            invoice.get("Numero"),
                            target_year,
                            target_month,
                        )
                        break

                    price_per_kwh = amount / month_kwh
                    monthly_prices[month_key] = price_per_kwh

                    _LOGGER.info(
                        "Invoice %s (€%.2f) for %d-%02d: %.2f kWh -> €%.4f/kWh",
                        invoice.get("Numero"),
                        amount,
                        target_year,
                        target_month,
                        month_kwh,
                        price_per_kwh,
                    )
                except (ValueError, TypeError):
                    pass
                break

    if not monthly_prices:
        _LOGGER.warning("No monthly prices could be calculated")
        return

    hass.data[DOMAIN].setdefault("price_per_kwh_monthly", {})[pod] = monthly_prices

    _LOGGER.info(
        "Calculated monthly prices for %s: %s",
        pod,
        {f"{y}-{m:02d}": f"€{p:.4f}/kWh" for (y, m), p in sorted(monthly_prices.items())},
    )
