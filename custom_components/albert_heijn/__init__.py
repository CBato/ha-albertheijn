"""Albert Heijn integration for Home Assistant."""

from __future__ import annotations

import logging
from pathlib import Path
import shutil

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_call_later, async_track_time_change

from .const import DOMAIN
from .coordinator import AlbertHeijnCoordinator
from .history import OrderHistory
from .intents import async_setup_intents
from .services import async_setup_services, async_unload_services

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]

CARD_FILENAME = "albert-heijn-orders-card.js"


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the Albert Heijn integration."""
    hass.data.setdefault(DOMAIN, {})
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Albert Heijn from a config entry."""
    coordinator = AlbertHeijnCoordinator(hass, entry)
    coordinator.history = OrderHistory(hass, coordinator.api, entry.entry_id)
    await coordinator.history.async_load()
    await coordinator.async_config_entry_first_refresh()

    # Order history, receipts, catalog, prices and bonus: shortly after start, then every night (and when an order
    # is delivered, see coordinator). Each step only fetches what is new or due. The basket and the next order are
    # live (coordinator, every minute).
    async def _sync(_now=None) -> None:
        await coordinator.async_sync_history()

    entry.async_on_unload(async_call_later(hass, 90, _sync))
    entry.async_on_unload(async_track_time_change(hass, _sync, hour=4, minute=15, second=0))

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Set up services and register card (only once for all entries)
    if len(hass.data[DOMAIN]) == 1:
        await async_setup_services(hass)
        await async_setup_intents(hass)

        # Copy the Lovelace card to www/ so it's served at /local/
        await hass.async_add_executor_job(_install_card, hass.config.path("www"))

    return True


def _install_card(www_dir: str) -> None:
    """Copy the card JS file to HA's www/community/albert_heijn directory."""
    dest_dir = Path(www_dir) / "community" / "albert_heijn"
    dest_dir.mkdir(parents=True, exist_ok=True)
    source = Path(__file__).parent / "www" / CARD_FILENAME
    dest = dest_dir / CARD_FILENAME
    if source.exists():
        shutil.copy2(source, dest)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)

    # Remove services if no entries remain
    if not hass.data[DOMAIN]:
        await async_unload_services(hass)

    return unload_ok
