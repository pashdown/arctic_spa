"""Button platform for Arctic Spa."""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import ArcticSpaCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Arctic Spa buttons."""
    coordinator: ArcticSpaCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([ArcticSpaBoostButton(coordinator, entry)])


class ArcticSpaBoostButton(CoordinatorEntity[ArcticSpaCoordinator], ButtonEntity):
    """SpaBoy chlorine boost (the app's Boost button).

    One-shot: the spa runs the boost for its own fixed period and has no
    off command, so this is a button rather than a switch.
    """

    _attr_has_entity_name = True
    _attr_name = "SpaBoy Boost"
    _attr_icon = "mdi:flask-plus"

    def __init__(
        self,
        coordinator: ArcticSpaCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialize the boost button."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_spaboy_boost"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "Arctic Spa",
            "manufacturer": "Arctic Spas",
            "model": "Hot Tub",
        }

    @property
    def available(self) -> bool:
        """You can't press a button on a spa you're not connected to."""
        return super().available and self.coordinator.client.connected

    async def async_press(self) -> None:
        """Start the boost."""
        if not await self.coordinator.async_spaboy_boost():
            raise HomeAssistantError("Spa not connected; boost not sent")
