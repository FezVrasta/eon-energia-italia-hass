"""Polling for E.ON Energia.

Two coordinators, because the two things they fetch move at completely different
speeds: hourly readings arrive daily and are polled every six hours, invoices
arrive monthly and are polled daily.

The invoice poll runs first at setup, deliberately. Costs are derived from the
invoices, so the consumption import needs the prices to already be in hand or the
first day of statistics lands without a cost.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from pyeonenergia import (
    EonEnergiaApiError,
    EonEnergiaAuthError,
    EonEnergiaClient,
    HourlyConsumption,
    Invoice,
)

from .const import DEFAULT_SCAN_INTERVAL, DOMAIN, INVOICE_SCAN_INTERVAL
from .statistics import import_days_batch, import_invoice_cost_statistics

_LOGGER = logging.getLogger(__name__)

#: E.ON publish hourly readings about two days in arrears, and occasionally
#: later. Walking back a week finds the most recent day that actually exists
#: without assuming a fixed lag.
OLDEST_DAY_TO_TRY = 8
NEWEST_DAY_TO_TRY = 2


class EonConsumptionCoordinator(DataUpdateCoordinator[list[HourlyConsumption]]):
    """Fetches recent daily readings and imports them into statistics."""

    def __init__(
        self,
        hass: HomeAssistant,
        api: EonEnergiaClient,
        pod: str,
        tariff_type: str,
    ) -> None:
        """Set up the six-hourly consumption poll."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(hours=DEFAULT_SCAN_INTERVAL),
        )
        self.api = api
        self.pod = pod
        self.tariff_type = tariff_type
        #: Guards against importing the same day twice, and against the periodic
        #: poll writing statistics while a historical import is mid-flight.
        self.import_state: dict[str, Any] = {
            "last_date": None,
            "importing_historical": False,
        }

    async def _fetch_recent_days(self) -> list[tuple[datetime, HourlyConsumption]]:
        """Return every day we can find in the last week, oldest first."""
        found: list[tuple[datetime, HourlyConsumption]] = []
        for days_ago in range(NEWEST_DAY_TO_TRY, OLDEST_DAY_TO_TRY):
            target = datetime.now() - timedelta(days=days_ago)
            readings = await self.api.get_daily_consumption(
                pod=self.pod, start_date=target, end_date=target
            )
            if readings:
                found.append((target, readings[0]))

        found.sort(key=lambda item: item[0])
        return found

    async def _async_update_data(self) -> list[HourlyConsumption]:
        """Fetch the latest readings, importing any day not yet seen."""
        try:
            days = await self._fetch_recent_days()
        except EonEnergiaAuthError as err:
            raise UpdateFailed(f"Authentication failed: {err}") from err
        except EonEnergiaApiError as err:
            raise UpdateFailed(f"Error communicating with API: {err}") from err

        if not days:
            _LOGGER.warning("No consumption data found for the last 7 days")
            return []

        latest_target, latest_reading = days[-1]
        _LOGGER.debug("Found consumption data for %s", latest_target.date())

        # A historical import is rewriting the same statistic ids, so leave the
        # writing to it and just return data for the sensors.
        if self.import_state.get("importing_historical"):
            _LOGGER.debug("Skipping auto-import: historical import in progress")
            return [latest_reading]

        pending = [
            (target, reading)
            for target, reading in days
            if not self._already_imported(target, reading)
        ]
        if pending:
            # As one batch: the sums are cumulative, so importing day by day
            # would rebase the series once per day for no reason.
            await import_days_batch(self.hass, pending, self.pod, self.tariff_type)

        self.import_state["last_date"] = self._day_key(latest_target, latest_reading)
        return [latest_reading]

    def _already_imported(
        self, target: datetime, reading: HourlyConsumption
    ) -> bool:
        """Whether this day has been written since Home Assistant started."""
        last = self.import_state.get("last_date")
        return bool(last) and self._day_key(target, reading) <= last

    @staticmethod
    def _day_key(target: datetime, reading: HourlyConsumption) -> str:
        """The day this reading is for, preferring what E.ON actually labelled it."""
        return reading.day.isoformat() if reading.day else target.strftime("%Y-%m-%d")


class EonInvoiceCoordinator(DataUpdateCoordinator[list[Invoice]]):
    """Fetches invoices and derives the per-month prices costs are built from."""

    def __init__(self, hass: HomeAssistant, api: EonEnergiaClient, pod: str) -> None:
        """Set up the daily invoice poll."""
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_invoices",
            update_interval=timedelta(hours=INVOICE_SCAN_INTERVAL),
        )
        self.api = api
        self.pod = pod

    async def _async_update_data(self) -> list[Invoice]:
        """Fetch this supply's invoices and refresh the cost statistics."""
        try:
            invoices = await self.api.get_invoices_for_pod(self.pod)
        except EonEnergiaAuthError as err:
            raise UpdateFailed(f"Authentication failed: {err}") from err
        except EonEnergiaApiError as err:
            raise UpdateFailed(f"Error fetching invoices: {err}") from err

        _LOGGER.debug("Fetched %d invoices for POD %s", len(invoices), self.pod)
        if invoices:
            await import_invoice_cost_statistics(
                self.hass, self.api, invoices, self.pod
            )
        return invoices
