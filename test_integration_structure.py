#!/usr/bin/env python3
"""Tests for the integration's own wiring, as opposed to the library's.

Two things here have already gone wrong once and are cheap to pin:

  * unique IDs. They were briefly derived from the config entry id, which
    duplicated every new entity on an instance that had two entries for one
    supply, and which orphans history if it ever changes again.
  * the import service. It used to find its work by walking hass.data; it now
    reads entry.runtime_data, and it has to set and clear the flag that stops
    the six-hourly poll writing the same statistic ids underneath it.

Run with: python test_integration_structure.py
"""

from __future__ import annotations

import asyncio
import importlib.util
import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parent
BASE = ROOT / "custom_components" / "eon_energia"
PKG = "eon_energia_structure_test"

# The repository root holds a `pyeonenergia/` directory whose package actually
# lives in `pyeonenergia/src/`. Without this the bare directory wins as a
# namespace package and every import from the library fails with
# "unknown location".
sys.path.insert(0, str(ROOT / "pyeonenergia" / "src"))


def _stub(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


class _Recorder:
    def __init__(self):
        self.cleared: list[str] = []

    def async_clear_statistics(self, ids):
        self.cleared.extend(ids)


RECORDER = _Recorder()


def _install_stubs():
    """Stub only what the two modules under test actually import."""
    _stub("homeassistant")
    _stub("homeassistant.components")
    _stub("homeassistant.components.recorder", get_instance=lambda hass: RECORDER)
    _stub("homeassistant.core", HomeAssistant=object, ServiceCall=object)
    _stub("homeassistant.helpers")
    _stub("homeassistant.helpers.device_registry", DeviceInfo=dict)
    _stub(
        "homeassistant.helpers.update_coordinator",
        CoordinatorEntity=type("CoordinatorEntity", (), {"__class_getitem__": classmethod(lambda cls, item: cls)}),
        DataUpdateCoordinator=object,
    )
    _stub("voluptuous", Schema=lambda *a, **k: None, Optional=lambda *a, **k: None,
          Required=lambda *a, **k: None, All=lambda *a: None,
          Range=lambda **k: None, Coerce=lambda t: t)

    pkg = types.ModuleType(PKG)
    pkg.__path__ = [str(BASE)]
    sys.modules[PKG] = pkg

    # pyeonenergia is installed for real; only const needs loading from disk.
    spec = importlib.util.spec_from_file_location(f"{PKG}.const", BASE / "const.py")
    const = importlib.util.module_from_spec(spec)
    sys.modules[f"{PKG}.const"] = const
    spec.loader.exec_module(const)

    # statistics.py pulls in a lot of recorder; the service only needs its two
    # entry points, so substitute a module that records how it was called.
    calls: list[tuple] = []

    async def import_historical_statistics(hass, api, pod, days, tariff_type):
        calls.append(("historical", pod, days, tariff_type))
        return {"2026-09-20": 5.0}

    async def import_invoice_cost_statistics(hass, api, invoices, pod, daily=None):
        calls.append(("costs", pod, len(invoices), daily is not None))

    _stub(
        f"{PKG}.statistics",
        import_historical_statistics=import_historical_statistics,
        import_invoice_cost_statistics=import_invoice_cost_statistics,
    )
    return calls


def _load(name):
    spec = importlib.util.spec_from_file_location(f"{PKG}.{name}", BASE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"{PKG}.{name}"] = module
    spec.loader.exec_module(module)
    return module


class _Coordinator:
    def __init__(self):
        self.import_state = {"last_date": None, "importing_historical": False}
        self.seen_flag_during_import = None


class _InvoiceCoordinator:
    def __init__(self, data):
        self.data = data
        self.refreshed = False

    async def async_request_refresh(self):
        self.refreshed = True


class _Data:
    def __init__(self, pod="POD1", invoices=None):
        self.api = object()
        self.pod = pod
        self.tariff_type = "multioraria"
        self.coordinator = _Coordinator()
        self.invoice_coordinator = _InvoiceCoordinator(invoices or [])
        self.supply = None


async def _run():
    calls = _install_stubs()
    entity = _load("entity")
    services = _load("services")
    results: list[tuple[str, str | None]] = []

    def check(name, fn):
        try:
            fn()
            results.append((name, None))
        except AssertionError as err:
            results.append((name, str(err)))

    # --- unique IDs -------------------------------------------------------
    check(
        "unique ids are keyed on the POD, not the entry",
        lambda: entity.unique_id("POD1", "daily_consumption") == "POD1_daily_consumption"
        or _fail(entity.unique_id("POD1", "daily_consumption")),
    )
    check(
        "the same supply and key always give the same id",
        lambda: entity.unique_id("POD1", "x") == entity.unique_id("POD1", "x")
        or _fail("not stable"),
    )
    check(
        "different supplies never collide",
        lambda: entity.unique_id("POD1", "x") != entity.unique_id("POD2", "x")
        or _fail("collision across PODs"),
    )

    info = entity.build_device_info("POD1")
    check(
        "device info identifies the supply",
        lambda: info["identifiers"] == {("eon_energia", "POD1")} or _fail(str(info)),
    )

    # --- the import service ----------------------------------------------
    data = _Data(invoices=[{"Numero": "1"}])
    calls.clear()
    await services._import_for_entry(None, data, days=30, clear=False)

    check(
        "consumption is imported before costs are derived",
        # Costs come from dividing an invoice by that month's consumption, so
        # the consumption has to be in hand first.
        lambda: [c[0] for c in calls] == ["historical", "costs", "historical"]
        or _fail(str([c[0] for c in calls])),
    )
    check(
        "the invoice poll is refreshed first",
        lambda: data.invoice_coordinator.refreshed or _fail("not refreshed"),
    )
    check(
        "the poll is unblocked again afterwards",
        lambda: data.coordinator.import_state["importing_historical"] is False
        or _fail("flag left set"),
    )
    check(
        "the poll is told not to re-import what was just written",
        lambda: data.coordinator.import_state["last_date"] is not None
        or _fail("last_date not set"),
    )

    # clear_existing goes through the recorder's own scheduling
    RECORDER.cleared.clear()
    calls.clear()
    await services._import_for_entry(None, _Data(), days=5, clear=True)
    check(
        "clear_existing clears every statistic this integration owns",
        lambda: len(RECORDER.cleared) == 5
        and all(c.startswith("eon_energia:POD1_") for c in RECORDER.cleared)
        or _fail(str(RECORDER.cleared)),
    )

    # a supply with no invoices must still import consumption
    calls.clear()
    await services._import_for_entry(None, _Data(invoices=[]), days=5, clear=False)
    check(
        "no invoices still imports consumption",
        lambda: [c[0] for c in calls] == ["historical"] or _fail(str(calls)),
    )

    # the flag must be cleared even when the import blows up
    failing = _Data()

    async def boom(*args, **kwargs):
        raise RuntimeError("API down")

    sys.modules[f"{PKG}.statistics"].import_historical_statistics = boom
    services.import_historical_statistics = boom
    try:
        await services._import_for_entry(None, failing, days=5, clear=False)
    except RuntimeError:
        pass
    check(
        "a failed import does not leave the poll blocked",
        lambda: failing.coordinator.import_state["importing_historical"] is False
        or _fail("flag left set after failure"),
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
