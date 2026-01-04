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
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform, UnitOfEnergy
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

            # Sort by date (most recent first)
            all_data.sort(key=lambda x: x[0], reverse=True)
            most_recent_date, most_recent_data = all_data[0]

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

            # Update the last imported date
            if all_data:
                last_imported_date["date"] = all_data[0][1].get(
                    "data", all_data[0][0].strftime("%Y-%m-%d")
                )

            # Return the most recent day's data for the sensors
            return [most_recent_data]

        except EONEnergiaAuthError as err:
            raise UpdateFailed(f"Authentication failed: {err}") from err
        except EONEnergiaApiError as err:
            raise UpdateFailed(f"Error communicating with API: {err}") from err

    coordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        name=DOMAIN,
        update_method=async_update_data,
        update_interval=timedelta(hours=DEFAULT_SCAN_INTERVAL),
    )

    # Fetch initial data
    await coordinator.async_config_entry_first_refresh()

    hass.data[DOMAIN][entry.entry_id] = {
        "api": api,
        "coordinator": coordinator,
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
    which can be used by the Energy Dashboard.
    """
    is_multioraria = tariff_type == TARIFF_MULTIORARIA

    # Define statistics based on tariff type
    stat_configs: dict[str, dict[str, str]] = {
        "total": {
            "id": f"{DOMAIN}:{pod}_consumption",
            "name": f"EON Energia {pod} Consumption",
        },
    }

    if is_multioraria:
        stat_configs.update({
            "F1": {
                "id": f"{DOMAIN}:{pod}_consumption_f1",
                "name": f"EON Energia {pod} F1 (Peak)",
            },
            "F2": {
                "id": f"{DOMAIN}:{pod}_consumption_f2",
                "name": f"EON Energia {pod} F2 (Mid-peak)",
            },
            "F3": {
                "id": f"{DOMAIN}:{pod}_consumption_f3",
                "name": f"EON Energia {pod} F3 (Off-peak)",
            },
        })

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

            # Update total
            running_sums["total"] += hourly_value
            statistics["total"].append(
                StatisticData(
                    start=stat_time,
                    sum=running_sums["total"],
                    state=hourly_value,
                )
            )

            # Update fascia-specific statistics
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
                unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            )
            async_add_external_statistics(hass, metadata, statistics[key])

    data_date = day_data.get("data", date.strftime("%Y-%m-%d"))
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

    # Define statistics based on tariff type
    stat_configs: dict[str, dict[str, str]] = {
        "total": {
            "id": f"{DOMAIN}:{pod}_consumption",
            "name": f"EON Energia {pod} Consumption",
        },
    }

    # Only add fascia statistics for multioraria tariffs
    if is_multioraria:
        stat_configs.update({
            "F1": {
                "id": f"{DOMAIN}:{pod}_consumption_f1",
                "name": f"EON Energia {pod} F1 (Peak)",
            },
            "F2": {
                "id": f"{DOMAIN}:{pod}_consumption_f2",
                "name": f"EON Energia {pod} F2 (Mid-peak)",
            },
            "F3": {
                "id": f"{DOMAIN}:{pod}_consumption_f3",
                "name": f"EON Energia {pod} F3 (Off-peak)",
            },
        })

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

    _LOGGER.info(
        "Fetching EON Energia data from %s to %s (tariff: %s)",
        start_date.strftime("%Y-%m-%d"),
        end_date.strftime("%Y-%m-%d"),
        tariff_type,
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
                unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            )
            _LOGGER.info("Importing %d hourly statistics for %s", len(statistics[key]), config["name"])
            async_add_external_statistics(hass, metadata, statistics[key])

    _LOGGER.info("Historical data import completed for %s", pod)
