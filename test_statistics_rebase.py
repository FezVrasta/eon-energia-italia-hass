#!/usr/bin/env python3
"""Regression test for Energy Dashboard spikes and negative readings.

Home Assistant stores a cumulative ``sum`` per hour and the Energy Dashboard
renders the difference between consecutive hours. The importers used to seed
that running total from the *latest* stored row and then write earlier hours,
which produced a spike where the rewritten range started and a negative reading
where it rejoined untouched data.

These tests pin the invariants that prevent it:

  * ``sum`` never decreases
  * ``sum[n] - sum[n-1] == state[n]`` for every hour, including the first one,
    measured against whatever was already stored
  * importing the same data twice changes nothing

Run with: python test_statistics_rebase.py
"""

import asyncio
import datetime
import importlib.util
import pathlib
import sys
import types

UTC = datetime.timezone.utc
HOUR = datetime.timedelta(hours=1)
BASE = datetime.datetime(2026, 9, 1, 0, 0, tzinfo=UTC)
SID = "eon_energia:TESTPOD_consumption"

# The fake store the stubbed recorder reads from.
STORE: list[dict] = []


def _statistics_during_period(hass, start, end, ids, period, units, types_):
    rows = [
        dict(r) for r in STORE
        if r["start"] >= start and (end is None or r["start"] < end)
    ]
    return {SID: rows} if rows else {}


class _Instance:
    async def async_add_executor_job(self, fn, *args):
        return fn(*args)


def _stub(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module


def _install_stubs() -> None:
    """Stub enough of Home Assistant to import the integration standalone."""
    _stub("homeassistant")
    _stub("homeassistant.components")
    _stub("homeassistant.components.recorder", get_instance=lambda hass: _Instance())
    _stub("homeassistant.components.recorder.models", StatisticData=dict,
          StatisticMeanType=types.SimpleNamespace(NONE=0), StatisticMetaData=dict)
    _stub("homeassistant.components.recorder.statistics",
          async_add_external_statistics=lambda *a: None,
          clear_statistics=lambda *a: None,
          statistics_during_period=_statistics_during_period)
    _stub("homeassistant.components.sensor",
          SensorDeviceClass=types.SimpleNamespace(ENERGY="energy"))
    _stub("homeassistant.config_entries", ConfigEntry=object)
    _stub("homeassistant.const", CONF_PASSWORD="password", CONF_USERNAME="username",
          CURRENCY_EURO="EUR", Platform=types.SimpleNamespace(SENSOR="sensor"),
          UnitOfEnergy=types.SimpleNamespace(KILO_WATT_HOUR="kWh"))
    _stub("homeassistant.core", HomeAssistant=object, ServiceCall=object)
    _stub("homeassistant.helpers")
    _stub("homeassistant.helpers.update_coordinator",
          DataUpdateCoordinator=object, UpdateFailed=Exception)
    _stub("homeassistant.util")
    _stub("homeassistant.util.dt",
          utc_from_timestamp=lambda t: datetime.datetime.fromtimestamp(t, UTC),
          start_of_local_day=lambda d: d,
          now=lambda: datetime.datetime.now(UTC),
          as_local=lambda d: d,
          parse_datetime=lambda s: None)
    _stub("voluptuous", Schema=dict, Optional=lambda *a, **k: None,
          Required=lambda *a, **k: None, All=lambda *a: None,
          Range=lambda **k: None, Coerce=lambda t: t)


def _load_integration():
    base = pathlib.Path(__file__).resolve().parent / "custom_components" / "eon_energia"
    pkg_name = "eon_energia_under_test"
    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = [str(base)]
    sys.modules[pkg_name] = pkg

    class _Permissive(types.ModuleType):
        """Stands in for a sibling module we could not import (e.g. no aiohttp).

        Returns a placeholder for any attribute so `from .api import X` works;
        none of it is exercised by these tests.
        """

        def __getattr__(self, item):
            value = type(item, (Exception,), {})
            setattr(self, item, value)
            return value

    for name in ("const", "api", "auth", "api_config"):
        spec = importlib.util.spec_from_file_location(f"{pkg_name}.{name}", base / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[f"{pkg_name}.{name}"] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules[f"{pkg_name}.{name}"] = _Permissive(f"{pkg_name}.{name}")

    spec = importlib.util.spec_from_file_location(f"{pkg_name}.__init__", base / "__init__.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"{pkg_name}.__init__"] = module
    spec.loader.exec_module(module)
    return module


def _seed_store(hours: int = 48, value: float = 0.5) -> None:
    """Populate the fake store with a clean, already-imported series."""
    STORE.clear()
    running = 0.0
    for i in range(hours):
        running += value
        STORE.append({"start": BASE + i * HOUR, "state": value, "sum": running})


def _commit(series) -> None:
    """Apply a built series to the fake store, like async_add_external_statistics."""
    by_start = {r["start"]: r for r in STORE}
    for row in series:
        by_start[row["start"]] = dict(row)
    STORE[:] = [by_start[k] for k in sorted(by_start)]


def _assert_series_sane() -> None:
    for i in range(1, len(STORE)):
        delta = STORE[i]["sum"] - STORE[i - 1]["sum"]
        assert delta >= -1e-9, (
            f"sum went backwards at {STORE[i]['start']}: delta {delta:.3f}"
        )
        assert abs(delta - STORE[i]["state"]) < 1e-6, (
            f"sum delta {delta:.3f} != state {STORE[i]['state']:.3f} "
            f"at {STORE[i]['start']}"
        )
    assert abs(STORE[0]["sum"] - STORE[0]["state"]) < 1e-6, "first row sum != its state"


async def _run() -> None:
    _install_stubs()
    build = _load_integration()._build_rebased_statistics
    results = []

    def record(name, fn):
        try:
            fn()
            results.append((name, None))
        except AssertionError as err:
            results.append((name, str(err)))

    # 1. Re-importing an older window must not break either boundary.
    _seed_store()
    window = {STORE[i]["start"]: STORE[i]["state"] for i in range(10, 30)}
    _commit(await build(None, SID, window))
    record("re-import of an older window stays monotonic", _assert_series_sane)

    # 2. Re-importing the tail.
    _seed_store()
    tail = {STORE[i]["start"]: STORE[i]["state"] for i in range(40, 48)}
    _commit(await build(None, SID, tail))
    record("re-import of the most recent hours stays monotonic", _assert_series_sane)

    # 3. Importing twice changes nothing.
    _seed_store()
    _commit(await build(None, SID, window))
    snapshot = [(r["start"], round(r["sum"], 9)) for r in STORE]
    _commit(await build(None, SID, window))
    again = [(r["start"], round(r["sum"], 9)) for r in STORE]
    record("importing the same data twice is a no-op",
           lambda: (_assert_series_sane(),
                    (snapshot == again) or (_ for _ in ()).throw(
                        AssertionError("second import changed the series"))))

    # 4. A corrected value mid-series must shift everything after it.
    _seed_store()
    target = STORE[20]["start"]
    _commit(await build(None, SID, {target: 5.0}))
    def corrected():
        _assert_series_sane()
        row = next(r for r in STORE if r["start"] == target)
        assert abs(row["state"] - 5.0) < 1e-9, "corrected value was not stored"
    record("a corrected hour rebases the rest of the series", corrected)

    # 5. Appending a brand new hour.
    _seed_store()
    nxt = STORE[-1]["start"] + HOUR
    _commit(await build(None, SID, {nxt: 0.75}))
    record("appending a new hour stays monotonic", _assert_series_sane)

    # 6. First ever import, nothing stored.
    STORE.clear()
    _commit(await build(None, SID, {BASE + i * HOUR: 0.3 for i in range(5)}))
    record("first import starts from zero", _assert_series_sane)

    width = max(len(n) for n, _ in results)
    failed = 0
    for name, err in results:
        if err is None:
            print(f"PASS  {name}")
        else:
            failed += 1
            print(f"FAIL  {name.ljust(width)}  {err}")
    print()
    print(f"{len(results) - failed}/{len(results)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    asyncio.run(_run())
