"""The Arctic Spa integration."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, Platform, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant, Event
from homeassistant.exceptions import ConfigEntryNotReady

from .const import DOMAIN
from .coordinator import ArcticSpaCoordinator
from .spa_client import ArcticSpaClient

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.CLIMATE,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.SWITCH,
    Platform.SELECT,
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Arctic Spa from a config entry."""
    host = entry.data[CONF_HOST]
    
    client = ArcticSpaClient(host)

    # Start persistent connection. If the spa isn't reachable/responding on
    # TCP 12121 yet, stop the background tasks and tell HA the entry isn't
    # ready so it retries setup with backoff instead of failing permanently.
    if not await client.async_start():
        await client.async_stop()
        raise ConfigEntryNotReady(
            f"Arctic Spa at {host}:{client.port} not responding; will retry"
        )

    coordinator = ArcticSpaCoordinator(hass, client)
    
    # Do initial data fetch
    await coordinator.async_config_entry_first_refresh()
    
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator
    
    # Register shutdown handler
    async def _async_shutdown(event: Event) -> None:
        """Shutdown the spa connection on HA stop."""
        await coordinator.async_shutdown()
    
    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_shutdown)
    )
    
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        coordinator: ArcticSpaCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_shutdown()
    
    return unload_ok
