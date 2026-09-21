#!/usr/bin/env python3
"""Tests for the DST hour mapping and the invoice-price sanity band.

The DST bug was real and observable: on 2025-10-26, when Italy's day is 25 hours
long, two of E.ON's hourly readings landed on the same instant. The second
overwrote the first while its value had already gone into the running total, so
that hour's cumulative delta came out at exactly twice its own value.

Run with: python test_dst_and_pricing.py
"""

import datetime
import importlib.util
import pathlib
import sys
import types
from zoneinfo import ZoneInfo

ROME = ZoneInfo("Europe/Rome")


def _stub(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module


def _start_of_local_day(day):
    """Stand-in for dt_util.start_of_local_day, pinned to Europe/Rome."""
    if day.tzinfo is None:
        day = day.replace(tzinfo=ROME)
    local = day.astimezone(ROME)
    return datetime.datetime(local.year, local.month, local.day, tzinfo=ROME)


def _install_stubs():
    _stub("homeassistant")
    _stub("homeassistant.components")
    _stub("homeassistant.components.recorder", get_instance=lambda hass: None)
    _stub("homeassistant.components.recorder.models", StatisticData=dict,
          StatisticMeanType=types.SimpleNamespace(NONE=0), StatisticMetaData=dict)
    _stub("homeassistant.components.recorder.statistics",
          async_add_external_statistics=lambda *a: None, clear_statistics=lambda *a: None,
          statistics_during_period=lambda *a, **k: {})
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
    _stub("homeassistant.util.dt", start_of_local_day=_start_of_local_day,
          utc_from_timestamp=lambda t: datetime.datetime.fromtimestamp(t, datetime.timezone.utc),
          now=lambda: datetime.datetime.now(ROME), as_local=lambda d: d.astimezone(ROME),
          parse_datetime=lambda s: None)
    _stub("voluptuous", Schema=dict, Optional=lambda *a, **k: None,
          Required=lambda *a, **k: None, All=lambda *a: None,
          Range=lambda **k: None, Coerce=lambda t: t)


def _load():
    base = pathlib.Path(__file__).resolve().parent / "custom_components" / "eon_energia"
    pkg_name = "eon_energia_dst_test"
    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = [str(base)]
    sys.modules[pkg_name] = pkg

    class _Permissive(types.ModuleType):
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


RESULTS = []


def check(name, fn):
    try:
        fn()
        RESULTS.append((name, None))
    except AssertionError as err:
        RESULTS.append((name, str(err)))


def main():
    _install_stubs()
    mod = _load()
    hour_starts = mod._local_hour_starts
    stat_time = mod._stat_time_for_hour

    day = lambda y, m, d: datetime.datetime(y, m, d, tzinfo=ROME)

    # --- the day that actually broke: 25 local hours ---
    def autumn_no_duplicates():
        d = day(2025, 10, 26)
        seen = [stat_time(d, h) for h in range(1, 25)]
        assert len(hour_starts(d)) == 25, f"expected a 25-hour day, got {len(hour_starts(d))}"
        assert None not in seen, "all 24 fields should map on a 25-hour day"
        assert len(set(seen)) == 24, (
            f"two fields collided: {len(set(seen))} distinct instants for 24 fields"
        )

    def autumn_covers_both_folds():
        # Fields 3 and 4 are the two 02:00s: one still on summer time, one after
        # the clocks go back. Same wall clock, an hour apart in real time.
        d = day(2025, 10, 26)
        a_utc, b_utc = (stat_time(d, h) for h in (3, 4))
        a, b = a_utc.astimezone(ROME), b_utc.astimezone(ROME)
        assert a.hour == b.hour == 2, f"expected both at 02:00 local, got {a} and {b}"
        assert a.utcoffset() != b.utcoffset(), (
            f"the repeated hour should span both offsets, got {a.utcoffset()} twice"
        )
        # Compare in UTC: subtracting two aware datetimes that share a tzinfo
        # ignores fold and subtracts the naive values, which would read as zero.
        assert b_utc - a_utc == datetime.timedelta(hours=1), (
            f"the two 02:00s should be an hour apart, got {b_utc - a_utc}"
        )

    # --- the other direction: 23 local hours ---
    def spring_drops_the_extra_field():
        d = day(2026, 3, 29)
        assert len(hour_starts(d)) == 23, f"expected a 23-hour day, got {len(hour_starts(d))}"
        assert stat_time(d, 24) is None, "field 24 has no hour on a 23-hour day"
        mapped = [stat_time(d, h) for h in range(1, 24)]
        assert None not in mapped, "fields 1-23 should all map"
        assert len(set(mapped)) == 23, "fields collided on the short day"

    def spring_skips_the_missing_hour():
        d = day(2026, 3, 29)
        locals_ = [t.astimezone(ROME).hour for t in hour_starts(d)]
        assert 2 not in locals_, f"02:00 does not exist on this day, got {locals_}"

    # --- ordinary days are unaffected ---
    def ordinary_day():
        d = day(2026, 6, 15)
        assert len(hour_starts(d)) == 24
        mapped = [stat_time(d, h) for h in range(1, 25)]
        assert len(set(mapped)) == 24
        assert [t.astimezone(ROME).hour for t in mapped] == list(range(24))
        assert stat_time(d, 25) is None and stat_time(d, 0) is None

    def hours_are_contiguous():
        for d in (day(2025, 10, 26), day(2026, 3, 29), day(2026, 6, 15)):
            hs = hour_starts(d)
            for i in range(1, len(hs)):
                gap = hs[i] - hs[i - 1]
                assert gap == datetime.timedelta(hours=1), f"{d.date()}: gap {gap}"

    # --- the price sanity band ---
    def price_band_rejects_the_real_case():
        # EUR165 invoice over the 23.05 kWh we had for that month.
        assert not (mod.MIN_DERIVED_PRICE <= 165.0 / 23.05 <= mod.MAX_DERIVED_PRICE), (
            "EUR7.16/kWh should be rejected"
        )

    def price_band_accepts_every_real_month():
        real = [0.3000, 0.2563, 0.2433, 0.2373, 0.2951,
                0.2928, 0.3269, 0.3795, 0.3510, 0.3507, 0.2514]
        for p in real:
            assert mod.MIN_DERIVED_PRICE <= p <= mod.MAX_DERIVED_PRICE, (
                f"EUR{p}/kWh is a real invoice price and must be accepted"
            )

    def price_band_has_headroom():
        assert mod.MIN_DERIVED_PRICE < 0.20, "band must allow cheap tariffs"
        assert mod.MAX_DERIVED_PRICE > 0.60, "band must allow expensive ones"

    check("autumn DST day: 24 fields map to 24 distinct instants", autumn_no_duplicates)
    check("autumn DST day: the repeated hour appears in both folds", autumn_covers_both_folds)
    check("spring DST day: the 24th field is dropped, not folded", spring_drops_the_extra_field)
    check("spring DST day: the non-existent 02:00 is skipped", spring_skips_the_missing_hour)
    check("ordinary day: unchanged, hours 0-23 in order", ordinary_day)
    check("every day: hour starts are exactly one hour apart", hours_are_contiguous)
    check("price band rejects EUR7.16/kWh from partial data", price_band_rejects_the_real_case)
    check("price band accepts all 11 real invoice prices", price_band_accepts_every_real_month)
    check("price band leaves headroom both ways", price_band_has_headroom)

    width = max(len(n) for n, _ in RESULTS)
    failed = 0
    for name, err in RESULTS:
        if err is None:
            print(f"PASS  {name}")
        else:
            failed += 1
            print(f"FAIL  {name.ljust(width)}  {err}")
    print()
    print(f"{len(RESULTS) - failed}/{len(RESULTS)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
