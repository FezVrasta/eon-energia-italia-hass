"""Writing E.ON's readings into Home Assistant's long-term statistics.

Home Assistant keeps a cumulative `sum` per hour and the Energy Dashboard renders
the difference between consecutive hours. That makes importing an hour a global
operation rather than a local one: writing an hour in the middle of an existing
series means renumbering everything after it, or the boundary shows up as a spike
and a matching negative reading.

Everything here exists to get that right, and to price the result from the
invoices E.ON have actually issued rather than an assumed tariff.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    statistics_during_period,
)
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import CURRENCY_EURO, UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from pyeonenergia import (
    EonEnergiaApiError,
    EonEnergiaClient,
    fascia_for_hour,
    parse_italian_date,
)

from .const import DOMAIN, TARIFF_MULTIORARIA

_LOGGER = logging.getLogger(__name__)


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


def _stat_row_start(row: dict[str, Any]) -> datetime:
    """Return a statistics row's start as an aware datetime.

    The recorder hands back a float timestamp on current versions and a
    datetime on older ones.
    """
    start = row["start"]
    if isinstance(start, (int, float)):
        return dt_util.utc_from_timestamp(start)
    return start


async def build_rebased_statistics(
    hass: HomeAssistant,
    statistic_id: str,
    new_values: dict[datetime, float],
) -> list[StatisticData]:
    """Build a statistics series whose cumulative ``sum`` stays monotonic.

    Home Assistant stores a running total per hour and the Energy Dashboard
    renders the difference between consecutive hours. That means an hour cannot
    be written in isolation: if it lands before existing data, every later hour
    has to be renumbered too.

    Seeding the running total from the *latest* stored row (which is what this
    integration used to do) and then writing earlier hours produced a spike
    where the rewritten range started and a negative reading where it rejoined
    untouched data. Both were visible on the Energy Dashboard.

    So: seed from the row immediately before the earliest hour being written,
    merge the new values over everything from that point on, and recompute the
    whole tail. Running it twice over the same data is a no-op.
    """
    if not new_values:
        return []

    first_start = min(new_values)

    # Seed from the last cumulative sum strictly before the rewritten range.
    prior = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        first_start - timedelta(days=730),
        first_start,
        [statistic_id],
        "hour",
        None,
        {"sum"},
    )
    running = 0.0
    if prior and prior.get(statistic_id):
        running = prior[statistic_id][-1].get("sum") or 0.0

    # Everything from the first affected hour onwards has to be renumbered.
    existing = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        first_start,
        None,
        [statistic_id],
        "hour",
        None,
        {"state"},
    )

    merged: dict[datetime, float] = {}
    if existing and existing.get(statistic_id):
        for row in existing[statistic_id]:
            if row.get("state") is not None:
                merged[_stat_row_start(row)] = float(row["state"])

    # Freshly fetched values win over whatever was stored before.
    merged.update(new_values)

    series: list[StatisticData] = []
    for start in sorted(merged):
        running += merged[start]
        series.append(StatisticData(start=start, sum=running, state=merged[start]))

    _LOGGER.debug(
        "Rebased %s: %d new hour(s), %d hour(s) rewritten from %s, seed sum=%.3f",
        statistic_id,
        len(new_values),
        len(series),
        first_start,
        running - sum(merged.values()),
    )
    return series


#: A plausible band for a price derived from one invoice over one month of kWh,
#: in EUR/kWh. Italian retail electricity sits far inside this; anything outside
#: it means the invoice and the consumption cover different periods.
MIN_DERIVED_PRICE = 0.05


MAX_DERIVED_PRICE = 1.50


async def import_days_batch(
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

    # Collect the hourly values per statistic. Cumulative sums are deliberately
    # NOT computed here: build_rebased_statistics() works them out against
    # whatever is already stored, so an overlapping re-import stays monotonic.
    new_values: dict[str, dict[datetime, float]] = {key: {} for key in stat_configs}

    for day, reading in days_data:
        # hours() resolves E.ON's 24 fields against the real local day, so the
        # spring-forward and autumn days come back with 23 and 24 entries rather
        # than a collision.
        for hour, stat_time, hourly_value in reading.hours():
            if hourly_value <= 0:
                continue

            new_values["total"][stat_time] = hourly_value

            fascia = None
            if is_multioraria:
                fascia = fascia_for_hour(day, hour)
                new_values[fascia][stat_time] = hourly_value

            if has_pricing:
                hourly_price, _ = _get_price_for_date(hass, pod, day.date(), fascia)
                if hourly_price:
                    new_values["cost"][stat_time] = hourly_value * hourly_price

    # Import all statistics at once
    for key, config in stat_configs.items():
        series = await build_rebased_statistics(hass, config["id"], new_values[key])
        if series:
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
            async_add_external_statistics(hass, metadata, series)

    _LOGGER.info(
        "Batch imported %d days of statistics (%.3f kWh across %d hours)",
        len(days_data),
        sum(new_values["total"].values()),
        len(new_values["total"]),
    )


#: The API times out server-side on long hourly ranges. A month at a time is
#: comfortably inside what it will answer.
HISTORY_CHUNK_DAYS = 30


#: Narrowest window worth retrying before giving up on it.
MIN_CHUNK_DAYS = 4


async def _fetch_consumption_chunked(
    api: EonEnergiaClient,
    pod: str,
    start_date: datetime,
    end_date: datetime,
    chunk_days: int = HISTORY_CHUNK_DAYS,
) -> list[dict[str, Any]] | None:
    """Fetch daily consumption over a long range, a chunk at a time.

    Returns the concatenated rows, or None if nothing could be fetched at all.
    Chunks that fail even at the minimum width are skipped with a warning rather
    than losing the rest of the range: a gap in one month should not cost the
    other eleven.
    """
    collected: list[dict[str, Any]] = []
    any_success = False
    window_start = start_date

    while window_start <= end_date:
        window_end = min(window_start + timedelta(days=chunk_days - 1), end_date)
        width = chunk_days

        while True:
            try:
                rows = await api.get_daily_consumption(
                    pod=pod, start_date=window_start, end_date=window_end
                )
                collected.extend(rows or [])
                any_success = True
                break
            except EonEnergiaApiError as err:
                if width <= MIN_CHUNK_DAYS:
                    _LOGGER.warning(
                        "Skipping %s..%s after repeated failures: %s",
                        window_start.strftime("%Y-%m-%d"),
                        window_end.strftime("%Y-%m-%d"),
                        err,
                    )
                    break
                width = max(width // 2, MIN_CHUNK_DAYS)
                window_end = min(window_start + timedelta(days=width - 1), end_date)
                _LOGGER.debug(
                    "Chunk failed (%s); retrying %s..%s at %d days",
                    err,
                    window_start.strftime("%Y-%m-%d"),
                    window_end.strftime("%Y-%m-%d"),
                    width,
                )

        window_start = window_end + timedelta(days=1)

    if not any_success:
        _LOGGER.error("Failed to fetch any consumption data for the requested range")
        return None

    _LOGGER.info(
        "Fetched %d days across %s..%s",
        len(collected),
        start_date.strftime("%Y-%m-%d"),
        end_date.strftime("%Y-%m-%d"),
    )
    return collected


async def import_historical_statistics(
    hass: HomeAssistant,
    api: EonEnergiaClient,
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
        "Starting import_historical_statistics for POD %s (days: %d, tariff: %s)",
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

    # Use timezone-aware dates to avoid DST issues
    # The API dates are in local Italian time, so we use that for consistency
    now = dt_util.now()  # Timezone-aware datetime
    end_date = now - timedelta(days=2)  # API has 2-day delay
    start_date = end_date - timedelta(days=days)

    # Hourly values only. This function used to seed a running sum from the data
    # before start_date, which got the head of the range right but left every
    # hour *after* the range holding its old total - a negative reading on the
    # Energy Dashboard where the two met. build_rebased_statistics() renumbers
    # the tail as well.
    new_values: dict[str, dict[datetime, float]] = {key: {} for key in stat_configs}

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

    # Fetch in chunks. Asking for a long range in one call makes the API time out
    # on its own side and answer HTTP 500 with "[1062] Read timed out" - a 200 day
    # request fails reliably, while a month's worth comes back fine. A failed chunk
    # is retried at half the width before being given up on, so one bad window
    # costs that window rather than the whole import.
    all_data = await _fetch_consumption_chunked(api, pod, start_date, end_date)
    if all_data is None:
        return daily_consumption

    if not all_data:
        _LOGGER.warning("No consumption data returned from API")
        return daily_consumption

    _LOGGER.info("Received %d days of consumption data", len(all_data))

    # Process each day's data
    for reading in all_data:
        if reading.day is None:
            continue

        date_str = reading.day.isoformat()
        current_date = datetime.combine(reading.day, time.min)
        day_total = 0.0

        for hour, stat_time, hourly_value in reading.hours():
            if hourly_value <= 0:
                continue

            day_total += hourly_value
            new_values["total"][stat_time] = hourly_value

            # Fascia-specific statistic (only for multioraria)
            fascia = None
            if is_multioraria:
                fascia = fascia_for_hour(current_date, hour)
                new_values[fascia][stat_time] = hourly_value

            if has_pricing:
                hourly_price, _ = _get_price_for_date(hass, pod, current_date.date(), fascia)
                if hourly_price:
                    new_values["cost"][stat_time] = hourly_value * hourly_price

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
        series = await build_rebased_statistics(hass, config["id"], new_values[key])
        if series:
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
            _LOGGER.info(
                "Importing %d new hourly statistics for %s (%d rewritten)",
                len(new_values[key]),
                config["name"],
                len(series),
            )
            async_add_external_statistics(hass, metadata, series)

    if has_pricing:
        _LOGGER.info(
            "Historical data import completed for %s (total: %.3f kWh, cost: €%.2f) - "
            "%d days from invoices, %d days estimated",
            pod,
            sum(new_values["total"].values()),
            sum(new_values.get("cost", {}).values()),
            invoiced_days,
            estimated_days,
        )
    else:
        _LOGGER.info(
            "Historical data import completed for %s (total: %.3f kWh, %d days)",
            pod,
            sum(new_values["total"].values()),
            len(daily_consumption),
        )

    return daily_consumption


async def import_invoice_cost_statistics(
    hass: HomeAssistant,
    api: EonEnergiaClient,
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
    except EonEnergiaApiError as err:
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
        # DataDocumento is not modelled; it only appears on some document types,
        # so fall back through raw before giving up.
        invoice_date = invoice.issued_on or parse_italian_date(
            invoice.raw.get("DataDocumento") or invoice.raw.get("Data")
        )
        if not invoice_date:
            continue

        forniture = invoice.raw.get("ListaForniture", [])
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
                            invoice.number,
                            target_year,
                            target_month,
                        )
                        break

                    price_per_kwh = amount / month_kwh

                    if not MIN_DERIVED_PRICE <= price_per_kwh <= MAX_DERIVED_PRICE:
                        # The invoice and the consumption cover different spans.
                        # It happens on the first month of data, where a full
                        # invoice is divided by however many days we managed to
                        # fetch: one account derived EUR7.16/kWh from EUR165 over
                        # 23 kWh, which then priced that month's whole cost graph.
                        # Better no price for the month than a wrong one.
                        _LOGGER.warning(
                            "Ignoring implausible price for %d-%02d: €%.2f over "
                            "%.2f kWh is €%.4f/kWh, outside €%.2f-€%.2f. The "
                            "invoice most likely covers a longer period than the "
                            "consumption data available for it",
                            target_year,
                            target_month,
                            amount,
                            month_kwh,
                            price_per_kwh,
                            MIN_DERIVED_PRICE,
                            MAX_DERIVED_PRICE,
                        )
                        break

                    monthly_prices[month_key] = price_per_kwh

                    _LOGGER.info(
                        "Invoice %s (€%.2f) for %d-%02d: %.2f kWh -> €%.4f/kWh",
                        invoice.number,
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
