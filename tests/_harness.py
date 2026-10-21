"""Shared setup for the offline tests.

The logic (api, db, history, suggest) only needs a few Home Assistant names, so instead of installing Home Assistant
the tests stub those modules and load custom_components/albert_heijn as package `albert_heijn` without running its
__init__.py. Every test run gets its own temporary folder for albert_heijn.db.

Import this module before importing anything from albert_heijn.
"""

import datetime as _dt
import os
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
pkg_dir = os.path.join(ROOT, "custom_components")
tmp = tempfile.mkdtemp(prefix="albert-heijn-test-")


def _module(name: str, **attrs) -> types.ModuleType:
    mod = types.ModuleType(name)
    mod.__dict__.update(attrs)
    sys.modules[name] = mod
    return mod


# what the stubbed Store holds: the JSON history of fork 0.6/0.7 that OrderHistory imports once (None = nothing)
OLD_STORE = {"data": None, "removed": False}


class _Store:
    """homeassistant.helpers.storage.Store backed by OLD_STORE."""

    def __init__(self, *args) -> None:
        pass

    async def async_load(self):
        return OLD_STORE["data"]

    async def async_remove(self) -> None:
        OLD_STORE["removed"] = True


_dt_util = _module(
    "homeassistant.util.dt",
    get_default_time_zone=lambda: _dt.timezone.utc,
    as_local=lambda d: d.astimezone(),
)
_module("homeassistant")
_module("homeassistant.core", HomeAssistant=object)
_module("homeassistant.helpers")
_module("homeassistant.helpers.storage", Store=_Store)
_module("homeassistant.util", dt=_dt_util)
_module("aiohttp", ClientSession=object)

_pkg = _module("albert_heijn")
_pkg.__path__ = [os.path.join(pkg_dir, "albert_heijn")]
sys.path.insert(0, pkg_dir)


class ConfigEntries:
    """hass.config_entries with only async_entries (e.g. to find the Picnic integration)."""

    def __init__(self, entries=None) -> None:
        self.entries = entries or []

    def async_entries(self, domain: str):
        return [e for e in self.entries if e.domain == domain]
