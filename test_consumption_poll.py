#!/usr/bin/env python3
"""Tests for how the consumption poll finds and skips days.

Setup used to take half a minute, for two reasons pinned here:

  * the recent week was fetched one day per request, at several seconds each.
    It is one ranged request now, and readings are keyed by the day E.ON
    labelled them rather than the day we asked for.
  * the "already imported" marker lived only in memory, so every restart
    re-imported and rebased the whole week. The first poll now seeds it from
    the last hour already in the recorder.

Run with: python test_consumption_poll.py
"""

from __future__ import annotations

import asyncio
import datetime
import importlib.util
import pathlib
import sys
import types
import zoneinfo

ROOT = pathlib.Path(__file__).resolve().parent
BASE = ROOT / "custom_components" / "eon_energia"
PKG = "eon_energia_poll_test"
ROME = zoneinfo.ZoneInfo("Europe/Rome")
SID = "eon_energia:POD1_consumption"

sys.path.insert(0, str(ROOT / "pyeonenergia" / "src"))

#: What the stubbed recorder answers for get_last_statistics.
LAST_STATS: dict = {}
#: Batches handed to import_days_batch.
IMPORTED: list = []


def _stub(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


class _Recorder:
    async def async_add_executor_job(self, func, *args):
        return func(*args)


class _Coordinator:
    def __init__(self, hass, logger, **kwargs):
        self.hass = hass

    def __class_getitem__(cls, item):
        return cls


class UpdateFailed(Exception):
    pass


def _install_stubs():
    _stub("homeassistant")
    _stub("homeassistant.components")
    _stub("homeassistant.components.recorder", get_instance=lambda hass: _Recorder())
    _stub(
        "homeassistant.components.recorder.statistics",
        get_last_statistics=lambda hass, n, sid, convert, types_: LAST_STATS,
    )
    _stub("homeassistant.core", HomeAssistant=object)
    _stub("homeassistant.helpers")
    _stub(
        "homeassistant.helpers.update_coordinator",
        DataUpdateCoordinator=_Coordinator,
        UpdateFailed=UpdateFailed,
    )
    _stub("homeassistant.util")
    _stub(
        "homeassistant.util.dt",
        utc_from_timestamp=lambda ts: datetime.datetime.fromtimestamp(
            ts, datetime.timezone.utc
        ),
        as_local=lambda value: value.astimezone(ROME),
    )

    pkg = types.ModuleType(PKG)
    pkg.__path__ = [str(BASE)]
    sys.modules[PKG] = pkg
    _stub(
        f"{PKG}.const",
        DEFAULT_SCAN_INTERVAL=6,
        DOMAIN="eon_energia",
        INVOICE_SCAN_INTERVAL=24,
    )

    async def import_days_batch(hass, days, pod, tariff_type):
        IMPORTED.append([reading.day.isoformat() for _, reading in days])

    async def import_invoice_cost_statistics(*args):
        return None

    _stub(
        f"{PKG}.statistics",
        import_days_batch=import_days_batch,
        import_invoice_cost_statistics=import_invoice_cost_statistics,
    )

    spec = importlib.util.spec_from_file_location(
        f"{PKG}.coordinator", BASE / "coordinator.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _Api:
    """Answers a ranged request with whatever days it was given."""

    def __init__(self, days):
        self.days = days
        self.calls: list[tuple[datetime.date, datetime.date]] = []

    async def get_daily_consumption(self, pod, start_date, end_date):
        from pyeonenergia import HourlyConsumption

        self.calls.append((start_date.date(), end_date.date()))
        return [
            HourlyConsumption.from_api(
                {"data": day.isoformat(), "valore_h01": "0,5"}
            )
            for day in self.days
        ]


def _day(days_ago):
    return (datetime.datetime.now() - datetime.timedelta(days=days_ago)).date()


def _stored_until(local_hour):
    """Make the recorder report ``local_hour`` as the last stored hour."""
    LAST_STATS.clear()
    LAST_STATS[SID] = [{"start": local_hour.timestamp()}]


async def _run():
    coordinator_module = _install_stubs()
    results = []

    def check(name, fn):
        try:
            fn()
            results.append((name, None))
        except Exception as err:  # noqa: BLE001 - reported, not raised
            results.append((name, err))

    def poll(days):
        api = _Api(days)
        coordinator = coordinator_module.EonConsumptionCoordinator(
            None, api, "POD1", "monoraria"
        )
        return api, coordinator

    # the week comes back in one request, oldest first, keyed by E.ON's day
    LAST_STATS.clear()
    IMPORTED.clear()
    days = [_day(n) for n in (2, 5, 3)]
    api, coordinator = poll(days)
    data = await coordinator._async_update_data()
    check(
        "the recent week is one request",
        lambda: api.calls == [(_day(7), _day(2))] or _fail(str(api.calls)),
    )
    check(
        "days are imported oldest first",
        lambda: IMPORTED
        == [[_day(5).isoformat(), _day(3).isoformat(), _day(2).isoformat()]]
        or _fail(str(IMPORTED)),
    )
    check(
        "the sensors get the newest day",
        lambda: data[0].day == _day(2) or _fail(str(data)),
    )

    # after a restart, a fully stored day is not imported again
    IMPORTED.clear()
    last_hour = datetime.datetime.combine(_day(3), datetime.time(23), ROME)
    _stored_until(last_hour)
    api, coordinator = poll([_day(n) for n in (4, 3, 2)])
    await coordinator._async_update_data()
    check(
        "a restart only imports days the recorder lacks",
        lambda: IMPORTED == [[_day(2).isoformat()]] or _fail(str(IMPORTED)),
    )

    # a day whose last hour is missing still counts as not imported
    IMPORTED.clear()
    last_hour = datetime.datetime.combine(_day(3), datetime.time(15), ROME)
    _stored_until(last_hour)
    api, coordinator = poll([_day(n) for n in (4, 3, 2)])
    await coordinator._async_update_data()
    check(
        "a partly stored day is imported again",
        lambda: IMPORTED == [[_day(3).isoformat(), _day(2).isoformat()]]
        or _fail(str(IMPORTED)),
    )

    # nothing new since the last run means no rebase at all
    IMPORTED.clear()
    last_hour = datetime.datetime.combine(_day(2), datetime.time(23), ROME)
    _stored_until(last_hour)
    api, coordinator = poll([_day(n) for n in (3, 2)])
    await coordinator._async_update_data()
    check(
        "an up to date recorder imports nothing",
        lambda: IMPORTED == [] or _fail(str(IMPORTED)),
    )

    width = max(len(n) for n, _ in results)
    failed = sum(1 for _, err in results if err)
    for name, err in results:
        print(f"PASS  {name}" if err is None else f"FAIL  {name.ljust(width)}  {err}")
    print()
    print(f"{len(results) - failed}/{len(results)} passed")
    sys.exit(1 if failed else 0)


def _fail(message):
    raise AssertionError(message)


if __name__ == "__main__":
    asyncio.run(_run())
