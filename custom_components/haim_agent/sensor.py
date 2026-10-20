"""When HAIM was last heard, and what it is doing.

Two sensors, both readable at a glance. The state one exists because "not
connected" has more than one cause and they mean very different things: a
network that is down for ten minutes is not the same as a key that has been
revoked, and a household should not have to read a log to tell them apart.
"""

from __future__ import annotations

from datetime import datetime

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import HaimConfigEntry
from .entity import HaimEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HaimConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data

    async_add_entities([HaimLastHeartbeatSensor(coordinator), HaimStatusSensor(coordinator)])


class HaimLastHeartbeatSensor(HaimEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "last_heartbeat")

    @property
    def native_value(self) -> datetime | None:
        return self._coordinator.last_success


class HaimStatusSensor(HaimEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["connected", "retrying", "paused", "revoked"]

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "agent_status")

    @property
    def native_value(self) -> str:
        if self._coordinator.last_error == "token_revoked":
            return "revoked"

        if self._coordinator.paused:
            return "paused"

        return "connected" if self._coordinator.connected else "retrying"
