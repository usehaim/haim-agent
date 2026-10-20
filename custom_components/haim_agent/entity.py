"""The device these entities hang off, and what they have in common.

One device per instance, so that everything HAIM adds to somebody's Home
Assistant sits in one place and can be looked at - or removed - as one thing.
A household that cannot find what a component added has no way to decide how it
feels about it.
"""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DOMAIN
from .coordinator import HaimCoordinator
from .version import AGENT_VERSION


class HaimEntity(Entity):
    """Base class: the device, the naming, and no polling of its own."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, coordinator: HaimCoordinator, key: str) -> None:
        self._coordinator = coordinator
        self._attr_unique_id = f"{coordinator.site_uuid or 'haim'}_{key}"
        self._attr_translation_key = key

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.site_uuid or "haim")},
            name="HAIM",
            manufacturer="Ennestudio",
            model="Instance agent",
            sw_version=AGENT_VERSION,
            # A service, not a box: it stops Home Assistant from filing this
            # under hardware that somebody might go looking for on a shelf.
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_added_to_hass(self) -> None:
        self._coordinator.add_listener(self.async_write_ha_state)

    async def async_will_remove_from_hass(self) -> None:
        self._coordinator.remove_listener(self.async_write_ha_state)
